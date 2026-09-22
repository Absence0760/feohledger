"""MFA enrollment security — the second-factor lifecycle.

`test_mfa.py` covers the helper mechanics; `test_mfa_security.py`
covers replay / single-use of codes. This file pins the *enrollment*
contract:

  - Master switch off ⇒ enroll / verify / disable all 400
  - Starting an enrollment NEVER disturbs the factor already in force:
    the candidate secret waits in Redis and is promoted onto the account
    only by a successful verify
  - Re-enrolling over a live factor demands a step-up (password or a
    code from the current authenticator) — a stolen access token alone
    must not be able to strip or swap the second factor
  - First-time enrollment stays frictionless (no step-up to demand)
  - Verify with the wrong code does NOT flip mfa_enabled
  - Disable requires password re-entry (stolen session can't strip MFA)
  - Disable is refused when the org enforces MFA — a regression that
    let users opt out under org-required would silently downgrade the
    tenant's security posture
  - The MFAEnrollStartResponse exposes the secret EXACTLY once (only
    on the enrollment-start path, never on /me or /mfa/disable)
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _fake_user(
    *, has_password: bool = True, mfa_secret: str | None = None, mfa_enabled: bool = False
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="user@acme.test",
        full_name="Test User",
        organization_id=uuid.uuid4(),
        is_active=True,
        hashed_password=(
            "$bcrypt-sha256$v=2,t=2b,r=12$Jl.B.u9pD6kCuDNdO0nFfu$cK3Jg2DYkqzysEPJ0Q1opQpBVRQtyka"
        )
        if has_password
        else None,
        mfa_secret=mfa_secret,
        mfa_enabled=mfa_enabled,
        mfa_enrolled_at=None,
        must_change_password=False,
        delegate_to_id=None,
        delegate_until=None,
        locale=None,  # _user_response reads this (set-locale feature); None is fine
        roles=[],  # _user_response reads this; empty list is fine
    )


def _db_returning_org(org):
    """Mock the control session for an org lookup inside disable_mfa."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


def _passkey_db(existing=None, org=None):
    """Mock the control session for the `_user_passkeys` lookup enroll-start
    now makes (a registered passkey is a live factor for step-up purposes), and
    for the org lookup a password step-up makes (a password is no proof in an
    SSO-only tenant) — by default an org that has not closed password sign-in.
    The two never collide: the passkey list is read through `scalars()`, the org
    through `scalar_one_or_none()`."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = existing or []
    result.scalar_one_or_none.return_value = (
        org if org is not None else SimpleNamespace(settings={})
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _no_audit_dispatch(monkeypatch):
    """Failed step-ups now write a PII-free audit row. These are unit tests
    against mocked sessions, so stub the dispatcher — `test_step_up_failure_is_audited`
    asserts on it explicitly instead."""
    monkeypatch.setattr("app.api.auth.dispatch_auth_audit", AsyncMock())


# ---------------------------------------------------------------------------
# Master switch — enroll / verify / disable refused when MFA disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_mfa_start_refused_when_master_switch_off():
    from app.api.auth import enroll_mfa_start

    user = _fake_user()
    with patch("app.api.auth.settings.mfa_enabled", False):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_start(user=user, db=_passkey_db())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_enroll_mfa_verify_refused_when_master_switch_off():
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    user = _fake_user(mfa_secret="ABCDEFGH")
    db = AsyncMock()
    with patch("app.api.auth.settings.mfa_enabled", False):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_verify(body=MFAEnrollVerifyRequest(code="123456"), user=user, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Enroll flow — secret freshness + verify required to flip on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_time_enroll_start_is_frictionless_and_writes_nothing_to_the_account():
    """An account with no factor yet has nothing to protect, so enrollment
    starts with no step-up. The candidate secret still must NOT land on the
    account row — only a successful verify puts it there."""
    from app.api.auth import enroll_mfa_start
    from app.services import mfa as mfa_service

    user = _fake_user(mfa_secret=None, mfa_enabled=False)

    with patch("app.api.auth.settings.mfa_enabled", True):
        resp = await enroll_mfa_start(user=user, db=_passkey_db())

    assert resp.secret
    assert user.mfa_secret is None, "candidate secret must not be written to the account"
    assert user.mfa_enabled is False
    assert await mfa_service.read_pending_totp_secret(user.id) == resp.secret


@pytest.mark.asyncio
async def test_enroll_start_over_a_live_factor_without_step_up_is_refused():
    """THE regression this file exists for. A caller holding nothing but a
    valid access token used to be able to POST /mfa/enroll and, as a pure
    side effect of *starting* an enrollment they never finish, leave the
    victim's account with `mfa_enabled=False` — a transient session leak
    turned into a durable second-factor strip. Enrollment over a live factor
    must now be refused outright, and the live factor left untouched."""
    from app.api.auth import enroll_mfa_start
    from app.services import mfa as mfa_service

    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_start(user=user, db=_passkey_db())

    assert exc.value.status_code == 400
    assert user.mfa_enabled is True, "a session-only caller must not strip the live factor"
    assert user.mfa_secret == "JBSWY3DPEHPK3PXP"
    assert await mfa_service.read_pending_totp_secret(user.id) is None


@pytest.mark.asyncio
async def test_enroll_start_over_a_live_factor_with_a_wrong_password_is_refused():
    """A wrong step-up credential is no better than none."""
    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_start(
                body=MFAStepUpRequest(password="guess"), user=user, db=_passkey_db()
            )

    assert exc.value.status_code == 400
    assert user.mfa_enabled is True
    assert user.mfa_secret == "JBSWY3DPEHPK3PXP"


@pytest.mark.asyncio
async def test_enroll_start_with_password_step_up_keeps_the_live_factor_until_verify():
    """Positive control: a correct password lets re-enrollment start — and
    even then the live factor stays in force. The swap happens at verify, so
    an abandoned re-enrollment leaves the account exactly as it was."""
    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest
    from app.services import mfa as mfa_service

    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
    ):
        resp = await enroll_mfa_start(
            body=MFAStepUpRequest(password="correct"), user=user, db=_passkey_db()
        )

    assert resp.secret != "JBSWY3DPEHPK3PXP"
    assert user.mfa_secret == "JBSWY3DPEHPK3PXP", "live factor must survive enroll-start"
    assert user.mfa_enabled is True
    assert await mfa_service.read_pending_totp_secret(user.id) == resp.secret


@pytest.mark.asyncio
async def test_enroll_start_accepts_a_current_authenticator_code_as_step_up():
    """The user who lost their password manager but still holds the phone can
    re-enroll with a code from the CURRENT authenticator."""
    import pyotp

    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    secret = pyotp.random_base32()
    user = _fake_user(mfa_secret=secret, mfa_enabled=True)

    with patch("app.api.auth.settings.mfa_enabled", True):
        resp = await enroll_mfa_start(
            body=MFAStepUpRequest(code=pyotp.TOTP(secret).now()), user=user, db=_passkey_db()
        )

    assert resp.secret != secret
    assert user.mfa_secret == secret


@pytest.mark.asyncio
async def test_enroll_mfa_verify_with_wrong_code_keeps_mfa_disabled():
    """A wrong code at verify must NOT flip mfa_enabled. A regression
    that did so would let an attacker who started enrollment land
    "enrolled" without ever proving they hold the secret."""
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest
    from app.services import mfa as mfa_service

    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    await mfa_service.stash_pending_totp_secret(user.id, "JBSWY3DPEHPK3PXP")
    db = AsyncMock()
    db.commit = AsyncMock()

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.mfa.verify_totp", return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_verify(
                body=MFAEnrollVerifyRequest(code="000000"),
                user=user,
                db=db,
            )

    assert exc.value.status_code == 401
    assert user.mfa_enabled is False, "wrong-code verify must not flip mfa_enabled"
    assert user.mfa_enrolled_at is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_enroll_mfa_verify_refused_when_no_secret_present():
    """Calling verify before start (no candidate in flight) must 400,
    not silently flip mfa_enabled to True on an empty secret. The
    secret IS the credential — verifying nothing must not pass."""
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    db = AsyncMock()

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_verify(
                body=MFAEnrollVerifyRequest(code="123456"),
                user=user,
                db=db,
            )

    assert exc.value.status_code == 400
    assert user.mfa_enabled is False


@pytest.mark.asyncio
async def test_enroll_mfa_verify_with_correct_code_flips_on():
    """Positive control — without it, the negative tests could pass
    because verify always rejects."""
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        name="Acme",
        plan="pro",
        settings={},
    )
    from app.services import mfa as mfa_service

    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    await mfa_service.stash_pending_totp_secret(user.id, "JBSWY3DPEHPK3PXP")

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=org)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.mfa.verify_totp", return_value=True),
    ):
        resp = await enroll_mfa_verify(
            body=MFAEnrollVerifyRequest(code="999999"),
            user=user,
            db=db,
        )

    # Verify is the ONLY place the candidate becomes the account's factor.
    assert user.mfa_secret == "JBSWY3DPEHPK3PXP"
    assert user.mfa_enabled is True
    assert user.mfa_enrolled_at is not None
    assert resp.mfa_enabled is True
    db.commit.assert_called()
    # Candidate consumed — it can't be replayed into a second promotion.
    assert await mfa_service.read_pending_totp_secret(user.id) is None


# ---------------------------------------------------------------------------
# Disable — password re-entry + org-required check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_mfa_requires_password_re_entry():
    """A stolen session must not strip MFA. The disable handler
    re-verifies the password as a soft re-auth — without that,
    anyone with the bearer token can rip out the second factor."""
    from app.api.auth import disable_mfa
    from app.schemas.auth import MFADisableRequest

    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)
    # An org that has not closed password sign-in, so the password IS a proof
    # here and it is the wrong one that is refused.
    db = _db_returning_org(SimpleNamespace(id=user.organization_id, settings={}))

    with patch("app.utils.passwords.pwd_context.verify", return_value=False):
        with pytest.raises(HTTPException) as exc:
            await disable_mfa(body=MFADisableRequest(password="guess"), user=user, db=db)
    assert exc.value.status_code == 400
    # State untouched.
    assert user.mfa_enabled is True
    assert user.mfa_secret == "JBSWY3DPEHPK3PXP"


@pytest.mark.asyncio
async def test_disable_mfa_refused_when_org_requires_mfa():
    """When the org has flipped on `mfa.required`, individual users
    can't opt out. A regression that let them would silently downgrade
    the whole tenant's security posture."""
    from app.api.auth import disable_mfa
    from app.schemas.auth import MFADisableRequest

    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        settings={"mfa": {"required": True}},
    )
    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)
    user.organization_id = org.id

    db = _db_returning_org(org)

    with (
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.mfa.org_requires_mfa", return_value=True),
    ):
        with pytest.raises(HTTPException) as exc:
            await disable_mfa(body=MFADisableRequest(password="correct"), user=user, db=db)
    assert exc.value.status_code == 400
    assert "organization requires MFA" in exc.value.detail
    # Disable rejected → state intact.
    assert user.mfa_enabled is True


@pytest.mark.asyncio
async def test_disable_mfa_clears_secret_and_enrolled_at_on_success():
    """When disable succeeds (right password, org doesn't enforce),
    the secret must be wiped — keeping a stale secret around would
    let a re-enable shortcut around the enrollment ceremony."""
    from app.api.auth import disable_mfa
    from app.schemas.auth import MFADisableRequest

    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        name="Acme",
        plan="pro",
        settings={},
    )
    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)
    user.organization_id = org.id
    db = _db_returning_org(org)

    with (
        patch("app.utils.passwords.pwd_context.verify", return_value=True),
        patch("app.api.auth.mfa.org_requires_mfa", return_value=False),
    ):
        await disable_mfa(
            body=MFADisableRequest(password="correct"),
            user=user,
            db=db,
        )

    assert user.mfa_enabled is False
    assert user.mfa_secret is None
    assert user.mfa_enrolled_at is None


# ---------------------------------------------------------------------------
# Secret exposure — only on the enroll-start response
# ---------------------------------------------------------------------------


def test_user_response_schema_does_not_expose_mfa_secret():
    """`/api/auth/me` and other endpoints that surface the User
    object must NOT include the TOTP secret. The MFAEnrollStartResponse
    is the only response shape that legitimately carries the secret."""
    from app.schemas.auth import UserResponse

    fields = set(UserResponse.model_fields.keys())
    assert "mfa_secret" not in fields
    assert "secret" not in fields


def test_only_enroll_start_response_carries_the_secret():
    """Sweep the auth schemas — only `MFAEnrollStartResponse` may
    declare `secret`. A future "let me show the secret on /me" PR
    would be caught here."""
    import app.schemas.auth as auth_schemas

    permitted = {"MFAEnrollStartResponse"}
    violations: list[str] = []

    for name, cls in inspect.getmembers(auth_schemas, inspect.isclass):
        if not hasattr(cls, "model_fields"):
            continue
        for fname in cls.model_fields:
            if "secret" in fname.lower() and name not in permitted:
                violations.append(f"{name}.{fname}")

    assert not violations, (
        f"schema(s) exposing a `secret` field outside enrollment-start: {violations}"
    )


def test_user_response_carries_mfa_enabled_flag_not_secret():
    """The UI needs to know whether MFA is on — but only as a
    boolean. Confirm the bool is present (so the UI can render the
    enroll / disable buttons correctly)."""
    from app.schemas.auth import UserResponse

    assert "mfa_enabled" in UserResponse.model_fields


# ---------------------------------------------------------------------------
# A registered passkey counts as a live factor for the TOTP enroll step-up too
# (otherwise the passkey door stays open while the TOTP door is shut), and a
# failed step-up is throttled + audited rather than being a silent, unlimited
# password oracle on a credential-management endpoint.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_start_requires_step_up_when_only_a_passkey_is_registered():
    """No TOTP yet, but a registered passkey — still a live second factor, so
    adding TOTP to the account must re-prove control. Without this the
    attacker just walks through the TOTP door instead of the passkey one."""
    from app.api.auth import enroll_mfa_start

    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    db = _passkey_db([SimpleNamespace(credential_id=b"abc")])

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_start(user=user, db=db)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_step_up_failure_is_audited(monkeypatch):
    """A wrong password against a second-factor change must leave a trail —
    it's the signal that someone is working a stolen session. PII-free: the
    submitted credential never enters the row."""
    from app.api.auth import enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    audit = AsyncMock()
    monkeypatch.setattr("app.api.auth.dispatch_auth_audit", audit)
    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=False),
    ):
        with pytest.raises(HTTPException):
            await enroll_mfa_start(
                body=MFAStepUpRequest(password="guess"), user=user, db=_passkey_db()
            )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "auth.mfa.step_up.failure"
    assert kwargs["details"] == {"operation": "totp_enroll"}
    assert "guess" not in repr(kwargs), "the submitted credential must never be audited"


@pytest.mark.asyncio
async def test_step_up_is_rate_limited_per_account():
    """The step-up check is a password oracle unless it's throttled — and
    per-ACCOUNT, because the attacker already holds the victim's token and can
    rotate source IPs at will."""
    from app.api.auth import STEP_UP_RATE_LIMIT_PER_MINUTE, enroll_mfa_start
    from app.schemas.auth import MFAStepUpRequest

    user = _fake_user(mfa_secret="JBSWY3DPEHPK3PXP", mfa_enabled=True)
    statuses = []

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.utils.passwords.pwd_context.verify", return_value=False),
    ):
        for _ in range(STEP_UP_RATE_LIMIT_PER_MINUTE + 2):
            try:
                await enroll_mfa_start(
                    body=MFAStepUpRequest(password="guess"), user=user, db=_passkey_db()
                )
            except HTTPException as exc:
                statuses.append(exc.status_code)

    assert 429 in statuses, f"expected a 429 once over the cap, got {statuses}"
    assert user.mfa_enabled is True
