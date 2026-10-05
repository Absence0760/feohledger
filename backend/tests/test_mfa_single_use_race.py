"""Single-use MFA and SSO secrets stay single-use under concurrency.

Every one of these was "single-use" only for requests that happened not to
overlap. Each was a check in one Redis round trip and a consume in another:

- the email OTP (employee and supplier portal): GET + compare, then an
  unconditional DELETE and ``return True`` — two requests carrying the same
  correct code both passed the compare and both were told it verified;
- the MFA challenge token: ``decode_challenge_token`` checks the blocklist
  BEFORE the factor is verified and ``consume_challenge_token`` wrote the
  blocklist entry with a plain SETEX, so two requests redeeming one challenge
  with two different valid factors (a TOTP code and an email OTP) both minted
  an access token from a single password check;
- the OIDC ``state``, the SAML RelayState and the SAML token handoff: GET then
  DELETE, where the RelayState is the binding that makes an assertion answer
  exactly one AuthnRequest.

The fake Redis here yields to the event loop on every command, exactly as a
network round trip does, so ``asyncio.gather`` interleaves the two requests the
way two concurrent HTTP requests would. With a non-yielding fake the race can't
be observed — which is how the old code passed its single-use tests.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from fastapi import HTTPException

from app.services import mfa, sso

pytestmark = pytest.mark.asyncio


class _YieldingRedis:
    """Key/value Redis stand-in whose every command suspends once, like I/O.

    Each command's own effect is applied atomically after the suspension —
    which is what real Redis guarantees per command, and nothing more.
    """

    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}

    async def setex(self, key, ttl, value):  # noqa: ARG002
        await asyncio.sleep(0)
        self.kv[key] = value.encode() if isinstance(value, str) else value
        return True

    async def set(self, key, value, nx=False, ex=None):  # noqa: ARG002
        await asyncio.sleep(0)
        if nx and key in self.kv:
            return None
        self.kv[key] = value.encode() if isinstance(value, str) else value
        return True

    async def get(self, key):
        await asyncio.sleep(0)
        return self.kv.get(key)

    async def getdel(self, key):
        await asyncio.sleep(0)
        return self.kv.pop(key, None)

    async def exists(self, key):
        await asyncio.sleep(0)
        return 1 if key in self.kv else 0

    async def delete(self, *keys):
        await asyncio.sleep(0)
        return sum(1 for k in keys if self.kv.pop(k, None) is not None)


@pytest.fixture
def yielding_redis(monkeypatch):
    fake = _YieldingRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.mfa.get_redis", _get_redis)
    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    monkeypatch.setattr("app.services.sso.get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# Email OTP
# ---------------------------------------------------------------------------


async def test_one_email_otp_verifies_once_under_concurrent_redemption(yielding_redis):
    user_id = uuid.uuid4()
    code = await mfa.issue_email_otp(user_id)

    results = await asyncio.gather(
        mfa.verify_email_otp(user_id, code), mfa.verify_email_otp(user_id, code)
    )

    assert sorted(results) == [False, True]


async def test_one_vendor_email_otp_verifies_once_under_concurrent_redemption(yielding_redis):
    vendor_user_id = uuid.uuid4()
    code = await mfa.issue_vendor_email_otp(vendor_user_id)

    results = await asyncio.gather(
        mfa.verify_vendor_email_otp(vendor_user_id, code),
        mfa.verify_vendor_email_otp(vendor_user_id, code),
    )

    assert sorted(results) == [False, True]


async def test_a_wrong_guess_does_not_burn_the_code(yielding_redis):
    """The claim happens only after the compare — so a concurrent wrong guess
    can't consume the real code out from under its owner."""
    user_id = uuid.uuid4()
    code = await mfa.issue_email_otp(user_id)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"

    assert await mfa.verify_email_otp(user_id, wrong) is False
    assert await mfa.verify_email_otp(user_id, code) is True


# ---------------------------------------------------------------------------
# MFA challenge token
# ---------------------------------------------------------------------------


async def test_a_challenge_token_is_consumed_exactly_once(yielding_redis):
    jti = str(uuid.uuid4())

    results = await asyncio.gather(
        mfa.consume_challenge_token(jti), mfa.consume_challenge_token(jti)
    )

    assert sorted(results) == [False, True]


def _user(secret: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="race@acme.test",
        organization_id=uuid.uuid4(),
        is_active=True,
        must_change_password=False,
        mfa_enabled=True,
        mfa_secret=secret,
    )


def _db_returning(user) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _request() -> MagicMock:
    req = MagicMock()
    req.client = SimpleNamespace(host="10.0.0.9")
    req.headers = {}
    return req


async def test_one_challenge_redeemed_with_two_factors_mints_one_session(yielding_redis):
    """The attack shape: one password check, then the challenge token is sent
    twice at once — once with the authenticator code, once with an emailed
    backup code. Each factor is individually valid and individually single-use,
    so only the challenge's own single-use claim can stop the second session."""
    from app.api import auth as auth_mod
    from app.schemas.auth import MFAVerifyRequest

    secret = pyotp.random_base32()
    user = _user(secret)
    challenge = mfa.create_challenge_token(user.id)
    email_code = await mfa.issue_email_otp(user.id)
    totp_code = pyotp.TOTP(secret).now()
    sessions: list[str] = []

    async def _register(user_id, jti, **kwargs):  # noqa: ARG001
        sessions.append(jti)

    async def _noop(*args, **kwargs):  # noqa: ARG001
        return None

    async def _redeem(method: str, code: str):
        try:
            return await auth_mod.verify_mfa(
                MFAVerifyRequest(challenge_token=challenge, code=code, method=method),
                _request(),
                _db_returning(user),
            )
        except HTTPException as exc:
            return exc

    with (
        patch.object(auth_mod.settings, "mfa_enabled", True),
        patch.object(auth_mod, "check_rate_limit", _noop),
        patch.object(auth_mod, "check_auth_failures", _noop),
        patch.object(auth_mod, "record_auth_failure", _noop),
        patch.object(auth_mod, "clear_auth_failures", _noop),
        patch.object(auth_mod, "dispatch_auth_audit", _noop),
        patch.object(auth_mod, "register_session", _register),
    ):
        results = await asyncio.gather(_redeem("totp", totp_code), _redeem("email", email_code))

    minted = [r for r in results if not isinstance(r, HTTPException)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(minted) == 1
    assert len(sessions) == 1
    assert len(refused) == 1 and refused[0].status_code == 401


# ---------------------------------------------------------------------------
# SSO state, SAML RelayState, SAML handoff
# ---------------------------------------------------------------------------


async def _race(consume, key):
    async def _one():
        try:
            return await consume(key)
        except sso.SSOValidationError:
            return None

    return await asyncio.gather(_one(), _one())


async def test_oidc_state_is_consumed_once_under_concurrency(yielding_redis):
    state, _nonce = await sso.create_state("acme")

    results = await _race(sso.consume_state, state)

    assert sum(r is not None for r in results) == 1


async def test_saml_relay_state_is_consumed_once_under_concurrency(yielding_redis):
    await sso.store_saml_relay_state("rs-1", "acme", "_req-1")

    results = await _race(sso.consume_saml_relay_state, "rs-1")

    assert sum(r is not None for r in results) == 1


async def test_saml_handoff_is_consumed_once_under_concurrency(yielding_redis):
    code = await sso.create_saml_handoff("jwt", False, "acme")

    results = await _race(sso.consume_saml_handoff, code)

    assert sum(r is not None for r in results) == 1
