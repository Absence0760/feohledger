"""Redis-backed sliding-window rate limiter.

Used to cap abuse vectors on unauthenticated endpoints (signup, captcha).
Each bucket is a Redis key holding a sorted-set of timestamps; we trim
entries older than the window and count what remains. ZADD/ZREMRANGEBYSCORE/
ZCARD are all O(log n) and run in a single MULTI.

Scope: one counter per (endpoint, client IP). For shared-IP networks this
is coarse but acceptable for the low-volume abuse surface of tenant signup.

This module also owns the **per-identity authentication-failure throttle**
(bottom half): the per-IP brake above cannot see a credential-stuffing or
TOTP-guessing run distributed across rotating source addresses, which is the
cheap, commodity shape of that attack. See ``check_auth_failures``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import time
import uuid

from fastapi import HTTPException, Request, status

from app.api.refusals import coded_refusal
from app.config import settings
from app.redis import get_redis

KEY_PREFIX = "ratelimit:"

#: The one code both axes answer with. No more specific than the English it
#: replaces: which bucket tripped, its limit and count stay server-side
#: (`docs/authentication.md` § Brute-force protection).
RATE_LIMITED = "rate_limited"


class RateLimitExceeded(HTTPException):
    """The limiter's 429: ``Retry-After`` plus the coded ``rate_limited`` refusal,
    whose only param is the header's own figure."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=coded_refusal(
                RATE_LIMITED,
                f"Too many requests. Please wait {retry_after_seconds} seconds "
                "before trying again.",
                retry_after_seconds=retry_after_seconds,
            ),
            headers={"Retry-After": str(retry_after_seconds)},
        )


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = (settings.trusted_proxy_cidrs or "").strip()
    if not raw:
        return ()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # Skip junk silently — a typo in config shouldn't crash the
            # rate limiter, and any unknown CIDR means "don't trust XFF
            # from anything", which is the safe default anyway.
            continue
    return tuple(nets)


def _ip_in_trusted_proxy(ip: str) -> bool:
    nets = _trusted_proxy_networks()
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def resolve_client_ip(request: Request | None) -> str | None:
    """Resolve the originating client IP.

    Only honours ``X-Forwarded-For`` when the connecting peer is in the
    configured ``trusted_proxy_cidrs`` allowlist — otherwise a direct
    attacker could rotate through arbitrary IPs by spoofing the header
    and dodge per-IP rate limits.

    This is the single shared resolver: every request-path caller that
    records or keys on a client IP (limiter buckets, login/signup/SSO
    audit rows, captcha verification) goes through here, so they all
    agree on who the client is when the app sits behind a trusted proxy
    (Caddy on the single-VM stack, the ALB on ECS).
    """
    if request is None or request.client is None:
        return None
    peer = request.client.host
    if _ip_in_trusted_proxy(peer):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def _client_ip(request: Request) -> str:
    return resolve_client_ip(request) or "unknown"


async def check_rate_limit(
    endpoint: str,
    request: Request | None = None,
    *,
    limit: int,
    window_seconds: int,
    subject: str | None = None,
) -> None:
    """Record this request and raise 429 if the caller is over the limit.

    The bucket is keyed on ``(endpoint, subject)``. ``subject`` defaults to the
    resolved client IP, but callers can pass an explicit value — e.g. the
    target email address, or an authenticated API key id — to cap abuse that a
    per-IP limit can't (an attacker rotating IPs to email-bomb one victim
    address, or a single API key flooding the public API). When ``subject`` is
    given the ``request`` is unused, so callers keying on an explicit subject
    may omit it; only the IP-fallback path needs the request.

    No-ops when ``settings.rate_limit_enabled`` is False. The switch
    exists so CI's e2e job (where every shard's 4 workers hit
    ``/api/auth/login`` from the same loopback IP) doesn't saturate
    the 10/min cap before the storage-state preload finishes.
    Deployed envs keep the limiter on by default.
    """
    if not settings.rate_limit_enabled:
        return

    if subject is not None:
        client = subject
    elif request is not None:
        client = _client_ip(request)
    else:
        raise ValueError("check_rate_limit requires either a request or an explicit subject")
    key = f"{KEY_PREFIX}{endpoint}:{client}"
    now = time.time()
    cutoff = now - window_seconds

    r = await get_redis()
    async with r.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {str(uuid.uuid4()): now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds)
        _, _, count, _ = await pipe.execute()

    if count > limit:
        # Retry-After: time until oldest entry in the window ages out.
        oldest = await r.zrange(key, 0, 0, withscores=True)
        if oldest:
            wait = int(oldest[0][1] + window_seconds - now)
            raise RateLimitExceeded(max(wait, 1))
        raise RateLimitExceeded(window_seconds)


# ---------------------------------------------------------------------------
# Per-identity authentication-failure throttle
# ---------------------------------------------------------------------------
#
# The limiter above buckets on the client IP. That stops one host hammering an
# endpoint, but it is blind to the actual shape of a modern password spray or
# second-factor guessing run: the same account attacked from thousands of
# residential-proxy addresses, each staying comfortably under the per-IP cap.
# The primitives below add the missing axis — a counter keyed on the IDENTITY
# being authenticated, so the budget is shared across every source address.
#
# Three properties make this safe to put in front of a login form:
#
#   1. **Failures only.** ``check_auth_failures`` never records anything; only
#      ``record_auth_failure`` does, and a successful authentication calls
#      ``clear_auth_failures``. A user typing the right credential is therefore
#      never counted against, however often they sign in.
#   2. **Keyed on the SUBMITTED identifier, checked before the account lookup.**
#      An address with no account throttles exactly like one that has an
#      account, so the 429 can never be used as an account-existence oracle.
#   3. **Hashed.** The identifier is digested before it becomes a Redis key, so
#      an email address never lands in the keyspace (or in anything that dumps
#      it, e.g. `redis-cli --scan`).
#
# Accepted trade-off: because the bucket is keyed on the identity, an attacker
# who knows an address can burn that account's *password* budget and delay a
# legitimate sign-in until the window rolls off. That is bounded (a rolling
# window, never a sticky lock), self-healing, and does not touch the SSO or
# passkey paths — which is the same call every mainstream implementation makes,
# and strictly better than leaving the spray unbounded.

AUTH_FAILURE_PREFIX = "authfail:"

# Password login. High enough that a human cycling through the passwords they
# might have used never notices; low enough that a distributed spray drops from
# unbounded to a few dozen guesses per account per day.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_SECONDS = 900  # 15 minutes

# Second factor. A TOTP code is 6 digits (10^6 keyspace) and an email OTP is
# single-use, so online guessing is the only avenue — 5 per 15 minutes puts a
# meaningful sweep of that keyspace orders of magnitude out of reach, where the
# per-IP cap alone left it reachable for anyone with a few hundred addresses.
MFA_FAILURE_LIMIT = 5
MFA_FAILURE_WINDOW_SECONDS = 900

# Emailed backup codes are *sent* mail, not a guess, so they are capped as a
# plain request budget (via `check_rate_limit(subject=...)`) rather than a
# failure budget. Per ACCOUNT and per hour, because the per-IP cap can't stop
# one valid challenge token being replayed from rotating addresses to bomb a
# victim's inbox. Generous enough for someone legitimately re-requesting a code
# that hasn't landed yet.
EMAIL_OTP_PER_ACCOUNT_PER_HOUR = 10


def auth_identity_key(*parts: str) -> str:
    """Digest an identity into an opaque, stable bucket key.

    ``parts`` are joined and hashed, so the raw value (an email address, a
    tenant slug, a user id) never becomes part of a Redis key. Case- and
    whitespace-normalised so ``Ada@Example.com `` and ``ada@example.com`` share
    one budget — otherwise the throttle is trivially side-stepped by varying
    the capitalisation of the address being sprayed.

    Multi-part callers (the supplier portal passes ``slug, email``) get a
    tenant-scoped bucket: a vendor address is unique only *within* a tenant DB,
    so keying on the address alone would let one tenant's traffic throttle
    another tenant's supplier.
    """
    raw = "\x1f".join(p.strip().casefold() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _auth_failure_key(scope: str, identity_key: str) -> str:
    return f"{AUTH_FAILURE_PREFIX}{scope}:{identity_key}"


async def check_auth_failures(
    scope: str,
    identity_key: str,
    *,
    limit: int,
    window_seconds: int,
) -> None:
    """Raise 429 if this identity already has ``limit`` recent failures.

    Read-only — it trims the window and counts, but never records an attempt.
    Call it *before* looking the account up (see property 2 above).
    """
    if not settings.rate_limit_enabled:
        return

    key = _auth_failure_key(scope, identity_key)
    now = time.time()

    r = await get_redis()
    async with r.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        _, count = await pipe.execute()

    if count >= limit:
        oldest = await r.zrange(key, 0, 0, withscores=True)
        if oldest:
            wait = int(oldest[0][1] + window_seconds - now)
            raise RateLimitExceeded(max(wait, 1))
        raise RateLimitExceeded(window_seconds)


async def record_auth_failure(scope: str, identity_key: str, *, window_seconds: int) -> None:
    """Record one failed authentication attempt against this identity."""
    if not settings.rate_limit_enabled:
        return

    key = _auth_failure_key(scope, identity_key)
    now = time.time()

    r = await get_redis()
    async with r.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zadd(key, {str(uuid.uuid4()): now})
        pipe.expire(key, window_seconds)
        await pipe.execute()


async def clear_auth_failures(scope: str, identity_key: str) -> None:
    """Drop this identity's failure budget — call it once the credential checks
    out, so a legitimate user is never penalised for earlier typos (or for
    someone else's spray against their address)."""
    if not settings.rate_limit_enabled:
        return

    r = await get_redis()
    await r.delete(_auth_failure_key(scope, identity_key))
