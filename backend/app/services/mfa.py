"""MFA helpers — TOTP enrollment, verification, email-OTP backup, challenge tokens.

Two factors are supported:

- **TOTP** (primary). Per-user shared secret stored on `User.mfa_secret`. Apps
  like Google Authenticator / 1Password / Authy scan the provisioning URI. A
  *candidate* secret from an in-flight enrollment is NOT written to the
  account — it waits in Redis (`stash_pending_totp_secret`) until the user
  proves they hold it, so starting an enrollment never disturbs the factor
  already in force.
- **Email OTP** (backup). Sent on demand to the account email when the user
  can't access their authenticator. The 6-digit code lives in Redis with a
  short TTL — no DB column needed.

The post-password "still need MFA" challenge is itself a short-lived JWT.
That keeps the login flow stateless: backend hands the browser a token,
browser submits TOTP/email-OTP with it, backend verifies + mints a real
access token. No DB row to garbage-collect.

Org-level enforcement is read from `Organization.settings.mfa.required`. When
true, users without enrolled MFA can still log in once but are gated until
they enroll (handled in the auth router).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyotp
import qrcode
from jose import JWTError, jwt

from app.config import settings
from app.redis import block_token, get_redis, is_token_blocked
from app.utils.passwords import verify_password

ALGORITHM = "HS256"

EMAIL_OTP_PREFIX = "mfa:email_otp:"
# Pending (started-but-not-yet-verified) TOTP enrollment secrets. Separate
# keyspaces per surface, same reason as the email-OTP prefixes below: an
# employee user id and a vendor user id must never share a slot.
PENDING_ENROLL_PREFIX = "mfa:pending_enroll:"
VENDOR_PENDING_ENROLL_PREFIX = "mfa:vendor_pending_enroll:"
# Supplier-portal email-OTP backup. A DISTINCT Redis keyspace from the employee
# one (`mfa:email_otp:`) so a vendor user id can never collide with an employee
# user id of the same UUID value — the same isolation principle as the distinct
# `vendor_mfa_challenge` token type.
VENDOR_EMAIL_OTP_PREFIX = "mfa:vendor_email_otp:"
CHALLENGE_TYPE = "mfa_challenge"
# Supplier-portal MFA challenge. A DISTINCT `typ` from both the employee
# challenge (`mfa_challenge`) and the vendor access token (`vendor`), so a
# vendor MFA challenge token can never resolve as an access token through
# `get_current_vendor_user`, nor be confused with the employee flow.
VENDOR_CHALLENGE_TYPE = "vendor_mfa_challenge"


# ---------------------------------------------------------------------------
# Org enforcement
# ---------------------------------------------------------------------------


def org_requires_mfa(org_settings: dict | None) -> bool:
    """True when the org has flipped on tenant-wide MFA enforcement."""
    if not settings.mfa_enabled:
        return False
    if not org_settings:
        return False
    mfa = org_settings.get("mfa") or {}
    return bool(mfa.get("required"))


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """Mint a fresh base32 TOTP secret. 160 bits of entropy = RFC 6238 default."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_label: str, issuer: str | None = None) -> str:
    """Build the otpauth:// URI authenticator apps consume from a QR code."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_label,
        issuer_name=issuer or settings.mfa_issuer,
    )


def qr_code_data_url(uri: str) -> str:
    """Render a QR code for the provisioning URI as a base64 data URL.

    Returning a data URL keeps the frontend dead simple — no separate image
    endpoint, no auth header juggling for an <img src>. The QR is small
    (~1KB) so the inline cost is negligible.
    """
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


# TTL for the single-use TOTP-code claim below. valid_window=1 accepts the
# current period plus one on either side, so a code can be valid for close to
# 90s around the moment it's generated — the claim TTL is set to that same
# upper bound so it can't outlive the code's own validity window (a shorter
# TTL would let a captured code be replayed again once the claim expires but
# the code itself is still accepted).
_TOTP_CLAIM_TTL_SECONDS = 90


# Work factor for the keyed KDF below. This is NOT password storage: security
# rests on the secret salt (a Redis-keyspace snapshot can't derive or precompute
# a key without FEOH_SECRET_KEY), so the iteration count is not a brute-force
# defense here — it's simply the computationally-expensive KDF that CodeQL's
# py/weak-sensitive-data-hashing rule requires for hashing sensitive data. Kept
# modest so the login-path derivation stays fast.
_KEYED_KDF_ITERATIONS = 100_000


def _keyed_digest(*parts: str) -> str:
    """PBKDF2-HMAC-SHA256 of the ``:``-joined parts, keyed by the server secret
    used as a fixed salt (fixed so the derivation stays deterministic for an O(1)
    Redis lookup). The secret salt is what protects it: a Redis-keyspace snapshot
    can neither reverse a key to its (secret, code) nor precompute one without
    FEOH_SECRET_KEY. PBKDF2 — not a bare or HMAC SHA-256 — is the KDF CodeQL's
    py/weak-sensitive-data-hashing rule accepts for sensitive data. 64-char hex,
    the same shape as a SHA-256 digest, so stored formats are unchanged."""
    msg = ":".join(parts).encode()
    return hashlib.pbkdf2_hmac(
        "sha256", msg, settings.secret_key.encode(), _KEYED_KDF_ITERATIONS
    ).hex()


def _totp_claim_key(secret: str, code: str) -> str:
    # Keyed KDF (not a bare hash): the TOTP secret/code never appear in
    # cleartext in Redis keyspace listings / slow logs, and without the server
    # key an attacker who can read the keyspace can't correlate or precompute a
    # claim key for a (secret, code) pair.
    return f"mfa:totp_used:{_keyed_digest(secret, code)}"


async def verify_totp(secret: str, code: str) -> bool:
    """Verify + consume a 6-digit TOTP. Allows ±1 30-second window for clock
    skew. Single-use: the first successful verification of a given (secret,
    code) pair claims it in Redis, so the SAME code can't be replayed again
    within its validity window — mirrors verify_email_otp's single-use
    pattern (issue #162). A brand-new code every ~30s is unaffected; only an
    exact repeat of an already-accepted code is rejected.
    """
    if not secret or not code:
        return False
    code = code.strip()
    try:
        if not pyotp.TOTP(secret).verify(code, valid_window=1):
            return False
    except Exception:
        return False

    r = await get_redis()
    # SET NX: the first caller to claim this (secret, code) pair wins; a
    # replay within the TTL finds the key already set and is rejected.
    claimed = await r.set(_totp_claim_key(secret, code), "1", nx=True, ex=_TOTP_CLAIM_TTL_SECONDS)
    return bool(claimed)


# ---------------------------------------------------------------------------
# Pending enrollment secret — the live factor is never disturbed
#
# TOTP enrollment is a two-step ceremony (mint a secret + QR, then prove you
# scanned it). Writing the candidate secret onto the account at step one meant
# merely *starting* an enrollment stripped whatever second factor was already
# in force — a leaked access token was enough to silently downgrade an account
# to single-factor. The candidate now waits here instead, under a short TTL,
# next to the WebAuthn ceremony challenge; only `/mfa/enroll/verify` promotes
# it onto the account.
#
# The value is the raw base32 seed (it has to be, to verify a code against it)
# — the same material `users.mfa_secret` already holds, just for minutes
# rather than years. It is never logged and never returned outside the
# enrollment-start response.
# ---------------------------------------------------------------------------


def _pending_enroll_key(prefix: str, subject_id: uuid.UUID) -> str:
    return f"{prefix}{subject_id}"


async def _stash_pending_secret(prefix: str, subject_id: uuid.UUID, secret: str) -> None:
    r = await get_redis()
    await r.setex(
        _pending_enroll_key(prefix, subject_id),
        settings.mfa_enroll_pending_ttl_seconds,
        secret,
    )


async def _read_pending_secret(prefix: str, subject_id: uuid.UUID) -> str | None:
    r = await get_redis()
    stored = await r.get(_pending_enroll_key(prefix, subject_id))
    if not stored:
        return None
    return stored.decode("utf-8") if isinstance(stored, bytes) else stored


async def _clear_pending_secret(prefix: str, subject_id: uuid.UUID) -> None:
    r = await get_redis()
    await r.delete(_pending_enroll_key(prefix, subject_id))


async def stash_pending_totp_secret(user_id: uuid.UUID, secret: str) -> None:
    """Hold a candidate enrollment secret for an employee user. Replaces any
    previous outstanding candidate — one enrollment in flight per user."""
    await _stash_pending_secret(PENDING_ENROLL_PREFIX, user_id, secret)


async def read_pending_totp_secret(user_id: uuid.UUID) -> str | None:
    """The employee user's in-flight candidate secret, or None if enrollment
    was never started (or has expired)."""
    return await _read_pending_secret(PENDING_ENROLL_PREFIX, user_id)


async def clear_pending_totp_secret(user_id: uuid.UUID) -> None:
    """Drop the employee candidate — called once it's promoted onto the account."""
    await _clear_pending_secret(PENDING_ENROLL_PREFIX, user_id)


async def stash_pending_vendor_totp_secret(vendor_user_id: uuid.UUID, secret: str) -> None:
    """Supplier-portal counterpart of `stash_pending_totp_secret`."""
    await _stash_pending_secret(VENDOR_PENDING_ENROLL_PREFIX, vendor_user_id, secret)


async def read_pending_vendor_totp_secret(vendor_user_id: uuid.UUID) -> str | None:
    """Supplier-portal counterpart of `read_pending_totp_secret`."""
    return await _read_pending_secret(VENDOR_PENDING_ENROLL_PREFIX, vendor_user_id)


async def clear_pending_vendor_totp_secret(vendor_user_id: uuid.UUID) -> None:
    """Supplier-portal counterpart of `clear_pending_totp_secret`."""
    await _clear_pending_secret(VENDOR_PENDING_ENROLL_PREFIX, vendor_user_id)


# ---------------------------------------------------------------------------
# Step-up re-authentication
# ---------------------------------------------------------------------------


async def step_up_verified(
    *,
    hashed_password: str | None,
    mfa_secret: str | None,
    password: str | None,
    code: str | None,
) -> bool:
    """Did the caller re-prove control of the account with a *stateless* proof?

    Used before an account's *existing* second factor can be added to, replaced
    or removed (TOTP re-enrollment, registering a passkey, deleting a passkey).
    Either credential satisfies it: the account password — the same check
    `/mfa/disable` makes — or a code from the authenticator currently enrolled.
    Both are things a bearer token alone does not grant, which is the point: an
    attacker holding a stolen session must not be able to swap the second
    factor out from under the owner.

    There is deliberately **no "nothing to challenge" escape hatch** here: an
    account with neither a password nor a TOTP secret fails this, and that is
    the safe answer — exempting it would let a stolen JWT plant an
    attacker-controlled passkey on an account the attacker never proved control
    of.

    A third proof exists on the employee surface and is NOT checked here: a
    **WebAuthn assertion** from an already-registered passkey
    (`api/auth._step_up_satisfied` -> `services/webauthn.finish_authentication`
    with `purpose=step_up`). It lives at the route layer because verifying it
    needs the DB (to resolve the credential row) and Redis (the single-use,
    purpose-bound challenge), which this pure helper deliberately doesn't
    touch. That third proof is what lets a passwordless SSO-only account whose
    sole factor is a passkey manage its own factors; before it existed, such an
    account was refused outright and recovered only via an admin password-set.
    The supplier portal has no passkey support (`WebAuthnCredential` is keyed to
    a control-plane `users.id`, and `VendorUser` is tenant-scoped), so there this
    function is still the whole story.

    Whether a password is *admissible* at all is the caller's to decide, and the
    employee surface decides it before calling: in a tenant that has closed
    password sign-in (`sso_only`) the route passes `password=None`, because a
    password that cannot sign in must not authenticate a factor change either
    (`api/auth._step_up_satisfied`). This helper stays pure — no org, no DB.

    Shared by the employee and supplier-portal surfaces so the two can't drift.
    Returns a plain bool; the caller decides the status code. Password
    comparison goes through the shared `verify_password` (the `pwd_context`
    bcrypt_sha256 wrapper, run off the event loop), TOTP through `verify_totp` —
    both constant-time internally.
    """
    if password and hashed_password and await verify_password(password, hashed_password):
        return True
    if code and mfa_secret and await verify_totp(mfa_secret, code):
        return True
    return False


# ---------------------------------------------------------------------------
# Email OTP (backup)
# ---------------------------------------------------------------------------


def _email_otp_key(user_id: uuid.UUID) -> str:
    return f"{EMAIL_OTP_PREFIX}{user_id}"


def _hash_otp(code: str) -> str:
    """Server-secret-keyed PBKDF2 of the OTP before storing — a Redis snapshot
    must not reveal the code, and keying it means the low-entropy 6-digit space
    can't be brute-forced from the stored digest without the server secret."""
    return _keyed_digest(code)


async def issue_email_otp(user_id: uuid.UUID) -> str:
    """Generate a 6-digit email backup code, store its hash in Redis, return plaintext.

    Caller is responsible for actually sending the code via the email adapter.
    Each call invalidates any previous outstanding code for the user.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    r = await get_redis()
    await r.setex(_email_otp_key(user_id), settings.mfa_email_otp_ttl_seconds, _hash_otp(code))
    return code


async def verify_email_otp(user_id: uuid.UUID, code: str) -> bool:
    """Verify + consume a previously-issued email OTP. Single-use."""
    if not code:
        return False
    r = await get_redis()
    key = _email_otp_key(user_id)
    stored = await r.get(key)
    if not stored:
        return False
    stored_hex = stored.decode("utf-8") if isinstance(stored, bytes) else stored
    # constant-time compare to thwart timing oracles
    if not hmac.compare_digest(stored_hex, _hash_otp(code.strip())):
        return False
    await r.delete(key)
    return True


# Supplier-portal email-OTP backup — same mechanism as the employee one above,
# but in a distinct Redis keyspace (`VENDOR_EMAIL_OTP_PREFIX`) so a vendor-user
# id and an employee-user id of the same UUID value never share a slot.


def _vendor_email_otp_key(vendor_user_id: uuid.UUID) -> str:
    return f"{VENDOR_EMAIL_OTP_PREFIX}{vendor_user_id}"


async def issue_vendor_email_otp(vendor_user_id: uuid.UUID) -> str:
    """Generate a 6-digit email backup code for a supplier-portal user, store
    its hash in Redis, return the plaintext for the caller to email. Each call
    invalidates any previous outstanding code for that vendor user."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    r = await get_redis()
    await r.setex(
        _vendor_email_otp_key(vendor_user_id),
        settings.mfa_email_otp_ttl_seconds,
        _hash_otp(code),
    )
    return code


async def verify_vendor_email_otp(vendor_user_id: uuid.UUID, code: str) -> bool:
    """Verify + consume a previously-issued supplier-portal email OTP. Single-use."""
    if not code:
        return False
    r = await get_redis()
    key = _vendor_email_otp_key(vendor_user_id)
    stored = await r.get(key)
    if not stored:
        return False
    stored_hex = stored.decode("utf-8") if isinstance(stored, bytes) else stored
    if not hmac.compare_digest(stored_hex, _hash_otp(code.strip())):
        return False
    await r.delete(key)
    return True


# ---------------------------------------------------------------------------
# Challenge token (short-lived JWT issued after password check)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChallengeTokenClaims:
    """The decoded subject id + jti of a verified, not-yet-consumed challenge
    token. Callers that actually complete the MFA exchange (mint a real
    access token) must call `consume_challenge_token(claims.jti)` — decoding
    alone does not burn the token, since some callers (e.g. requesting an
    email OTP) legitimately decode it more than once before the user
    completes the factor."""

    subject_id: uuid.UUID
    jti: str


async def consume_challenge_token(jti: str) -> None:
    """Single-use: blocklist a challenge token's jti immediately after a
    successful MFA verify (issue #162) so it can't be replayed to mint a
    second access token from the same password check. Shares the same Redis
    blocklist as regular access-token logout — jti values never collide
    across token types since each is a freshly generated UUID."""
    await block_token(jti, settings.mfa_challenge_ttl_seconds)


def create_challenge_token(user_id: uuid.UUID) -> str:
    expire = datetime.now(UTC) + timedelta(seconds=settings.mfa_challenge_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "typ": CHALLENGE_TYPE,
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


async def decode_challenge_token(token: str) -> ChallengeTokenClaims:
    """Verify a challenge token and return its claims. Raises ValueError on a
    bad, expired, wrong-type, or already-consumed (replayed) token."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid or expired MFA challenge token") from exc
    if payload.get("typ") != CHALLENGE_TYPE:
        raise ValueError("Wrong token type for MFA challenge")
    try:
        subject_id = uuid.UUID(payload["sub"])
        jti = payload["jti"]
    except (KeyError, ValueError) as exc:
        raise ValueError("Malformed MFA challenge token") from exc
    if await is_token_blocked(jti):
        raise ValueError("MFA challenge token already used")
    return ChallengeTokenClaims(subject_id=subject_id, jti=jti)


# ---------------------------------------------------------------------------
# Vendor (supplier-portal) MFA challenge token — same shape, distinct `typ`
# ---------------------------------------------------------------------------


def create_vendor_challenge_token(vendor_user_id: uuid.UUID) -> str:
    """Short-lived JWT proving a supplier-portal password was accepted but MFA
    is still owed. `typ=vendor_mfa_challenge` keeps it out of every other auth
    path (employee challenge, vendor access token)."""
    expire = datetime.now(UTC) + timedelta(seconds=settings.mfa_challenge_ttl_seconds)
    payload = {
        "sub": str(vendor_user_id),
        "typ": VENDOR_CHALLENGE_TYPE,
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


async def decode_vendor_challenge_token(token: str) -> ChallengeTokenClaims:
    """Verify a vendor MFA challenge token and return its claims. Raises
    ValueError on a bad, expired, wrong-type, or already-consumed (replayed)
    token. `subject_id` is the vendor_user_id."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid or expired MFA challenge token") from exc
    if payload.get("typ") != VENDOR_CHALLENGE_TYPE:
        raise ValueError("Wrong token type for MFA challenge")
    try:
        subject_id = uuid.UUID(payload["sub"])
        jti = payload["jti"]
    except (KeyError, ValueError) as exc:
        raise ValueError("Malformed MFA challenge token") from exc
    if await is_token_blocked(jti):
        raise ValueError("MFA challenge token already used")
    return ChallengeTokenClaims(subject_id=subject_id, jti=jti)
