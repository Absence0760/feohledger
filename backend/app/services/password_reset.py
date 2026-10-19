"""Self-service password recovery for the main AP app (main app only — the
supplier portal is out of scope; see `docs/authentication.md`).

A user who forgot their password requests a reset link
(``POST /api/auth/forgot-password``), gets emailed a single-use token, and
redeems it (``POST /api/auth/reset-password``) to set a new one. This module
owns only the token primitive; ``app/api/auth.py`` owns the rate limiting,
the (org-scoped) user lookup, the enumeration-resistant response, and the
email send.

Mirrors the MFA pending-enrollment / email-OTP Redis patterns in
``services/mfa.py``: no DB row for the token — it lives in Redis, keyed by a
digest of the token itself, mapped to the user id, with a short TTL. A stray
un-redeemed reset link needs no garbage collection; it just expires.
Consuming a token (:func:`consume_reset_token`) is a single atomic
``GETDEL``, so two concurrent redemptions of the same token can't both
succeed — the second sees nothing, same as an already-expired one.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from app.config import settings
from app.redis import get_redis

PASSWORD_RESET_PREFIX = "auth:pwreset:"

_KEYED_KDF_ITERATIONS = 100_000


def _keyed_digest(token: str) -> str:
    """PBKDF2-HMAC-SHA256 of the token, keyed by the server secret — same
    construction as ``services/mfa.py::_keyed_digest``. The token itself is
    already high-entropy (32 random bytes via `secrets.token_urlsafe`), so
    this isn't a brute-force defense; it's so a Redis-keyspace snapshot
    doesn't hand over the live, still-valid secret that resets the account,
    the same reasoning `mfa.verify_email_otp` applies to its low-entropy
    6-digit code, one rung stronger here since the input isn't guessable
    either way."""
    return hashlib.pbkdf2_hmac(
        "sha256", token.encode("utf-8"), settings.secret_key.encode("utf-8"), _KEYED_KDF_ITERATIONS
    ).hex()


def _reset_key(token: str) -> str:
    return f"{PASSWORD_RESET_PREFIX}{_keyed_digest(token)}"


async def issue_reset_token(user_id: uuid.UUID, *, ttl_seconds: int | None = None) -> str:
    """Mint a fresh single-use reset token for ``user_id``, store its digest
    in Redis mapped to the user id, and return the plaintext for the caller
    to email. A previously issued, still-outstanding token for this user (if
    any) is left alone — it simply expires on its own TTL or gets consumed
    first, whichever happens; multiple valid links for one user is not a
    security concern since each is still a single, unguessable secret.

    ``ttl_seconds`` defaults to the forgot-password lifetime
    (``FEOH_PASSWORD_RESET_TTL_MINUTES``). The one other caller is signup's
    set-password link (``FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS``,
    ``docs/decisions.md`` §283): the same token, store and single-use redemption
    through ``POST /api/auth/reset-password``, only a longer life, because
    nobody asked for it a minute ago and is waiting at the keyboard."""
    token = secrets.token_urlsafe(32)
    ttl = ttl_seconds if ttl_seconds is not None else settings.password_reset_ttl_minutes * 60
    if ttl <= 0:
        raise ValueError("A reset token needs a positive lifetime.")
    r = await get_redis()
    await r.setex(_reset_key(token), ttl, str(user_id))
    return token


async def peek_reset_token(token: str) -> uuid.UUID | None:
    """The user id a still-valid token belongs to, WITHOUT consuming it.

    Lets the reset page say "this link has expired" before the visitor types a
    password, and fill the hidden username field a password manager files the
    new password under. Returns ``None`` for exactly the cases
    :func:`consume_reset_token` does, so it reveals nothing that redeeming the
    token would not; only the holder of the secret learns anything.
    """
    if not token:
        return None
    r = await get_redis()
    return _as_user_id(await r.get(_reset_key(token)))


async def consume_reset_token(token: str) -> uuid.UUID | None:
    """Atomically read + delete the token's mapping.

    Returns the user id for a still-valid, not-yet-used token; ``None`` for
    anything else (unknown, already consumed, or expired) — deliberately
    indistinguishable, so a reused or forged token reveals no more than an
    expired one.
    """
    if not token:
        return None
    r = await get_redis()
    return _as_user_id(await r.getdel(_reset_key(token)))


def _as_user_id(raw: bytes | str | None) -> uuid.UUID | None:
    if not raw:
        return None
    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        return uuid.UUID(raw_str)
    except ValueError:
        return None
