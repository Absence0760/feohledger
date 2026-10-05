"""Redis client, token blocklist, and per-user active-session tracking."""

import time

import redis.asyncio as redis

from app.config import settings

pool = redis.ConnectionPool.from_url(settings.redis_url)

BLOCKLIST_PREFIX = "token:blocked:"
SESSION_SET_PREFIX = "active_jtis:"
# Companion hash to the sorted set above: field = JTI, value = a small JSON
# blob describing the session (IP, device label, sign-in method) so the user
# can recognise their own sessions in the "where you're signed in" list. The
# sorted set stays the authoritative membership/ordering structure — this hash
# only decorates it, and every membership mutation keeps the two in step.
SESSION_META_PREFIX = "session_meta:"


async def get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=pool)


async def block_token(jti: str, expires_in: int) -> None:
    """Add a token ID to the blocklist. Expires automatically with the token."""
    r = await get_redis()
    await r.setex(f"{BLOCKLIST_PREFIX}{jti}", expires_in, "1")


async def claim_token_block(jti: str, expires_in: int) -> bool:
    """Blocklist a token ID only if it is not already blocklisted, atomically.

    `block_token`'s SETEX cannot tell its caller whether the jti was already
    burned, which is exactly what a single-use token (the MFA challenge) needs
    to know: two concurrent redemptions both passed `is_token_blocked`, so the
    one that loses this `SET NX` must refuse. True when this call claimed it.
    """
    r = await get_redis()
    return bool(await r.set(f"{BLOCKLIST_PREFIX}{jti}", "1", nx=True, ex=expires_in))


async def is_token_blocked(jti: str) -> bool:
    """Check if a token has been blocklisted (logged out)."""
    r = await get_redis()
    return await r.exists(f"{BLOCKLIST_PREFIX}{jti}") > 0


# ---------------------------------------------------------------------------
# Per-user active-session tracking (concurrent session limit + forced logout)
#
# Each login adds the newly-minted JTI to a Redis sorted set keyed by user,
# scored by issue time (epoch seconds). This lets us:
#   - Enforce a concurrent-session cap: evict the lowest-scored (oldest) JTI
#     when the set exceeds the configured max.
#   - Revoke every session for a user on admin role change / deactivation.
# The set itself carries a TTL equal to the access-token lifetime so stale
# JTIs age out naturally if a user stops logging in.
# ---------------------------------------------------------------------------


def _session_key(user_id) -> str:
    return f"{SESSION_SET_PREFIX}{user_id}"


def _session_meta_key(user_id) -> str:
    return f"{SESSION_META_PREFIX}{user_id}"


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def track_session(
    user_id,
    jti: str,
    ttl_seconds: int,
    max_sessions: int,
    meta: str | None = None,
) -> list[str]:
    """Record a new session JTI for a user and enforce the concurrent-session cap.

    Returns the list of JTIs that were evicted (oldest-first) because the
    user was over the cap. Callers should add these to the blocklist so the
    evicted tokens stop authenticating immediately.

    If `max_sessions` is 0 or negative, no cap is enforced.

    `meta` is an optional pre-serialised JSON blob describing the session. It
    is stored in the companion hash under the same JTI and torn down with it,
    so the descriptive layer can never outlive the membership it describes.
    """
    r = await get_redis()
    key = _session_key(user_id)
    meta_key = _session_meta_key(user_id)
    now = time.time()

    # ZADD — score = issue time so ZRANGE returns oldest-first.
    await r.zadd(key, {jti: now})
    # Refresh TTL on the set so it doesn't outlive the token lifetime by much.
    # Set is refreshed on every login, which is what we want — inactive users
    # have their set evicted by Redis automatically.
    await r.expire(key, ttl_seconds)
    if meta is not None:
        await r.hset(meta_key, jti, meta)
        await r.expire(meta_key, ttl_seconds)

    evicted: list[str] = []
    if max_sessions and max_sessions > 0:
        count = await r.zcard(key)
        if count > max_sessions:
            # Oldest first (lowest score). Pop (count - max_sessions) entries.
            overflow = count - max_sessions
            members = await r.zrange(key, 0, overflow - 1)
            for m in members:
                evicted.append(_decode(m))
            if members:
                await r.zrem(key, *members)
                await r.hdel(meta_key, *evicted)
    return evicted


async def untrack_session(user_id, jti: str) -> None:
    """Remove a single JTI from the user's active set (called on logout)."""
    r = await get_redis()
    await r.zrem(_session_key(user_id), jti)
    await r.hdel(_session_meta_key(user_id), jti)


async def get_active_sessions(user_id) -> list[str]:
    """Return the JTIs currently tracked as active for the user."""
    r = await get_redis()
    members = await r.zrange(_session_key(user_id), 0, -1)
    return [_decode(m) for m in members]


async def get_active_sessions_with_scores(user_id) -> list[tuple[str, float]]:
    """Return `(jti, issued_at_epoch)` for every tracked session, oldest first.

    The score is the epoch seconds `track_session` stamped at sign-in, which is
    what lets the caller derive a session's age and expiry without a second
    lookup.
    """
    r = await get_redis()
    rows = await r.zrange(_session_key(user_id), 0, -1, withscores=True)
    return [(_decode(member), float(score)) for member, score in rows]


async def get_session_meta(user_id) -> dict[str, str]:
    """Return the raw per-JTI metadata blobs for a user (`{jti: json}`)."""
    r = await get_redis()
    raw = await r.hgetall(_session_meta_key(user_id))
    return {_decode(k): _decode(v) for k, v in (raw or {}).items()}


async def drop_sessions(user_id, jtis: list[str]) -> None:
    """Forget sessions without blocklisting them.

    Used to prune entries whose token already expired on its own — there is
    nothing left to revoke, so adding them to the blocklist would only waste
    keys. Blocklisting a *live* session is `revoke_session` / `block_token`.
    """
    if not jtis:
        return
    r = await get_redis()
    await r.zrem(_session_key(user_id), *jtis)
    await r.hdel(_session_meta_key(user_id), *jtis)


async def revoke_session(user_id, jti: str, ttl_seconds: int) -> bool:
    """Blocklist one tracked JTI and forget it. Returns False if not tracked.

    The membership check is the authorization: a JTI is only ever revocable
    through the set of the user it belongs to, so one user can never revoke
    another's session even by guessing a JTI.
    """
    r = await get_redis()
    removed = await r.zrem(_session_key(user_id), jti)
    if not removed:
        return False
    await r.hdel(_session_meta_key(user_id), jti)
    await r.setex(f"{BLOCKLIST_PREFIX}{jti}", ttl_seconds, "1")
    return True


async def revoke_all_sessions(
    user_id, ttl_seconds: int, except_jti: str | None = None
) -> list[str]:
    """Blocklist every active JTI for the user and clear the tracking set.

    Used on admin role change and account deactivation — SOC 2 expects an
    actor's active sessions to drop the moment their permissions change.
    Returns the list of JTIs that were revoked.

    `except_jti` keeps one session alive (and tracked) — the "sign out
    everywhere else" case, where signing the caller out too would be a
    surprising side effect of a defensive action.
    """
    r = await get_redis()
    key = _session_key(user_id)
    members = await r.zrange(key, 0, -1)
    jtis = [_decode(m) for m in members if _decode(m) != except_jti]
    for jti in jtis:
        await r.setex(f"{BLOCKLIST_PREFIX}{jti}", ttl_seconds, "1")
    if not jtis:
        return jtis
    keeping = except_jti is not None and any(_decode(m) == except_jti for m in members)
    if keeping:
        await r.zrem(key, *jtis)
        await r.hdel(_session_meta_key(user_id), *jtis)
    else:
        # Nothing survives — drop both keys outright rather than removing every
        # member one by one.
        await r.delete(key)
        await r.delete(_session_meta_key(user_id))
    return jtis
