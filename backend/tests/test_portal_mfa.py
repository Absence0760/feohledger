"""Supplier-portal MFA (TOTP) tests.

Mirrors the employee MFA tests but for VendorUser + the `typ=vendor` JWT.
Covers: enroll → verify → login-now-challenges → challenge-verify → token;
disable; wrong-code rejected; and — the security-critical part — that the
three token types (`vendor` access, `vendor_mfa_challenge`, employee
`mfa_challenge`) stay strictly separate so there's no cross-auth leak.

Unit-level: the tenant DB session is mocked. `FEOH_MFA_ENABLED` is forced on for
the flow tests via the `settings` object (the master switch the endpoints read).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pyotp
import pytest
from fastapi import HTTPException

from app.api.deps import create_vendor_access_token
from app.api.portal_auth import (
    portal_login,
    portal_mfa_challenge,
    portal_mfa_disable,
    portal_mfa_enroll,
    portal_mfa_verify,
    portal_request_email_otp,
)
from app.config import settings
from app.schemas.portal import (
    PortalLoginRequest,
    PortalMFAChallengeResponse,
    PortalMFAChallengeVerifyRequest,
    PortalMFADisableRequest,
    PortalMFAEmailChallengeRequest,
    PortalMFAStepUpRequest,
    PortalMFAVerifyRequest,
    PortalTokenResponse,
)
from app.services import mfa


@pytest.fixture(autouse=True)
def _session_capable_redis(monkeypatch):
    """Portal sign-in now REGISTERS the session (a Redis zset + companion hash),
    which is what gives "sign out my other devices" something to revoke. The
    autouse conftest fake is key/value only, so swap in the richer stand-in for
    ``app.redis`` here. ``app.services.mfa``'s Redis stays on the conftest fake.
    """
    from tests.test_session_management import FakeRedis

    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


def _fake_request() -> SimpleNamespace:
    """Stand-in for the FastAPI Request the portal auth routes read.

    A bare ``MagicMock()`` was enough while the routes only passed it to the
    rate limiter, but a successful sign-in now RECORDS the session (client IP +
    a coarse device label) — and a MagicMock IP isn't JSON-serialisable. This
    stub exposes exactly the two attributes the code reads.
    """
    return SimpleNamespace(client=None, headers={"user-agent": "Chrome on macOS"})


@pytest.fixture
def mfa_on(monkeypatch):
    """Flip the platform MFA master switch on for the duration of a test."""
    monkeypatch.setattr(settings, "mfa_enabled", True)
    yield


@pytest.fixture(autouse=True)
def _no_audit_dispatch(monkeypatch):
    """A failed portal step-up now writes a PII-free audit row. These are unit
    tests against a mocked tenant session, so stub the dispatcher —
    `test_portal_step_up_failure_is_audited` asserts on it explicitly."""
    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", AsyncMock())


def _mock_db(*, vendor_user=None, vendor=None):
    """A MagicMock tenant session whose `execute(...).scalar_one_or_none()`
    returns `vendor_user` first then `vendor` (the order the endpoints query)."""
    returns = []
    if vendor_user is not None:
        returns.append(vendor_user)
    if vendor is not None:
        returns.append(vendor)

    seq = iter(returns)

    def _execute(*_a, **_k):
        res = MagicMock()
        try:
            res.scalar_one_or_none.return_value = next(seq)
        except StopIteration:
            res.scalar_one_or_none.return_value = None
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


def _vendor_user(**overrides):
    base = dict(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="supplier@vendor.com",
        full_name="Supplier Rep",
        hashed_password=None,
        is_active=True,
        must_change_password=False,
        mfa_secret=None,
        mfa_enabled=False,
        mfa_enrolled_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _vendor():
    return SimpleNamespace(id=uuid.uuid4(), name="Acme Supplies", status="active")


# ---------------------------------------------------------------------------
# Enroll → verify → flips mfa_enabled on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_then_verify_activates_mfa(mfa_on):
    vu = _vendor_user()
    db = _mock_db(vendor_user=None, vendor=_vendor())

    enroll = await portal_mfa_enroll(vu=vu)
    assert enroll.secret  # plaintext secret returned during enrollment
    assert enroll.provisioning_uri.startswith("otpauth://")
    assert enroll.qr_code_data_url.startswith("data:image/png;base64,")
    # The candidate waits in Redis — nothing is written to the account yet.
    assert vu.mfa_secret is None
    assert vu.mfa_enabled is False
    assert await mfa.read_pending_vendor_totp_secret(vu.id) == enroll.secret

    code = pyotp.TOTP(enroll.secret).now()
    me = await portal_mfa_verify(body=PortalMFAVerifyRequest(code=code), vu=vu, db=db)
    assert vu.mfa_secret == enroll.secret  # promoted only now
    assert vu.mfa_enabled is True
    assert vu.mfa_enrolled_at is not None
    assert me.mfa_enabled is True
    assert await mfa.read_pending_vendor_totp_secret(vu.id) is None


@pytest.mark.asyncio
async def test_verify_wrong_code_rejected(mfa_on):
    vu = _vendor_user()
    await mfa.stash_pending_vendor_totp_secret(vu.id, pyotp.random_base32())
    db = _mock_db()
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_verify(body=PortalMFAVerifyRequest(code="000000"), vu=vu, db=db)
    assert exc.value.status_code == 401
    assert vu.mfa_enabled is False
    assert vu.mfa_secret is None


@pytest.mark.asyncio
async def test_verify_without_enrollment_400(mfa_on):
    vu = _vendor_user(mfa_secret=None)
    db = _mock_db()
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_verify(body=PortalMFAVerifyRequest(code="123456"), vu=vu, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Re-enrollment step-up — the supplier-portal half of the fix for the
# "MFA silently disabled via re-enrollment" hole.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_over_a_live_factor_without_step_up_is_refused(mfa_on):
    """The portal twin of the employee regression: a caller holding only a
    stolen vendor access token used to be able to POST /portal/auth/mfa/enroll
    and, purely as a side effect of starting an enrollment they never finish,
    leave the supplier's account with `mfa_enabled=False`. Refused now, with
    the live factor untouched."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)

    with pytest.raises(HTTPException) as exc:
        await portal_mfa_enroll(vu=vu)

    assert exc.value.status_code == 400
    # Coded, so the portal page states it in the supplier's language; the
    # English stays as the fallback.
    assert exc.value.detail == {
        "code": "portal_step_up_failed",
        "message": (
            "Confirm your password or a current authenticator code to change "
            "your two-factor settings."
        ),
        "params": {},
    }
    assert vu.mfa_enabled is True, "a session-only caller must not strip the live factor"
    assert vu.mfa_secret == secret
    assert await mfa.read_pending_vendor_totp_secret(vu.id) is None


@pytest.mark.asyncio
async def test_enroll_over_a_live_factor_accepts_a_current_code(mfa_on):
    """A code from the CURRENT authenticator satisfies the step-up — and even
    then the live factor survives until the new one verifies."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)

    enroll = await portal_mfa_enroll(
        body=PortalMFAStepUpRequest(code=pyotp.TOTP(secret).now()), vu=vu
    )

    assert enroll.secret != secret
    assert vu.mfa_secret == secret, "live factor must survive enroll-start"
    assert vu.mfa_enabled is True


@pytest.mark.asyncio
async def test_enroll_over_a_live_factor_accepts_the_portal_password(mfa_on, monkeypatch):
    """Password re-entry is the other accepted step-up, matching the employee
    surface and `/mfa/disable`."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True, hashed_password="hash")
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: True)

    enroll = await portal_mfa_enroll(body=PortalMFAStepUpRequest(password="correct"), vu=vu)

    assert enroll.secret != secret
    assert vu.mfa_secret == secret


@pytest.mark.asyncio
async def test_enroll_over_a_live_factor_rejects_a_wrong_password(mfa_on, monkeypatch):
    """A wrong step-up credential is no better than none."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True, hashed_password="hash")
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    with pytest.raises(HTTPException) as exc:
        await portal_mfa_enroll(body=PortalMFAStepUpRequest(password="guess"), vu=vu)

    assert exc.value.status_code == 400
    assert vu.mfa_enabled is True
    assert vu.mfa_secret == secret


@pytest.mark.asyncio
async def test_first_time_portal_enroll_needs_no_step_up(mfa_on):
    """Onboarding must stay frictionless — a vendor with no factor yet has
    nothing to protect."""
    vu = _vendor_user()

    enroll = await portal_mfa_enroll(vu=vu)

    assert enroll.secret
    assert vu.mfa_secret is None


# ---------------------------------------------------------------------------
# Login now challenges → challenge-verify → real token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_challenges_when_mfa_enrolled(mfa_on, monkeypatch):
    secret = pyotp.random_base32()
    vu = _vendor_user(hashed_password="x", mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_: True)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))

    res = await portal_login(
        body=PortalLoginRequest(email=vu.email, password="pw"),
        request=_fake_request(),
        slug="acme",
        db=db,
    )
    assert isinstance(res, PortalMFAChallengeResponse)
    assert res.mfa_required is True
    # TOTP primary + the email-OTP backup factor are both offered.
    assert res.methods == ["totp", "email"]
    # The challenge token must carry the vendor-challenge typ, NOT an access token.
    claims = await mfa.decode_vendor_challenge_token(res.mfa_challenge_token)
    assert claims.subject_id == vu.id


@pytest.mark.asyncio
async def test_login_no_challenge_when_not_enrolled(mfa_on, monkeypatch):
    vu = _vendor_user(hashed_password="x", mfa_enabled=False)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_: True)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))

    res = await portal_login(
        body=PortalLoginRequest(email=vu.email, password="pw"),
        request=_fake_request(),
        slug="acme",
        db=db,
    )
    assert isinstance(res, PortalTokenResponse)
    assert res.access_token


@pytest.mark.asyncio
async def test_login_skips_mfa_when_master_switch_off(monkeypatch):
    """FEOH_MFA_ENABLED off → enrolled vendor still gets a plain token."""
    monkeypatch.setattr(settings, "mfa_enabled", False)
    vu = _vendor_user(hashed_password="x", mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_: True)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))

    res = await portal_login(
        body=PortalLoginRequest(email=vu.email, password="pw"),
        request=_fake_request(),
        slug="acme",
        db=db,
    )
    assert isinstance(res, PortalTokenResponse)


@pytest.mark.asyncio
async def test_challenge_verify_mints_access_token(mfa_on, monkeypatch):
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))

    challenge = mfa.create_vendor_challenge_token(vu.id)
    code = pyotp.TOTP(secret).now()
    res = await portal_mfa_challenge(
        body=PortalMFAChallengeVerifyRequest(challenge_token=challenge, code=code),
        request=_fake_request(),
        slug="acme",
        db=db,
    )
    assert isinstance(res, PortalTokenResponse)
    # The minted token is a real vendor access token.
    from app.api.deps import decode_token

    payload = decode_token(res.access_token)
    assert payload["typ"] == "vendor"
    assert payload["sub"] == str(vu.id)


@pytest.mark.asyncio
async def test_challenge_verify_wrong_code_rejected(mfa_on, monkeypatch):
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))

    challenge = mfa.create_vendor_challenge_token(vu.id)
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_challenge(
            body=PortalMFAChallengeVerifyRequest(challenge_token=challenge, code="000000"),
            request=_fake_request(),
            slug="acme",
            db=db,
        )
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# The second-factor stage of a supplier sign-in leaves evidence
# ---------------------------------------------------------------------------
#
# `/portal/auth/login` is the only portal handler that audits, and it audits
# only REJECTIONS (`portal.login.failure`). Once a supplier enrols a second
# factor, `/login` stops minting the token and hands back a challenge — so the
# sign-in completes here instead, and before these rows turning MFA on took the
# whole account's sign-in off the trail. `/mfa/challenge` is the portal twin of
# `api/auth.verify_mfa`, which has audited both outcomes since it was built.
#
# Deliberately NOT audited: `/mfa/challenge/email`, which only issues a code —
# see the reason string in `test_audit_append_only._TENANT_MUTATORS_WITHOUT_
# DIRECT_AUDIT`, and `test_requesting_an_email_otp_writes_no_audit_row` below,
# which pins that as a decision rather than an omission.


def _audit_spy(monkeypatch):
    """Replace the autouse stub with one whose calls this test reads."""
    spy = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", spy)
    return spy


def _audit_rows(spy) -> list[dict]:
    return [call.kwargs for call in spy.await_args_list]


def _audit_row(spy, action: str) -> dict:
    """The one row for `action`, asserting there is exactly one.

    A successful MFA challenge writes TWO rows — `portal.mfa.verify.success`
    (which factor) and `portal.login.success` (a session was minted), matching
    what the employee twin writes. So a bare single-row unpack no longer says
    what it used to; select by action instead, and keep the "exactly one"
    assertion that made the unpack worth writing.
    """
    rows = [r for r in _audit_rows(spy) if r["action"] == action]
    assert len(rows) == 1, f"expected exactly one {action} row, got {len(rows)}"
    return rows[0]


async def _challenge(vu, code, monkeypatch, *, method="totp", ip="203.0.113.9", validated=True):
    """Drive `portal_mfa_challenge` directly.

    `validated=False` builds the body with `model_construct`, skipping Pydantic
    — the only way to ask what the HANDLER does with a `method` the schema
    would refuse, now that it pins `^(totp|email)$`. Used by exactly one test,
    to keep the route's own derivation covered as the second gate.
    """
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    request = _fake_request()
    request.client = SimpleNamespace(host=ip)
    fields = {
        "challenge_token": mfa.create_vendor_challenge_token(vu.id),
        "code": code,
        "method": method,
    }
    body = (
        PortalMFAChallengeVerifyRequest(**fields)
        if validated
        else PortalMFAChallengeVerifyRequest.model_construct(**fields)
    )
    return await portal_mfa_challenge(
        body=body,
        request=request,
        slug="acme",
        db=_mock_db(vendor_user=vu),
    )


@pytest.mark.asyncio
async def test_challenge_verify_success_is_audited(mfa_on, monkeypatch):
    """A completed second factor is a sign-in, and a sign-in is evidence."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    spy = _audit_spy(monkeypatch)

    code = pyotp.TOTP(secret).now()
    await _challenge(vu, code, monkeypatch)

    row = _audit_row(spy, "portal.mfa.verify.success")
    assert row["action"] == "portal.mfa.verify.success"
    assert row["organization_id"] == vu.organization_id
    # Subject is the vendor user on both axes, matching every other portal MFA
    # row — an auditor filters the trail by `entity_id`.
    assert row["actor_id"] == vu.id
    assert row["entity_id"] == vu.id
    assert row["details"] == {"method": "totp", "ip": "203.0.113.9"}
    # The row is about a credential; it must never carry one.
    assert secret not in repr(row)
    assert code not in repr(row)


@pytest.mark.asyncio
async def test_an_mfa_sign_in_is_also_a_sign_in(mfa_on, monkeypatch):
    """A supplier who carries a second factor still appears in `login.success`.

    Without this the action returned only the password-only subset, which is a
    BIASED SAMPLE rather than a gap: an auditor querying successful supplier
    sign-ins would silently miss exactly the accounts that are best protected,
    and nothing in the result would say so. That is why it was not half-fixed
    when the password-only row landed.

    The factor row and the sign-in row answer different questions — *which
    second factor* versus *a session was minted* — and the employee twin
    already writes both (`auth.mfa.verify.success` + `auth.login.success`).
    """
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    spy = _audit_spy(monkeypatch)

    code = pyotp.TOTP(secret).now()
    await _challenge(vu, code, monkeypatch)

    assert {r["action"] for r in _audit_rows(spy)} == {
        "portal.mfa.verify.success",
        "portal.login.success",
    }

    row = _audit_row(spy, "portal.login.success")
    assert row["organization_id"] == vu.organization_id
    assert row["actor_id"] == vu.id
    assert row["entity_id"] == vu.id
    # The method names the factor actually used, so the two rows can be
    # reconciled without joining them; the shape mirrors the employee twin's
    # `password+mfa:{factor}`.
    assert row["details"] == {"ip": "203.0.113.9", "method": "password+mfa:totp"}
    # PII-free and closed, the same rule the password-only row is held to.
    assert set(row["details"]) == {"ip", "method"}
    assert vu.email not in repr(row)
    assert secret not in repr(row)
    assert code not in repr(row)


@pytest.mark.asyncio
async def test_no_sign_in_row_when_the_second_factor_is_wrong(mfa_on, monkeypatch):
    """A refused factor is not a sign-in, so it gets no sign-in row."""
    vu = _vendor_user(mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    spy = _audit_spy(monkeypatch)

    with pytest.raises(HTTPException):
        await _challenge(vu, "000000", monkeypatch)

    assert [r["action"] for r in _audit_rows(spy)] == ["portal.mfa.verify.failure"]


@pytest.mark.asyncio
async def test_challenge_verify_failure_is_audited(mfa_on, monkeypatch):
    """A guessing campaign against the second factor is what the trail is for.

    The per-account failure budget that throttles it is a Redis rolling window
    — it forgets, so it is a brake, not evidence.
    """
    vu = _vendor_user(mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    spy = _audit_spy(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _challenge(vu, "000000", monkeypatch)

    assert exc.value.status_code == 401
    (row,) = _audit_rows(spy)
    assert row["action"] == "portal.mfa.verify.failure"
    assert row["details"] == {"method": "totp", "ip": "203.0.113.9"}
    assert "000000" not in repr(row)


@pytest.mark.asyncio
async def test_challenge_verify_records_which_factor_was_used(mfa_on, monkeypatch):
    """`method` is the whole reason issuing an email OTP needs no row of its own:
    the redemption row says a backup code was the factor that let someone in."""
    vu = _vendor_user(mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    monkeypatch.setattr(
        "app.api.portal_auth.mfa.verify_vendor_email_otp", AsyncMock(return_value=True)
    )
    spy = _audit_spy(monkeypatch)

    await _challenge(vu, "123456", monkeypatch, method="email")

    row = _audit_row(spy, "portal.mfa.verify.success")
    assert row["action"] == "portal.mfa.verify.success"
    assert row["details"]["method"] == "email"


@pytest.mark.asyncio
async def test_no_success_row_when_the_session_could_not_be_minted(mfa_on, monkeypatch):
    """The row goes on the trail only once the sign-in actually took effect.

    `_mint_portal_session` registers the session in Redis and lets its failures
    propagate; `dispatch_auth_audit` swallows its own. Auditing first would let
    a Redis blip leave a permanent, immutable row asserting a completed sign-in
    for a request that 500'd and handed the caller no token — the same reason
    the enrollment rows are written after their commit (decisions §111), and the
    order `api/auth.verify_mfa` already uses.
    """
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    monkeypatch.setattr(
        "app.api.portal_auth._mint_portal_session",
        AsyncMock(side_effect=RuntimeError("redis is down")),
    )
    spy = _audit_spy(monkeypatch)

    with pytest.raises(RuntimeError):
        await _challenge(vu, pyotp.TOTP(secret).now(), monkeypatch)

    assert _audit_rows(spy) == [], "the trail claimed a sign-in that never happened"


@pytest.mark.asyncio
async def test_the_audited_factor_is_the_branch_taken_not_the_caller_s_string(mfa_on, monkeypatch):
    """The second gate. `PortalMFAChallengeVerifyRequest.method` now pins
    `^(totp|email)$` (see the closed-vocabulary section at the foot of this
    file), so an unrecognised factor no longer reaches the handler over HTTP —
    but the route still derives the audited factor from the branch it actually
    took, and that must stay true independently of the schema. An audit trail
    is append-only and shipped to a WORM store, which makes it the worst
    possible place to echo unbounded caller-controlled text, and a row claiming
    a factor nobody used is worse than no row.

    Built with `model_construct` to bypass validation — the point is what the
    handler does on its own, not what the validator catches first.
    """
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    spy = _audit_spy(monkeypatch)

    # Verifies as TOTP (the fall-through), so it must be audited as TOTP.
    await _challenge(
        vu, pyotp.TOTP(secret).now(), monkeypatch, method="EMAIL</b>x", validated=False
    )

    row = _audit_row(spy, "portal.mfa.verify.success")
    assert row["details"]["method"] == "totp"
    assert "EMAIL</b>x" not in repr(row)
    # The sign-in row derives its method from the same branch, never the caller.
    assert "EMAIL</b>x" not in repr(_audit_row(spy, "portal.login.success"))


@pytest.mark.asyncio
async def test_a_legacy_vendor_user_with_no_org_writes_no_row(mfa_on, monkeypatch):
    """`dispatch_auth_audit` resolves the tenant DB from `organization_id`, so
    there is nowhere to route a row for a vendor user that predates it. Skipping
    is the existing `_audit_portal_mfa_event` contract — pinned here because
    sign-in is the one path where losing the row silently would matter most."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True, organization_id=None)
    spy = _audit_spy(monkeypatch)

    res = await _challenge(vu, pyotp.TOTP(secret).now(), monkeypatch)

    assert isinstance(res, PortalTokenResponse), "the sign-in itself must still succeed"
    assert _audit_rows(spy) == []


@pytest.mark.asyncio
async def test_requesting_an_email_otp_writes_no_audit_row(mfa_on, monkeypatch):
    """Issuing a backup code is deliberately unaudited — a pinned decision.

    Three reasons, none of them "not done yet". The auditable event is the code
    being REDEEMED, which `/mfa/challenge` records with its `method`. The send
    is already bounded by a per-IP and a per-account cap. And the endpoint is
    204-on-every-path precisely so it cannot be used to discover which supplier
    addresses exist and are enrolled — a row would be written for exactly that
    set, rebuilding the oracle inside the trail. Its employee twin
    (`api/auth.request_email_otp`) is unaudited on the same terms; auditing only
    the supplier surface would be a knowingly asymmetric control.
    """
    vu = _vendor_user(mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr("app.api.portal_auth._send_vendor_email_otp", AsyncMock())
    spy = _audit_spy(monkeypatch)

    await portal_request_email_otp(
        body=PortalMFAEmailChallengeRequest(
            challenge_token=mfa.create_vendor_challenge_token(vu.id)
        ),
        request=_fake_request(),
        db=_mock_db(vendor_user=vu),
    )

    assert _audit_rows(spy) == []


# ---------------------------------------------------------------------------
# Email-OTP backup factor — request a code, then verify it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_otp_request_sends_code_to_enrolled_vendor(mfa_on, monkeypatch):
    """An enrolled, active vendor with a valid challenge token gets a code
    issued + emailed to their account address."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    issue = AsyncMock(return_value="654321")
    monkeypatch.setattr("app.api.portal_auth.mfa.issue_vendor_email_otp", issue)
    send = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth._send_vendor_email_otp", send)

    challenge = mfa.create_vendor_challenge_token(vu.id)
    res = await portal_request_email_otp(
        body=PortalMFAEmailChallengeRequest(challenge_token=challenge),
        request=_fake_request(),
        db=db,
    )
    assert res is None  # 204
    issue.assert_awaited_once_with(vu.id)
    # The code is handed to the email helper — never returned in the response.
    send.assert_awaited_once()
    assert send.await_args.args[1] == "654321"


@pytest.mark.asyncio
async def test_email_otp_request_silent_for_unenrolled_vendor(mfa_on, monkeypatch):
    """A vendor who hasn't enrolled MFA gets no code — and a silent 204 (no
    enumeration of which accounts exist / are enrolled)."""
    vu = _vendor_user(mfa_enabled=False)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    issue = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth.mfa.issue_vendor_email_otp", issue)
    send = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth._send_vendor_email_otp", send)

    challenge = mfa.create_vendor_challenge_token(vu.id)
    res = await portal_request_email_otp(
        body=PortalMFAEmailChallengeRequest(challenge_token=challenge),
        request=_fake_request(),
        db=db,
    )
    assert res is None
    issue.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_otp_request_rejects_employee_challenge(mfa_on, monkeypatch):
    """The email-request endpoint enforces the same vendor-challenge typ gate —
    an employee challenge token is a 401."""
    db = _mock_db()
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    employee_challenge = mfa.create_challenge_token(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await portal_request_email_otp(
            body=PortalMFAEmailChallengeRequest(challenge_token=employee_challenge),
            request=_fake_request(),
            db=db,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_challenge_verify_email_method_mints_token(mfa_on, monkeypatch):
    """method='email' + a valid email OTP mints a real vendor access token."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    verify = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.portal_auth.mfa.verify_vendor_email_otp", verify)

    challenge = mfa.create_vendor_challenge_token(vu.id)
    res = await portal_mfa_challenge(
        body=PortalMFAChallengeVerifyRequest(
            challenge_token=challenge, code="654321", method="email"
        ),
        request=_fake_request(),
        slug="acme",
        db=db,
    )
    assert isinstance(res, PortalTokenResponse)
    verify.assert_awaited_once_with(vu.id, "654321")
    from app.api.deps import decode_token

    assert decode_token(res.access_token)["typ"] == "vendor"


@pytest.mark.asyncio
async def test_challenge_verify_email_wrong_or_expired_code_rejected(mfa_on, monkeypatch):
    """A bad / expired email OTP is a 401 — and never falls through to TOTP."""
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor_user=vu)
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.api.portal_auth.mfa.verify_vendor_email_otp", AsyncMock(return_value=False)
    )

    challenge = mfa.create_vendor_challenge_token(vu.id)
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_challenge(
            body=PortalMFAChallengeVerifyRequest(
                challenge_token=challenge, code="000000", method="email"
            ),
            request=_fake_request(),
            slug="acme",
            db=db,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_email_otp_keyspace_isolated_from_employee(monkeypatch):
    """The vendor email-OTP Redis key is a DISTINCT prefix from the employee one,
    so the same UUID value can't collide across surfaces."""

    class _FakeRedis:
        def __init__(self):
            self.store = {}

        async def setex(self, key, ttl, val):
            self.store[key] = val

        async def get(self, key):
            return self.store.get(key)

        async def delete(self, key):
            # Real Redis DEL returns the number of keys removed; the email-OTP
            # single-use claim reads it.
            return 0 if self.store.pop(key, None) is None else 1

    fake = _FakeRedis()
    monkeypatch.setattr("app.services.mfa.get_redis", AsyncMock(return_value=fake))

    same_id = uuid.uuid4()
    employee_code = await mfa.issue_email_otp(same_id)
    vendor_code = await mfa.issue_vendor_email_otp(same_id)
    keys = list(fake.store.keys())
    assert any(k.startswith(mfa.EMAIL_OTP_PREFIX) for k in keys)
    assert any(k.startswith(mfa.VENDOR_EMAIL_OTP_PREFIX) for k in keys)
    assert len(fake.store) == 2  # two separate slots, no overwrite

    # A vendor code must NOT verify against the employee keyspace and vice versa.
    assert await mfa.verify_vendor_email_otp(same_id, employee_code) is False
    assert await mfa.verify_email_otp(same_id, vendor_code) is False
    # Each verifies against its own slot, single-use.
    assert await mfa.verify_vendor_email_otp(same_id, vendor_code) is True
    assert await mfa.verify_vendor_email_otp(same_id, vendor_code) is False


# ---------------------------------------------------------------------------
# Disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_requires_valid_code(mfa_on):
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db(vendor=_vendor())

    code = pyotp.TOTP(secret).now()
    me = await portal_mfa_disable(body=PortalMFADisableRequest(code=code), vu=vu, db=db)
    assert vu.mfa_enabled is False
    assert vu.mfa_secret is None
    assert me.mfa_enabled is False


@pytest.mark.asyncio
async def test_disable_wrong_code_rejected(mfa_on):
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    db = _mock_db()
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_disable(body=PortalMFADisableRequest(code="000000"), vu=vu, db=db)
    assert exc.value.status_code == 401
    assert vu.mfa_enabled is True  # unchanged


# ---------------------------------------------------------------------------
# Token-type isolation — the cross-auth-leak guard
# ---------------------------------------------------------------------------


def test_vendor_challenge_token_distinct_typ():
    """The vendor challenge token must carry `vendor_mfa_challenge` — distinct
    from the employee `mfa_challenge` and the `vendor` access token."""
    vu_id = uuid.uuid4()
    from app.api.deps import decode_token

    tok = mfa.create_vendor_challenge_token(vu_id)
    assert decode_token(tok)["typ"] == "vendor_mfa_challenge"


@pytest.mark.asyncio
async def test_employee_challenge_token_not_accepted_as_vendor_challenge():
    """An employee MFA challenge token must NOT decode as a vendor challenge."""
    employee_challenge = mfa.create_challenge_token(uuid.uuid4())
    with pytest.raises(ValueError):
        await mfa.decode_vendor_challenge_token(employee_challenge)


@pytest.mark.asyncio
async def test_vendor_access_token_not_accepted_as_challenge():
    """A full vendor access token must NOT decode as a vendor MFA challenge."""
    access = create_vendor_access_token(uuid.uuid4(), uuid.uuid4())
    with pytest.raises(ValueError):
        await mfa.decode_vendor_challenge_token(access)


@pytest.mark.asyncio
async def test_challenge_endpoint_rejects_employee_challenge_token(mfa_on, monkeypatch):
    """Hitting the portal challenge endpoint with an EMPLOYEE challenge token
    is a 401 — the typ check blocks the cross-surface confusion."""
    db = _mock_db()
    monkeypatch.setattr("app.api.portal_auth.check_rate_limit", AsyncMock(return_value=None))
    employee_challenge = mfa.create_challenge_token(uuid.uuid4())
    code = "123456"
    with pytest.raises(HTTPException) as exc:
        await portal_mfa_challenge(
            body=PortalMFAChallengeVerifyRequest(challenge_token=employee_challenge, code=code),
            request=_fake_request(),
            slug="acme",
            db=db,
        )
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Step-up hardening — a credential check on a credential-management endpoint
# is a password oracle unless throttled, and a silent one unless audited.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_step_up_failure_is_audited(mfa_on, monkeypatch):
    """A wrong credential against a factor change leaves a PII-free trail."""
    audit = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", audit)
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True, hashed_password="hash")
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    with pytest.raises(HTTPException) as exc:
        await portal_mfa_enroll(body=PortalMFAStepUpRequest(password="guess"), vu=vu)

    # A wrong password gets the very refusal a missing proof does: one code,
    # so the code says nothing the English did not.
    assert exc.value.detail["code"] == "portal_step_up_failed"
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "portal.mfa.step_up.failure"
    assert kwargs["details"] == {"operation": "totp_enroll"}
    assert "guess" not in repr(kwargs), "the submitted credential must never be audited"


@pytest.mark.asyncio
async def test_portal_mfa_enrollment_success_is_audited(mfa_on, monkeypatch):
    """Adding a second factor to a supplier account writes `portal.mfa.enrolled`
    — mirrors `api/auth.py::enroll_mfa_verify`. PII-free."""
    audit = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", audit)
    vu = _vendor_user()
    db = _mock_db(vendor_user=None, vendor=_vendor())

    secret = pyotp.random_base32()
    await mfa.stash_pending_vendor_totp_secret(vu.id, secret)
    await portal_mfa_verify(
        body=PortalMFAVerifyRequest(code=pyotp.TOTP(secret).now()), vu=vu, db=db
    )

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "portal.mfa.enrolled"
    # `replaced` records whether this enrollment REPLACED a live factor —
    # the fact a "who swapped the supplier's second factor" investigation
    # turns on. PII-free either way: the factor kind and a boolean, never
    # the secret and never the supplier's address.
    assert kwargs["details"] == {"factor": "totp", "replaced": False}
    assert secret not in repr(kwargs)


@pytest.mark.asyncio
async def test_portal_step_up_is_rate_limited_per_account(mfa_on):
    """Per-VENDOR-USER, not per-IP: the attacker already holds their token."""
    from app.api.portal_auth import STEP_UP_RATE_LIMIT_PER_MINUTE

    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    statuses = []

    for _ in range(STEP_UP_RATE_LIMIT_PER_MINUTE + 2):
        try:
            await portal_mfa_enroll(body=PortalMFAStepUpRequest(code="000000"), vu=vu)
        except HTTPException as exc:
            statuses.append(exc.status_code)

    assert 429 in statuses, f"expected a 429 once over the cap, got {statuses}"
    assert vu.mfa_enabled is True
    assert vu.mfa_secret == secret


# ---------------------------------------------------------------------------
# `method` is a closed vocabulary, and both surfaces agree on it
#
# `PortalMFAChallengeVerifyRequest.method` was an unconstrained `str` while its
# employee twin (`schemas/auth.MFAVerifyRequest`) pinned `^(totp|email)$`. The
# route reads it as `"email" if body.method == "email" else "totp"`, so every
# unrecognised value — `sms`, a client typo, a trailing newline — fell through
# to TOTP and came back a *success*: the supplier asked to clear the challenge
# with a factor this app has never had, and got a session. The audit trail was
# never at risk (the row derives its factor from the branch actually taken,
# never from the request), but the two surfaces disagreed about what a factor
# name is, and a typo was invisible to the caller.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "sms",  # a factor this app has never had
        "totpx",  # a suffix typo
        "TOTP",  # right factor, wrong case — the route compares exactly
        "",  # empty string
        "totp email",  # both, which is not a thing
        "totp\n",  # the anchors must bind the WHOLE value, not a prefix line
        "email\n",
    ],
)
def test_portal_mfa_challenge_rejects_unknown_factor_at_schema_level(method):
    """An unrecognised factor name is rejected by Pydantic before the handler
    runs — not silently downgraded to TOTP."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PortalMFAChallengeVerifyRequest(challenge_token="x", code="123456", method=method)


def test_portal_mfa_challenge_accepts_both_real_factors():
    """The change is a tightening, not a break: both shipped factors still
    pass, and omitting `method` still defaults to TOTP. The field stays
    optional here (unlike the employee twin, which requires it), so a client
    sending only `{challenge_token, code}` is unaffected."""
    for method in ("totp", "email"):
        body = PortalMFAChallengeVerifyRequest(challenge_token="x", code="123456", method=method)
        assert body.method == method

    assert PortalMFAChallengeVerifyRequest(challenge_token="x", code="123456").method == "totp"


def test_portal_and_employee_mfa_verify_agree_on_the_factor_vocabulary():
    """Drift guard. Two surfaces verifying the same second factor must not
    disagree about what a factor is called — that disagreement is the bug this
    section closes, and it is one edit away from returning."""
    from app.schemas.auth import MFAVerifyRequest

    def _pattern(model, field):
        return [
            item.pattern
            for item in model.model_fields[field].metadata
            if getattr(item, "pattern", None) is not None
        ]

    portal = _pattern(PortalMFAChallengeVerifyRequest, "method")
    employee = _pattern(MFAVerifyRequest, "method")
    assert portal == ["^(totp|email)$"], portal
    assert portal == employee, (portal, employee)


@pytest.mark.asyncio
async def test_portal_mfa_challenge_endpoint_422s_on_unknown_factor(mfa_on):
    """End-to-end through the ASGI app: `method: "sms"` is refused at the
    request boundary and the handler never runs, while the two real factors get
    past validation INTO the handler — which then 401s on the junk challenge
    token. The 401 is the point: it is the handler's refusal, not the
    validator's, so `totp`/`email` provably still reach the code."""
    import httpx

    from app.main import app
    from app.tenant import get_tenant_db, get_tenant_slug

    async def _slug() -> str:
        return "acme"

    async def _db():
        return MagicMock()

    app.dependency_overrides[get_tenant_slug] = _slug
    app.dependency_overrides[get_tenant_db] = _db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {"challenge_token": "not-a-real-token", "code": "123456"}

            bad = await client.post(
                "/api/portal/auth/mfa/challenge", json={**payload, "method": "sms"}
            )
            assert bad.status_code == 422, bad.text
            # The error names the offending field, so a client typo is visible
            # instead of quietly verifying a different factor. `method` is a
            # closed factor name, never a credential — the response echoes
            # neither the submitted code nor the challenge token.
            locs = [tuple(err["loc"]) for err in bad.json()["detail"]]
            assert ("body", "method") in locs, bad.text
            assert "not-a-real-token" not in bad.text
            assert "123456" not in bad.text

            for method in ("totp", "email"):
                ok = await client.post(
                    "/api/portal/auth/mfa/challenge", json={**payload, "method": method}
                )
                assert ok.status_code == 401, (method, ok.status_code, ok.text)
    finally:
        app.dependency_overrides.pop(get_tenant_slug, None)
        app.dependency_overrides.pop(get_tenant_db, None)
