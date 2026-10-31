"""Generic-error / no-enumeration tests on the auth surface.

CWE-204 ("Observable Response Discrepancy"): a login that says
"unknown email" for missing accounts and "wrong password" for
existing-but-wrong-password is a free user-enumeration oracle for
phishing / credential-stuffing tooling. The contract is that every
failure mode raises the same HTTPException — same status code, same
detail string — regardless of whether the email exists.

Tests also cover:
  - Endpoints must not echo the plaintext password back to the caller
  - Audit-log dispatch must not receive plaintext passwords
  - The detail string must not name internal concepts ("JWT",
    "bearer") that help a fuzzer reach exposed surface area

Each test calls the handler / dependency function directly with
mocked DB sessions — same DB-free pattern as `test_auth_token_security`
and `test_payment_run_actions`. We exercise the production code path
end-to-end (including the audit-dispatch call) without spinning up
an HTTP transport.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

# Mock-DB tests: login writes an auth audit row through its own control-plane/tenant sessions
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("no_auth_audit_write")

# Real `bcrypt_sha256` hash of the string "correctpw1" — used by the
# fake-user fixtures so `verify_password` runs the production code
# path and reliably rejects any other guess.
_HASH_OF_CORRECTPW1 = (
    "$bcrypt-sha256$v=2,t=2b,r=12$Jl.B.u9pD6kCuDNdO0nFfu$cK3Jg2DYkqzysEPJ0Q1opQpBVRQtyka"
)


def _db_returning_user(user):
    """Mock the control-plane session whose `execute().scalar_one_or_none()`
    returns `user`."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _fake_request(ip: str = "203.0.113.1"):
    """Minimal mock of the FastAPI Request the login handler reads."""
    req = MagicMock()
    req.client = SimpleNamespace(host=ip)
    req.headers = {}
    return req


def _fake_user(email: str, *, has_password: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        organization_id=uuid.uuid4(),
        is_active=True,
        mfa_enabled=False,
        # Real bcrypt_sha256 hash, pinned in tests/test_bcrypt_sha256_compat.py;
        # it rejects any wrong password without the test guessing at CPU cycles.
        hashed_password=_HASH_OF_CORRECTPW1 if has_password else None,
        must_change_password=False,
        full_name="Test User",
    )


# ---------------------------------------------------------------------------
# /api/auth/login enumeration parity
#
# NB: SSO-only mode is a DELIBERATE exception to the 401-parity below — a tenant
# that requires SSO returns 403 + an "use your identity provider" message (so the
# user gets an actionable hint, and the workspace's SSO requirement is already
# public via /auth/{sso,saml}/config anyway). That 403-vs-401 break is pinned in
# tests/test_sso_only.py; don't collapse it back to 401 to "restore parity".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_unknown_email_and_wrong_password_raise_same_status():
    """An attacker can't distinguish "user exists" from "user doesn't
    exist" by reading the HTTP status — both branches must 401."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    with patch("app.api.auth.dispatch_auth_audit", AsyncMock()):
        # Branch 1: user not found
        with pytest.raises(HTTPException) as exc_unknown:
            await login(
                body=LoginRequest(email="noone@nowhere.test", password="x"),
                request=_fake_request(),
                db=_db_returning_user(None),
            )

        # Branch 2: user exists, wrong password
        with pytest.raises(HTTPException) as exc_wrong:
            await login(
                body=LoginRequest(email="real@user.test", password="wrong-password-12"),
                request=_fake_request(),
                db=_db_returning_user(_fake_user("real@user.test")),
            )

    assert exc_unknown.value.status_code == exc_wrong.value.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_email_runs_dummy_verify_to_equalize_timing():
    """The timing-oracle guard: a not-found login must still run a password
    verification (dummy_verify) so its wall-clock cost matches the
    wrong-password path — otherwise the faster not-found response enumerates
    which emails have accounts."""
    from app.api import auth
    from app.schemas.auth import LoginRequest

    with (
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
        patch("app.api.auth.dummy_verify", AsyncMock()) as dv,
    ):
        with pytest.raises(HTTPException):
            await auth.login(
                body=LoginRequest(email="noone@nowhere.test", password="x"),
                request=_fake_request(),
                db=_db_returning_user(None),
            )
    dv.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_unknown_email_and_wrong_password_raise_same_detail():
    """Same detail string for both branches. A wording diff ("user
    not found" vs "incorrect password") is a CWE-204 oracle even
    when statuses match."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    with patch("app.api.auth.dispatch_auth_audit", AsyncMock()):
        with pytest.raises(HTTPException) as exc_unknown:
            await login(
                body=LoginRequest(email="noone@nowhere.test", password="x"),
                request=_fake_request(),
                db=_db_returning_user(None),
            )

        with pytest.raises(HTTPException) as exc_wrong:
            await login(
                body=LoginRequest(email="real@user.test", password="wrong-password-12"),
                request=_fake_request(),
                db=_db_returning_user(_fake_user("real@user.test")),
            )

    assert exc_unknown.value.detail == exc_wrong.value.detail
    detail = exc_unknown.value.detail.lower()
    # Defensive: the detail should not name which branch was taken.
    for forbidden in ("user", "exist", "found", "incorrect"):
        assert forbidden not in detail, f"detail leaks enumeration term: {forbidden!r}"


@pytest.mark.asyncio
async def test_login_detail_does_not_echo_the_attempted_password():
    """The HTTPException raised on failed login must not include the
    password the caller sent — a debug-mode regression here would
    push every guess into nginx access logs."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    secret = "Secret-Sauce-Pizza-42"
    with patch("app.api.auth.dispatch_auth_audit", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await login(
                body=LoginRequest(email="real@user.test", password=secret),
                request=_fake_request(),
                db=_db_returning_user(_fake_user("real@user.test")),
            )

    assert secret not in (exc.value.detail or "")


@pytest.mark.asyncio
async def test_login_detail_does_not_echo_the_attempted_email():
    """Same property for the email — if the detail interpolated
    `body.email`, an XSS-via-error vector opens up for any client
    that renders the response directly."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    email = "victim+<script>@nowhere.test"
    with patch("app.api.auth.dispatch_auth_audit", AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await login(
                body=LoginRequest(email=email, password="x"),
                request=_fake_request(),
                db=_db_returning_user(None),
            )

    assert email not in (exc.value.detail or "")
    assert "<script>" not in (exc.value.detail or "")


@pytest.mark.asyncio
async def test_login_audit_dispatch_never_receives_plaintext_password():
    """We log auth events for SOC 2 — but the audit dispatch helper
    must never receive the plaintext password as part of `details`.
    A regression here means every brute-force attempt ends up in the
    central audit store along with the guesses."""
    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    captured: list[dict] = []

    async def fake_dispatch(**kwargs):
        captured.append(kwargs)

    def fake_queue(**kwargs):
        # The login-failure row is now QUEUED off the response path rather than
        # awaited (see `tests/test_login_audit_off_response_path.py`), so the
        # payload has to be inspected here — same contract, one fewer await.
        captured.append(kwargs)

    secret = "Secret-Sauce-Pizza-42"
    with (
        patch("app.api.auth.dispatch_auth_audit", new=fake_dispatch),
        patch("app.api.auth.queue_auth_audit", new=fake_queue),
    ):
        with pytest.raises(HTTPException):
            await login(
                body=LoginRequest(email="real@user.test", password=secret),
                request=_fake_request(),
                db=_db_returning_user(_fake_user("real@user.test")),
            )

    assert captured, "expected at least one audit dispatch"
    for call in captured:
        details = call.get("details") or {}
        serialised = repr(details).lower()
        assert secret.lower() not in serialised, "audit log leaked password"
        # The body must not carry a `password` key at all. The
        # `reason` field (no_password / bad_password) is a categorical
        # signal, not the value, so it stays.
        assert "password" not in {k.lower() for k in details}


# ---------------------------------------------------------------------------
# /api/portal/auth/login — same enumeration contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_login_unknown_email_and_wrong_password_raise_same_status():
    """Vendor-portal login mirrors the contract on the employee side."""
    from app.api.portal_auth import portal_login
    from app.schemas.portal import PortalLoginRequest

    # Branch 1: vendor user not found
    with pytest.raises(HTTPException) as exc_unknown:
        await portal_login(
            body=PortalLoginRequest(email="noone@nowhere.test", password="x"),
            request=_fake_request(),
            slug="acme",
            db=_db_returning_user(None),
        )

    # Branch 2: vendor user exists, wrong password
    vu = SimpleNamespace(
        id=uuid.uuid4(),
        email="real@vendor.test",
        vendor_id=uuid.uuid4(),
        # A rejected sign-in now writes `portal.login.failure` into this org's
        # trail, so the stand-in needs the column the dispatcher resolves the
        # tenant DB from. `None` exercises the legacy-row skip.
        organization_id=None,
        is_active=True,
        hashed_password=_HASH_OF_CORRECTPW1,
        must_change_password=False,
    )
    with pytest.raises(HTTPException) as exc_wrong:
        await portal_login(
            body=PortalLoginRequest(email="real@vendor.test", password="wrong-12"),
            request=_fake_request(),
            slug="acme",
            db=_db_returning_user(vu),
        )

    assert exc_unknown.value.status_code == exc_wrong.value.status_code == 401
    assert exc_unknown.value.detail == exc_wrong.value.detail


@pytest.mark.asyncio
async def test_portal_login_unknown_email_runs_dummy_verify():
    """Same timing-equalization guard on the vendor-portal login."""
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest

    with patch("app.api.portal_auth.dummy_verify", AsyncMock()) as dv:
        with pytest.raises(HTTPException):
            await portal_auth.portal_login(
                body=PortalLoginRequest(email="noone@nowhere.test", password="x"),
                request=_fake_request(),
                slug="acme",
                db=_db_returning_user(None),
            )
    dv.assert_awaited_once()


@pytest.mark.asyncio
async def test_portal_login_does_not_echo_password_in_detail():
    """Same anti-echo contract on the portal side."""
    from app.api.portal_auth import portal_login
    from app.schemas.portal import PortalLoginRequest

    secret = "Portal-Secret-99"
    with pytest.raises(HTTPException) as exc:
        await portal_login(
            body=PortalLoginRequest(email="real@vendor.test", password=secret),
            request=_fake_request(),
            slug="acme",
            db=_db_returning_user(None),
        )
    assert secret not in (exc.value.detail or "")


# ---------------------------------------------------------------------------
# Generic detail on unauthenticated protected routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_protected_request_has_generic_detail():
    """The 401 from `get_current_user` must not name internal
    concepts ("JWT", "bearer") that help a fuzzer."""
    from app.api.deps import get_current_user

    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=None, db=db)
    detail = (exc.value.detail or "").lower()
    assert exc.value.status_code == 401
    assert "jwt" not in detail
    assert "bearer" not in detail


@pytest.mark.asyncio
async def test_unauthenticated_protected_request_does_not_leak_token_value():
    """If a caller sends a malformed Authorization, the response
    must not include that value (defence against an integrator
    accidentally pasting their token into a screenshot)."""
    from app.api.deps import get_current_user

    fake_token = "Bearer SENSITIVE_LEAKABLE_VALUE_12345"
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=fake_token, db=db)
    assert "SENSITIVE_LEAKABLE_VALUE_12345" not in (exc.value.detail or "")


# ---------------------------------------------------------------------------
# Schema-level validation
# ---------------------------------------------------------------------------


def test_login_request_rejects_missing_password_at_schema_level():
    """`LoginRequest` must reject a body with no password — Pydantic
    422 runs before the handler, preventing a "password defaults to
    empty" regression from ever reaching bcrypt."""
    import pydantic

    from app.schemas.auth import LoginRequest

    with pytest.raises(pydantic.ValidationError):
        LoginRequest(email="x@y.test")  # type: ignore[call-arg]


def test_login_request_rejects_missing_email_at_schema_level():
    import pydantic

    from app.schemas.auth import LoginRequest

    with pytest.raises(pydantic.ValidationError):
        LoginRequest(password="anything")  # type: ignore[call-arg]


def test_portal_login_request_rejects_missing_fields_at_schema_level():
    """Same shape check for the vendor-portal login schema."""
    import pydantic

    from app.schemas.portal import PortalLoginRequest

    with pytest.raises(pydantic.ValidationError):
        PortalLoginRequest(email="x@y.test")  # type: ignore[call-arg]
    with pytest.raises(pydantic.ValidationError):
        PortalLoginRequest(password="anything")  # type: ignore[call-arg]
