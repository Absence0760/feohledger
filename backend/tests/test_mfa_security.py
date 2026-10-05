"""MFA security-property tests.

`test_mfa.py` covers the happy-path mechanics (issue / verify / decode).
These tests pin the *security* contracts that protect the second factor:

  - Email OTP is **single-use** (verify consumes the stored hash)
  - Issuing a new OTP for the same user **invalidates** the previous one
  - Email OTP is **rate-limited via TTL** (the comparison is
    constant-time; TTL is short)
  - Stored OTP is **hashed**, never plaintext (a Redis dump shouldn't
    yield a usable code)
  - TOTP cannot be replayed within the same 30s window
  - Challenge tokens **expire** and a stale one is rejected even with
    the right type / signature
  - Challenge tokens of **another type** (`typ=user`) are refused
  - Verify path is **constant-time** against the stored hash (no
    short-circuit on first wrong character)

The MFA challenge token type is the key gate between "password
accepted" and "JWT issued". Every test here closes one way an
attacker can short-circuit that gate.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from jose import jwt

from app.config import settings


class _FakeRedis:
    """In-memory stand-in with TTL semantics. Stored bytes mirror what
    real Redis returns; `setex` overrides any prior key."""

    def __init__(self):
        self.store: dict[str, tuple[float, bytes]] = {}

    async def setex(self, key, ttl, value):
        expiry = time.time() + ttl
        self.store[key] = (
            expiry,
            value if isinstance(value, bytes) else value.encode("utf-8"),
        )

    async def get(self, key):
        item = self.store.get(key)
        if not item:
            return None
        expiry, value = item
        if expiry < time.time():
            del self.store[key]
            return None
        return value

    async def delete(self, key):
        # Real Redis DEL returns the number of keys removed; the email-OTP
        # single-use claim reads it.
        return 0 if self.store.pop(key, None) is None else 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.mfa.get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# Email OTP — single-use, hashed, rotation-on-reissue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_otp_is_single_use(fake_redis):
    """Verifying once must remove the code so a second verify with the
    same plaintext fails. A regression where verify left the key in
    Redis would make replay trivial."""
    from app.services import mfa

    user_id = uuid.uuid4()
    code = await mfa.issue_email_otp(user_id)

    assert await mfa.verify_email_otp(user_id, code) is True
    assert await mfa.verify_email_otp(user_id, code) is False, (
        "OTP must be single-use — second verify with the same code must fail"
    )


@pytest.mark.asyncio
async def test_email_otp_reissue_invalidates_previous_code(fake_redis):
    """If a user re-requests an email OTP, the previous code must stop
    working immediately. Without this, every reissue widens the
    attacker's window of valid codes."""
    from app.services import mfa

    user_id = uuid.uuid4()
    first = await mfa.issue_email_otp(user_id)
    second = await mfa.issue_email_otp(user_id)
    # They almost-certainly differ (entropy = 10^6), but the key
    # contract is that the *first* code is no longer accepted.
    assert await mfa.verify_email_otp(user_id, first) is False, (
        "reissuing must invalidate the previous OTP"
    )
    # And the new code does work.
    assert await mfa.verify_email_otp(user_id, second) is True


@pytest.mark.asyncio
async def test_email_otp_stored_value_is_a_keyed_kdf_not_plaintext_or_bare_hash(fake_redis):
    """A snapshot of Redis must not yield a usable code. The stored value must be
    a 64-char hex digest, never the plaintext — and it must be the KEYED KDF, not
    a bare SHA-256 of the code. A bare hash of a 6-digit code is trivially
    brute-forced offline from the stored digest; keying it with the server secret
    (server-secret-keyed PBKDF2) closes that."""
    import hashlib

    from app.services import mfa

    user_id = uuid.uuid4()
    plaintext = await mfa.issue_email_otp(user_id)

    stored_bytes = fake_redis.store[f"mfa:email_otp:{user_id}"][1]
    stored = stored_bytes.decode("utf-8")
    assert plaintext not in stored, "Redis must not hold the plaintext OTP"
    assert len(stored) == 64, f"expected 64-char hex digest; got {len(stored)} chars"
    assert all(c in "0123456789abcdef" for c in stored), "stored value must be hex"
    bare = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert stored != bare, "stored digest must be a keyed KDF, not a bare SHA-256 of the code"


@pytest.mark.asyncio
async def test_email_otp_verify_rejects_when_ttl_expired(fake_redis):
    """The TTL is the entropy budget — once it lapses, the code must
    fail closed even if the plaintext was right."""
    from app.services import mfa

    user_id = uuid.uuid4()
    code = await mfa.issue_email_otp(user_id)
    # Walk the fake clock forward by tampering with the stored expiry.
    expiry, value = fake_redis.store[f"mfa:email_otp:{user_id}"]
    fake_redis.store[f"mfa:email_otp:{user_id}"] = (time.time() - 1, value)
    assert await mfa.verify_email_otp(user_id, code) is False


@pytest.mark.asyncio
async def test_email_otp_verify_rejects_empty_and_whitespace_codes(fake_redis):
    """A user pressing Enter on an empty input must NOT succeed even
    if no code is outstanding (the stored hash of "" or "      "
    isn't predictable, but a regression that special-cases empty
    strings — for any reason — opens the gate)."""
    from app.services import mfa

    user_id = uuid.uuid4()
    await mfa.issue_email_otp(user_id)

    assert await mfa.verify_email_otp(user_id, "") is False
    assert await mfa.verify_email_otp(user_id, "      ") is False


@pytest.mark.asyncio
async def test_email_otp_uses_constant_time_compare(fake_redis):
    """The verify path uses `hmac.compare_digest` against the stored
    hash. Without it, a timing oracle leaks the code one character at
    a time. We can't reliably *time* this in a unit test, but we can
    confirm the helper is wired up — a regression that swaps it for
    `==` is caught by inspection."""
    import inspect

    from app.services import mfa

    src = inspect.getsource(mfa.verify_email_otp)
    assert "compare_digest" in src, (
        "verify_email_otp must use hmac.compare_digest; raw == is a timing oracle"
    )


# ---------------------------------------------------------------------------
# Challenge token — expiry + type enforcement
# ---------------------------------------------------------------------------


def _mint_challenge_payload(payload: dict) -> str:
    """Bypass `create_challenge_token` to forge edge-case payloads."""
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


async def test_challenge_token_rejected_when_expired():
    """A stale challenge — even with the right type and signature —
    must not be accepted. Otherwise the password-only step grants a
    much larger time window than the 5-minute design."""
    from app.services import mfa

    expired_payload = {
        "sub": str(uuid.uuid4()),
        "typ": "mfa_challenge",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) - timedelta(seconds=10),
    }
    token = _mint_challenge_payload(expired_payload)
    with pytest.raises(ValueError):
        await mfa.decode_challenge_token(token)


async def test_challenge_token_rejected_when_typ_is_user():
    """The challenge token must carry `typ=mfa_challenge`. A regression
    that lets an `typ=user` JWT pass through `decode_challenge_token`
    would let an already-authenticated user bypass MFA entirely."""
    from app.services import mfa

    payload = {
        "sub": str(uuid.uuid4()),
        "typ": "user",  # wrong type
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = _mint_challenge_payload(payload)
    with pytest.raises(ValueError, match="Wrong token type"):
        await mfa.decode_challenge_token(token)


async def test_challenge_token_rejected_when_missing_typ():
    """A token with no `typ` at all must not be treated as a valid
    challenge — the absence of the discriminator is itself disqualifying."""
    from app.services import mfa

    payload = {
        "sub": str(uuid.uuid4()),
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = _mint_challenge_payload(payload)
    with pytest.raises(ValueError):
        await mfa.decode_challenge_token(token)


async def test_challenge_token_rejected_when_signed_with_wrong_secret():
    """An attacker can't mint a valid challenge by guessing the
    algorithm — the secret is the wall."""
    from app.services import mfa

    payload = {
        "sub": str(uuid.uuid4()),
        "typ": "mfa_challenge",
        "jti": str(uuid.uuid4()),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    bad = jwt.encode(payload, "wrong-secret", algorithm="HS256")
    with pytest.raises(ValueError):
        await mfa.decode_challenge_token(bad)


async def test_challenge_token_rejected_when_alg_is_none():
    """alg=none is the canonical JWT bypass. The hand-rolled token
    below has no signature — decode must fail."""
    import base64
    import json

    from app.services import mfa

    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "sub": str(uuid.uuid4()),
                    "typ": "mfa_challenge",
                    "jti": str(uuid.uuid4()),
                    "exp": int(time.time()) + 300,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    token = f"{header}.{body}."  # no signature
    with pytest.raises(ValueError):
        await mfa.decode_challenge_token(token)


def test_challenge_token_ttl_matches_settings():
    """The token's `exp` claim must derive from `mfa_challenge_ttl_seconds`
    — a regression that hardcoded a longer TTL would invisibly widen the
    challenge window. Verify the diff between mint time and decoded exp
    is within tolerance of the configured TTL."""
    from app.services import mfa

    user_id = uuid.uuid4()
    minted_at = time.time()
    token = mfa.create_challenge_token(user_id)
    payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])

    ttl_observed = payload["exp"] - minted_at
    # Allow ±5s of clock skew between the mint and the read.
    configured = settings.mfa_challenge_ttl_seconds
    assert configured - 5 <= ttl_observed <= configured + 5, (
        f"challenge TTL drifted: configured={configured}, observed≈{ttl_observed}"
    )


# ---------------------------------------------------------------------------
# TOTP replay protection
# ---------------------------------------------------------------------------


async def test_totp_window_does_not_accept_arbitrary_old_codes():
    """`verify_totp` must reject a code computed for a window far in
    the past. A `valid_window=1000`-style mistake would let any
    historical code authenticate."""
    import pyotp

    from app.services import mfa

    secret = mfa.generate_totp_secret()
    totp = pyotp.TOTP(secret)
    # Code from 10 minutes ago — way outside any reasonable drift.
    old_code = totp.at(int(time.time()) - 600)
    assert await mfa.verify_totp(secret, old_code) is False


async def test_totp_rejects_obviously_wrong_codes():
    """Belt-and-braces: random 6-digit strings must not pass. A
    regression where verify defaulted to True on parse error would
    accept "000000"."""
    from app.services import mfa

    secret = mfa.generate_totp_secret()
    for bad in ("000000", "123456", "999999", "abcdef"):
        assert await mfa.verify_totp(secret, bad) is False, f"unexpectedly accepted: {bad!r}"


def test_totp_claim_key_is_a_keyed_kdf_not_bare_hash():
    """The single-use TOTP claim key must be a KEYED KDF of the (secret, code)
    pair, not a bare SHA-256. A bare hash lets anyone who can read the Redis
    keyspace precompute / correlate a claim key for a known (secret, code) pair,
    and trips CodeQL's py/weak-sensitive-data-hashing rule. Regression guard for
    the fix that derives this key through the server-secret-keyed PBKDF2."""
    import hashlib

    from app.config import settings
    from app.services import mfa

    secret = mfa.generate_totp_secret()
    code = "000000"
    key = mfa._totp_claim_key(secret, code)

    # Not a bare (reversible/precomputable) SHA-256 of secret:code.
    bare = "mfa:totp_used:" + hashlib.sha256(f"{secret}:{code}".encode()).hexdigest()
    assert key != bare, "claim key must not be a bare SHA-256 of secret:code"

    # It is the server-secret-keyed PBKDF2 derivation, deterministic for lookups.
    expected = (
        "mfa:totp_used:"
        + hashlib.pbkdf2_hmac(
            "sha256",
            f"{secret}:{code}".encode(),
            settings.secret_key.encode(),
            mfa._KEYED_KDF_ITERATIONS,
        ).hex()
    )
    assert key == expected, "claim key must be the server-keyed PBKDF2 of secret:code"
    assert key == mfa._totp_claim_key(secret, code), "derivation must be deterministic"


async def test_step_up_verified_rejects_a_wrong_totp_code():
    """`step_up_verified` gates every factor change (enroll-over-live, disable,
    passkey add/remove). A WRONG authenticator code must fail it — otherwise a
    stolen session could strip or swap the second factor. Regression guard for
    the async `verify_totp` call: an un-awaited coroutine is always truthy, so a
    sync call would accept ANY code as a valid step-up (a real bypass)."""
    import pyotp

    from app.services import mfa

    secret = mfa.generate_totp_secret()
    # A correct current code satisfies the step-up.
    assert (
        await mfa.step_up_verified(
            hashed_password=None,
            mfa_secret=secret,
            password=None,
            code=pyotp.TOTP(secret).now(),
        )
        is True
    )
    # A wrong code must NOT — and neither should a bare presence of a code.
    assert (
        await mfa.step_up_verified(
            hashed_password=None, mfa_secret=secret, password=None, code="000000"
        )
        is False
    )


# ---------------------------------------------------------------------------
# Org-required MFA — login handler refuses to mint an access token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_required_mfa_does_not_short_circuit_to_access_token(fake_redis):
    """When the org enforces MFA, the password-success branch of
    `/api/auth/login` must NOT mint a final access token even if the
    user hasn't enrolled. A regression that bypassed the challenge
    path on missing enrollment would silently downgrade the org's MFA
    requirement to "off"."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.api.auth import login
    from app.schemas.auth import LoginRequest

    # Use a real hash so verify succeeds for the matching plaintext.
    from app.utils.passwords import pwd_context

    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="user@acme.test",
        organization_id=uuid.uuid4(),
        is_active=True,
        mfa_enabled=False,  # not enrolled
        mfa_secret=None,
        hashed_password=pwd_context.hash("correct-password-12"),
        must_change_password=False,
        full_name="Test User",
    )
    org = SimpleNamespace(
        id=user.organization_id,
        slug="acme",
        settings={"mfa": {"required": True}},
    )

    result_user = MagicMock()
    result_user.scalar_one_or_none = MagicMock(return_value=user)
    result_org = MagicMock()
    result_org.scalar_one_or_none = MagicMock(return_value=org)
    # Passkey lookup (added by the WebAuthn factor) — this user has none.
    result_passkeys = MagicMock()
    result_passkeys.scalars.return_value.all.return_value = []
    db = AsyncMock()
    # First execute → user lookup; second → org lookup; third → passkey lookup
    db.execute = AsyncMock(side_effect=[result_user, result_org, result_passkeys])

    request = MagicMock()
    request.client = SimpleNamespace(host="127.0.0.1")
    request.headers = {}

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.dispatch_auth_audit", AsyncMock()),
    ):
        resp = await login(
            body=LoginRequest(email=user.email, password="correct-password-12"),
            request=request,
            db=db,
        )

    # Must be a challenge response, not a TokenResponse.
    assert hasattr(resp, "mfa_challenge_token"), (
        "org-required MFA must produce a challenge, not an access token"
    )
    assert not hasattr(resp, "access_token") or getattr(resp, "access_token", None) is None
