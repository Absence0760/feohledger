"""Single-use password-set tokens — forgot-password, and every set-password link.

Two token families share one construction (random 32-byte token, keyed PBKDF2
digest as the Redis key, TTL, atomic ``GETDEL`` redemption) under two key
prefixes:

* **Employee** (``auth:pwreset:``) — a control-plane ``User``. Minted by
  forgot-password, signup (§283), admin invite / reset and partner child
  provisioning; redeemed by ``POST /api/auth/reset-password``.
* **Supplier portal** (``portal:pwreset:``) — a tenant-DB ``VendorUser``,
  minted by the AP team's portal invite and reset; redeemed by ``POST
  /api/portal/auth/reset-password``. The stored value binds the token to its
  tenant slug, because a ``VendorUser`` id means nothing outside its own
  tenant DB; a token presented on another tenant's host is refused WITHOUT
  being spent.

Separate prefixes so neither redemption endpoint can ever accept the other
family's token, whatever the ids happen to be.

**Superseding.** Each stored value also carries the subject's *generation*
(``<subject>#<n>``), and a per-subject counter (``…gen:<subject>``) holds the
current one. :func:`revoke_outstanding` / :func:`revoke_outstanding_portal`
bump it, which kills every link minted before — an admin reset calls it before
minting its own link, and every successful redemption calls it after, so a
72-hour invite or reset link cannot outlive the password it was for. A token
whose generation is not current is refused exactly like an expired one.

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
PORTAL_PASSWORD_RESET_PREFIX = "portal:pwreset:"

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


def _portal_reset_key(token: str) -> str:
    return f"{PORTAL_PASSWORD_RESET_PREFIX}{_keyed_digest(token)}"


def _gen_key(prefix: str, subject: str) -> str:
    return f"{prefix}gen:{subject}"


def _gen_ttl() -> int:
    """How long a generation counter must outlive every token it can refer to."""
    return (
        max(
            settings.password_reset_ttl_minutes * 60,
            settings.signup_set_password_ttl_hours * 3600,
            settings.set_password_link_ttl_hours * 3600,
        )
        + 3600
    )


async def _current_gen(r, prefix: str, subject: str) -> str:
    raw = await r.get(_gen_key(prefix, subject))
    if not raw:
        return "0"
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


async def _bump_gen(prefix: str, subject: str) -> None:
    r = await get_redis()
    key = _gen_key(prefix, subject)
    await r.incr(key)
    await r.expire(key, _gen_ttl())


def _split_gen(raw: bytes | str | None) -> tuple[str, str] | None:
    """``(subject, generation)`` of a stored value; a value written before the
    generation existed reads as generation ``0``."""
    if not raw:
        return None
    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    subject, sep, gen = raw_str.partition("#")
    return subject, (gen if sep else "0")


def stored_owner(raw: bytes | str | None) -> str | None:
    """The subject an employee token's stored value names (``<user_id>``),
    whatever its generation — for :mod:`services.credential_cleanup`, which
    deletes a deleted account's links outright rather than asking whether they
    are still live. Comparing the raw value would never match: it carries
    ``#<generation>``."""
    parts = _split_gen(raw)
    return parts[0] if parts else None


def generation_key(user_id: uuid.UUID) -> str:
    """The employee generation counter of ``user_id``."""
    return _gen_key(PASSWORD_RESET_PREFIX, str(user_id))


def is_generation_key(key: bytes | str) -> bool:
    """Whether a key under the employee prefix is a generation counter rather
    than a token (its value is a number, not a subject)."""
    key_str = key.decode("utf-8") if isinstance(key, bytes) else key
    return key_str.startswith(f"{PASSWORD_RESET_PREFIX}gen:")


async def _live_subject(r, prefix: str, raw: bytes | str | None) -> str | None:
    """The subject of a stored value, or ``None`` when it was superseded."""
    parts = _split_gen(raw)
    if parts is None:
        return None
    subject, gen = parts
    owner = subject.rpartition(":")[2]  # a portal subject is "<slug>:<id>"
    if gen != await _current_gen(r, prefix, owner):
        return None
    return subject


async def revoke_outstanding(user_id: uuid.UUID) -> None:
    """Supersede every employee link minted for ``user_id`` so far."""
    await _bump_gen(PASSWORD_RESET_PREFIX, str(user_id))


async def revoke_outstanding_portal(vendor_user_id: uuid.UUID) -> None:
    """Supersede every portal link minted for ``vendor_user_id`` so far."""
    await _bump_gen(PORTAL_PASSWORD_RESET_PREFIX, str(vendor_user_id))


def set_password_link_ttl_seconds() -> int:
    """Lifetime of a set-password link nobody asked for — an admin or partner
    invite, an admin-initiated reset, a supplier-portal invite or reset
    (``FEOH_SET_PASSWORD_LINK_TTL_HOURS``)."""
    return settings.set_password_link_ttl_hours * 3600


async def _store(key: str, value: str, ttl: int) -> None:
    if ttl <= 0:
        raise ValueError("A reset token needs a positive lifetime.")
    r = await get_redis()
    await r.setex(key, ttl, value)


async def issue_reset_token(user_id: uuid.UUID, *, ttl_seconds: int | None = None) -> str:
    """Mint a fresh single-use reset token for ``user_id``, store its digest
    in Redis mapped to the user id, and return the plaintext for the caller
    to email. A previously issued, still-outstanding token for this user (if
    any) is left alone unless the caller first calls :func:`revoke_outstanding`
    (an admin reset does); every one of them dies when any is redeemed.

    ``ttl_seconds`` defaults to the forgot-password lifetime
    (``FEOH_PASSWORD_RESET_TTL_MINUTES``). The other callers are the
    set-password links — signup's (``FEOH_SIGNUP_SET_PASSWORD_TTL_HOURS``,
    ``docs/decisions.md`` §283) and the admin / partner ones
    (:func:`set_password_link_ttl_seconds`): the same token, store and
    single-use redemption through ``POST /api/auth/reset-password``, only a
    longer life, because nobody asked for it a minute ago and is waiting at
    the keyboard."""
    token = secrets.token_urlsafe(32)
    ttl = ttl_seconds if ttl_seconds is not None else settings.password_reset_ttl_minutes * 60
    r = await get_redis()
    gen = await _current_gen(r, PASSWORD_RESET_PREFIX, str(user_id))
    await _store(_reset_key(token), f"{user_id}#{gen}", ttl)
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
    raw = await r.get(_reset_key(token))
    return _as_user_id(await _live_subject(r, PASSWORD_RESET_PREFIX, raw))


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
    raw = await r.getdel(_reset_key(token))
    user_id = _as_user_id(await _live_subject(r, PASSWORD_RESET_PREFIX, raw))
    if user_id is not None:
        # Redeemed: every sibling link for the account is now stale.
        await revoke_outstanding(user_id)
    return user_id


def _as_user_id(raw: bytes | str | None) -> uuid.UUID | None:
    if not raw:
        return None
    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        return uuid.UUID(raw_str)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Supplier portal (VendorUser, tenant DB)
# ---------------------------------------------------------------------------


async def issue_portal_reset_token(
    slug: str, vendor_user_id: uuid.UUID, *, ttl_seconds: int | None = None
) -> str:
    """Mint a single-use set-password token for a supplier-portal user.

    The stored value is ``"<slug>:<vendor_user_id>"``: the tenant slug the
    account lives under travels with the token, so redemption can refuse a
    token presented on another tenant's host before spending it. Defaults to
    the set-password-link lifetime — the only minters are the AP team's invite
    and reset, which the supplier did not ask for a minute ago.
    """
    if not slug or ":" in slug:
        raise ValueError("A portal reset token needs a tenant slug.")
    token = secrets.token_urlsafe(32)
    ttl = ttl_seconds if ttl_seconds is not None else set_password_link_ttl_seconds()
    r = await get_redis()
    gen = await _current_gen(r, PORTAL_PASSWORD_RESET_PREFIX, str(vendor_user_id))
    await _store(_portal_reset_key(token), f"{slug}:{vendor_user_id}#{gen}", ttl)
    return token


async def peek_portal_reset_token(token: str, slug: str) -> uuid.UUID | None:
    """The vendor-user id a still-valid portal token belongs to on THIS tenant,
    without consuming it. ``None`` for unknown, spent, expired, and a token
    minted for another tenant — indistinguishably."""
    if not token or not slug:
        return None
    r = await get_redis()
    stored = await r.get(_portal_reset_key(token))
    return _portal_subject(await _live_subject(r, PORTAL_PASSWORD_RESET_PREFIX, stored), slug)


async def consume_portal_reset_token(token: str, slug: str) -> uuid.UUID | None:
    """Redeem a portal token on tenant ``slug``: single-use, atomic.

    A token minted for another tenant is refused and left in place — a
    supplier who opened the link on the wrong host has not lost it. Only a
    token whose tenant matches is spent, by ``GETDEL``, so two concurrent
    redemptions cannot both succeed.
    """
    if not token or not slug:
        return None
    key = _portal_reset_key(token)
    r = await get_redis()
    if (
        _portal_subject(
            await _live_subject(r, PORTAL_PASSWORD_RESET_PREFIX, await r.get(key)), slug
        )
        is None
    ):
        return None
    raw = await _live_subject(r, PORTAL_PASSWORD_RESET_PREFIX, await r.getdel(key))
    vu_id = _portal_subject(raw, slug)
    if vu_id is not None:
        # Redeemed: every sibling link for the supplier is now stale.
        await revoke_outstanding_portal(vu_id)
    return vu_id


def _portal_subject(raw: bytes | str | None, slug: str) -> uuid.UUID | None:
    if not raw:
        return None
    raw_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    stored_slug, sep, vu_id = raw_str.rpartition(":")
    if not sep or stored_slug != slug:
        return None
    return _as_user_id(vu_id)
