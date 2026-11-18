"""Tests that login / logout / MFA / SSO endpoints emit auth audit events.

These tests exercise the endpoint functions directly with an AsyncMock DB
and mocked session/audit helpers — no Postgres or Redis required. The goal
is to assert that every auth path writes the expected audit entry,
matching the SOC 2 auth-event coverage requirement.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_request(ip: str = "10.0.0.1"):
    req = MagicMock()
    req.client = SimpleNamespace(host=ip)
    return req


def _db_returning(user, org=None):
    """Build an AsyncMock DB whose first execute() returns the user, second the org."""
    from app.utils.passwords import pwd_context

    db = AsyncMock()
    user_result = MagicMock()
    user_result.scalar_one_or_none = MagicMock(return_value=user)
    org_result = MagicMock()
    org_result.scalar_one_or_none = MagicMock(return_value=org)
    # When MFA is enabled the login path also queries the user's passkeys
    # (_user_passkeys → db.execute → result.scalars().all()). Provide an empty
    # result as the third execute so the MFA path doesn't exhaust the mock.
    passkeys_result = MagicMock()
    passkeys_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(side_effect=[user_result, org_result, passkeys_result])
    # commit / refresh shouldn't be touched, but leave them as AsyncMock
    db.commit = AsyncMock()
    # Hash the user's password so verification works in the "success" path.
    if user and getattr(user, "hashed_password", None) == "REAL":
        user.hashed_password = pwd_context.hash("secret")
    return db


def _make_user(*, email="alice@acme.com", password="secret"):
    from app.utils.passwords import pwd_context

    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        hashed_password=pwd_context.hash(password),
        organization_id=uuid.uuid4(),
        is_active=True,
        must_change_password=False,
        mfa_enabled=False,
        mfa_secret=None,
        full_name="Alice",
    )


def _make_org():
    return SimpleNamespace(id=uuid.uuid4(), settings={})


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_writes_audit_and_registers_session():
    from app.api import auth as auth_mod
    from app.schemas.auth import LoginRequest

    user = _make_user()
    org = _make_org()
    db = _db_returning(user, org)

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    async def _fake_register(user_id, jti, **kwargs):
        calls.append({"register_session": (user_id, jti), "session_meta": kwargs})

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod, "register_session", _fake_register),
        patch.object(auth_mod.settings, "mfa_enabled", False),
    ):
        resp = await auth_mod.login(
            LoginRequest(email=user.email, password="secret"),
            _fake_request(),
            db,
        )

    assert resp.access_token
    # Session was registered + audit success written
    actions = [c.get("action") for c in calls if "action" in c]
    assert "auth.login.success" in actions
    assert any("register_session" in c for c in calls)


@pytest.mark.asyncio
async def test_login_failure_bad_password_writes_audit():
    from fastapi import HTTPException

    from app.api import auth as auth_mod
    from app.schemas.auth import LoginRequest

    user = _make_user(password="real-password")
    db = _db_returning(user, _make_org())

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    def _fake_queue(**kwargs):
        # A login FAILURE row is queued off the response path, not awaited —
        # the enumeration fix in `tests/test_login_audit_off_response_path.py`.
        calls.append(kwargs)

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod, "queue_auth_audit", _fake_queue),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.login(
                LoginRequest(email=user.email, password="wrong-password"),
                _fake_request(),
                db,
            )
    assert exc.value.status_code == 401

    actions = [c["action"] for c in calls]
    assert "auth.login.failure" in actions
    failure_call = next(c for c in calls if c["action"] == "auth.login.failure")
    assert failure_call["details"]["email"] == user.email
    assert failure_call["details"]["ip"] == "10.0.0.1"
    assert failure_call["details"]["reason"] == "bad_password"


@pytest.mark.asyncio
async def test_login_failure_unknown_email_does_not_crash():
    """Unknown-email failures have no user to bind, and must NOT raise — the
    endpoint still responds 401, just without an audit entry (we can't
    pick a tenant DB without an organization_id).

    The queue is still *called*, with `organization_id=None`: both rejection
    branches reach it identically, which is what keeps them
    indistinguishable. Only the row is dropped."""
    from fastapi import HTTPException

    from app.api import auth as auth_mod
    from app.schemas.auth import LoginRequest

    db = _db_returning(user=None, org=None)

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    def _fake_queue(**kwargs):
        calls.append(kwargs)

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod, "queue_auth_audit", _fake_queue),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.login(
                LoginRequest(email="ghost@nowhere.com", password="x"),
                _fake_request(),
                db,
            )
    assert exc.value.status_code == 401
    # Queued with no organization — so no row can be routed anywhere.
    assert [c["organization_id"] for c in calls] == [None]


@pytest.mark.asyncio
async def test_login_mfa_path_writes_challenge_issued_audit():
    from app.api import auth as auth_mod
    from app.schemas.auth import LoginRequest

    user = _make_user()
    user.mfa_enabled = True
    user.mfa_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    org = _make_org()
    db = _db_returning(user, org)

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod.settings, "mfa_enabled", True),
    ):
        resp = await auth_mod.login(
            LoginRequest(email=user.email, password="secret"),
            _fake_request(),
            db,
        )

    # Response is the MFA challenge, not a token
    assert getattr(resp, "mfa_challenge_token", None)
    actions = [c["action"] for c in calls]
    assert "auth.mfa.challenge_issued" in actions
    # Must NOT have issued login.success — user hasn't verified yet.
    assert "auth.login.success" not in actions


# ---------------------------------------------------------------------------
# MFA verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mfa_verify_success_writes_audit_and_registers_session():
    import pyotp

    from app.api import auth as auth_mod
    from app.schemas.auth import MFAVerifyRequest
    from app.services import mfa as mfa_svc

    user = _make_user()
    user.mfa_enabled = True
    user.mfa_secret = pyotp.random_base32()
    db = _db_returning(user)

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    async def _fake_register(user_id, jti, **kwargs):
        calls.append({"register_session": (user_id, jti), "session_meta": kwargs})

    challenge = mfa_svc.create_challenge_token(user.id)
    code = pyotp.TOTP(user.mfa_secret).now()

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod, "register_session", _fake_register),
        patch.object(auth_mod.settings, "mfa_enabled", True),
    ):
        resp = await auth_mod.verify_mfa(
            MFAVerifyRequest(challenge_token=challenge, code=code, method="totp"),
            _fake_request(),
            db,
        )

    assert resp.access_token
    actions = [c.get("action") for c in calls if "action" in c]
    assert "auth.mfa.verify.success" in actions
    assert "auth.login.success" in actions
    assert any("register_session" in c for c in calls)


@pytest.mark.asyncio
async def test_mfa_verify_failure_writes_audit():
    import pyotp
    from fastapi import HTTPException

    from app.api import auth as auth_mod
    from app.schemas.auth import MFAVerifyRequest
    from app.services import mfa as mfa_svc

    user = _make_user()
    user.mfa_enabled = True
    user.mfa_secret = pyotp.random_base32()
    db = _db_returning(user)

    calls: list[dict] = []

    async def _fake_audit(**kwargs):
        calls.append(kwargs)

    challenge = mfa_svc.create_challenge_token(user.id)

    with (
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
        patch.object(auth_mod.settings, "mfa_enabled", True),
    ):
        with pytest.raises(HTTPException) as exc:
            await auth_mod.verify_mfa(
                MFAVerifyRequest(challenge_token=challenge, code="000000", method="totp"),
                _fake_request(),
                db,
            )
    assert exc.value.status_code == 401

    actions = [c["action"] for c in calls]
    assert "auth.mfa.verify.failure" in actions
    assert "auth.mfa.verify.success" not in actions


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_writes_audit_and_untracks_session():
    from app.api import auth as auth_mod
    from app.api.deps import create_access_token_with_jti

    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token, jti = create_access_token_with_jti(user_id, org_id)

    calls: list = []

    async def _fake_end(user_id, jti, ttl):
        calls.append(("end_session", user_id, jti, ttl))

    async def _fake_audit(**kwargs):
        calls.append(("audit", kwargs["action"]))

    with (
        patch.object(auth_mod, "end_session", _fake_end),
        patch.object(auth_mod, "dispatch_auth_audit", _fake_audit),
    ):
        await auth_mod.logout(_fake_request(), authorization=f"Bearer {token}")

    # end_session called
    end_calls = [c for c in calls if c[0] == "end_session"]
    assert len(end_calls) == 1
    assert end_calls[0][1] == user_id
    assert end_calls[0][2] == jti
    # audit event logged
    assert ("audit", "auth.logout") in calls


# ---------------------------------------------------------------------------
# SSO callback success + failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sso_callback_success_writes_audit():
    """Lighter-weight coverage: the module must export dispatch_auth_audit
    + register_session so the success path and the failure path both have
    somewhere to write. Full flow is covered by the existing SSO tests."""
    from app.api import auth_sso

    assert hasattr(auth_sso, "dispatch_auth_audit")
    assert hasattr(auth_sso, "register_session")

    # Smoke: the success and failure action names are present in the module
    # source so a future refactor doesn't silently drop the audit hooks.
    import inspect

    src = inspect.getsource(auth_sso)
    assert "auth.sso.login.success" in src
    assert "auth.sso.login.failure" in src


# Quiet lint
_ = asyncio
