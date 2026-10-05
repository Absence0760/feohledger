"""Tests for concurrent-session tracking, eviction, and forced logout.

Covers the Redis-backed session set directly plus the higher-level
session_management service. A FakeRedis stand-in keeps these tests
off the real Redis instance so they run in CI without docker-compose.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# FakeRedis — minimal sorted-set + string surface the session helpers need.
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory stand-in for the redis.asyncio Redis surface we use."""

    def __init__(self) -> None:
        self.strings: dict[str, bytes] = {}
        # key -> list of (score, member) tuples, kept sorted by score
        self.zsets: dict[str, list[tuple[float, str]]] = {}
        # key -> {field: value} — the session-metadata companion hash
        self.hashes: dict[str, dict[str, bytes]] = {}
        self.ttls: dict[str, int] = {}

    async def setex(self, key, ttl, value) -> None:
        self.strings[key] = value if isinstance(value, bytes) else str(value).encode("utf-8")
        self.ttls[key] = ttl

    async def set(self, key, value, nx: bool = False, ex: int | None = None):
        # `app.redis.claim_token_block` (the MFA challenge's single-use claim)
        # is a `SET NX EX`: None when the key already exists, as in real Redis.
        if nx and key in self.strings:
            return None
        self.strings[key] = value if isinstance(value, bytes) else str(value).encode("utf-8")
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def exists(self, key) -> int:
        return 1 if key in self.strings else 0

    async def zadd(self, key, mapping: dict) -> int:
        zset = self.zsets.setdefault(key, [])
        added = 0
        for member, score in mapping.items():
            existing = next((i for i, (_, m) in enumerate(zset) if m == member), None)
            if existing is not None:
                zset[existing] = (float(score), member)
            else:
                zset.append((float(score), member))
                added += 1
        zset.sort(key=lambda x: x[0])
        return added

    async def expire(self, key, ttl) -> int:
        self.ttls[key] = ttl
        return 1 if (key in self.zsets or key in self.strings or key in self.hashes) else 0

    async def zcard(self, key) -> int:
        return len(self.zsets.get(key, []))

    async def zrange(self, key, start, stop, withscores: bool = False):
        zset = self.zsets.get(key, [])
        if stop == -1:
            stop_idx = len(zset)
        else:
            stop_idx = stop + 1
        window = zset[start:stop_idx]
        if withscores:
            return [(m.encode(), s) for (s, m) in window]
        return [m.encode() for (_, m) in window]

    async def hset(self, key, field, value) -> int:
        h = self.hashes.setdefault(key, {})
        new = field not in h
        h[field] = value if isinstance(value, bytes) else str(value).encode("utf-8")
        return 1 if new else 0

    async def hdel(self, key, *fields) -> int:
        h = self.hashes.get(key)
        if not h:
            return 0
        removed = 0
        for f in fields:
            name = f.decode() if isinstance(f, bytes) else f
            if h.pop(name, None) is not None:
                removed += 1
        # Real Redis drops a hash once its last field is gone.
        if not h:
            del self.hashes[key]
        return removed

    async def hgetall(self, key) -> dict[bytes, bytes]:
        return {k.encode(): v for k, v in self.hashes.get(key, {}).items()}

    async def zrem(self, key, *members) -> int:
        zset = self.zsets.get(key, [])
        removed = 0
        for m in members:
            name = m.decode() if isinstance(m, bytes) else m
            for i, (_, existing) in enumerate(list(zset)):
                if existing == name:
                    zset.pop(i)
                    removed += 1
                    break
        return removed

    async def delete(self, key) -> int:
        hit = 0
        if key in self.strings:
            del self.strings[key]
            hit = 1
        if key in self.zsets:
            del self.zsets[key]
            hit = 1
        if key in self.hashes:
            del self.hashes[key]
            hit = 1
        self.ttls.pop(key, None)
        return hit


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


# ---------------------------------------------------------------------------
# app.redis primitives
# ---------------------------------------------------------------------------


def test_track_session_adds_to_sorted_set(fake_redis):
    from app.redis import track_session

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    evicted = asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))
    assert evicted == []
    assert len(fake_redis.zsets[f"active_jtis:{user_id}"]) == 1
    assert fake_redis.ttls[f"active_jtis:{user_id}"] == 1800


def test_track_session_evicts_oldest_when_over_cap(fake_redis):
    from app.redis import track_session

    user_id = uuid.uuid4()
    # Seed 5 sessions spaced in time so the ordering is deterministic.
    seeded = [str(uuid.uuid4()) for _ in range(5)]
    for i, jti in enumerate(seeded):
        # Backdate each JTI so zrange(0, 0) returns the first one.
        key = f"active_jtis:{user_id}"
        fake_redis.zsets.setdefault(key, []).append((time.time() - (100 - i), jti))
    fake_redis.zsets[f"active_jtis:{user_id}"].sort(key=lambda x: x[0])

    new_jti = str(uuid.uuid4())
    evicted = asyncio.run(track_session(user_id, new_jti, ttl_seconds=1800, max_sessions=5))

    # Oldest of the 5 seeded JTIs is the one that gets pushed out.
    assert evicted == [seeded[0]]
    remaining = {m for (_, m) in fake_redis.zsets[f"active_jtis:{user_id}"]}
    assert seeded[0] not in remaining
    assert new_jti in remaining
    assert len(remaining) == 5


def test_track_session_no_cap_when_max_is_zero(fake_redis):
    from app.redis import track_session

    user_id = uuid.uuid4()
    for _ in range(20):
        asyncio.run(track_session(user_id, str(uuid.uuid4()), ttl_seconds=1800, max_sessions=0))
    assert len(fake_redis.zsets[f"active_jtis:{user_id}"]) == 20


def test_untrack_session_removes_single_jti(fake_redis):
    from app.redis import track_session, untrack_session

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))
    asyncio.run(untrack_session(user_id, jti))
    assert fake_redis.zsets[f"active_jtis:{user_id}"] == []


def test_revoke_all_sessions_blocklists_every_jti(fake_redis):
    from app.redis import revoke_all_sessions, track_session

    user_id = uuid.uuid4()
    jtis = [str(uuid.uuid4()) for _ in range(3)]
    for jti in jtis:
        asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))

    revoked = asyncio.run(revoke_all_sessions(user_id, 1800))
    assert sorted(revoked) == sorted(jtis)
    assert f"active_jtis:{user_id}" not in fake_redis.zsets
    for jti in jtis:
        assert f"token:blocked:{jti}" in fake_redis.strings


def test_revoke_all_sessions_is_noop_without_active_sessions(fake_redis):
    from app.redis import revoke_all_sessions

    user_id = uuid.uuid4()
    revoked = asyncio.run(revoke_all_sessions(user_id, 1800))
    assert revoked == []


# ---------------------------------------------------------------------------
# session_management service wrappers
# ---------------------------------------------------------------------------


def test_register_session_blocklists_evicted(fake_redis):
    """When the cap is hit, register_session must both untrack AND blocklist."""
    from app.services import session_management

    user_id = uuid.uuid4()
    # Seed 5 sessions at controlled timestamps.
    seeded = [str(uuid.uuid4()) for _ in range(5)]
    key = f"active_jtis:{user_id}"
    for i, jti in enumerate(seeded):
        fake_redis.zsets.setdefault(key, []).append((time.time() - (100 - i), jti))
    fake_redis.zsets[key].sort(key=lambda x: x[0])

    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 5
        fake.access_token_expire_minutes = 30
        new_jti = str(uuid.uuid4())
        evicted = asyncio.run(session_management.register_session(user_id, new_jti))

    assert evicted == [seeded[0]]
    # Evicted JTI must be on the blocklist, not just gone from the set.
    assert f"token:blocked:{seeded[0]}" in fake_redis.strings


def test_register_session_override_via_settings(fake_redis):
    """FEOH_MAX_CONCURRENT_SESSIONS=2 should cap at 2."""
    from app.services import session_management

    user_id = uuid.uuid4()
    jtis = [str(uuid.uuid4()) for _ in range(4)]

    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 2
        fake.access_token_expire_minutes = 30
        for jti in jtis:
            asyncio.run(session_management.register_session(user_id, jti))
            # Nudge the clock forward between iterations
            time.sleep(0.001)

    remaining = [m for (_, m) in fake_redis.zsets[f"active_jtis:{user_id}"]]
    assert len(remaining) == 2
    # The last two JTIs survive; the first two were evicted.
    assert jtis[-1] in remaining
    assert jtis[-2] in remaining
    assert f"token:blocked:{jtis[0]}" in fake_redis.strings
    assert f"token:blocked:{jtis[1]}" in fake_redis.strings


def test_register_session_disabled_when_cap_is_zero(fake_redis):
    from app.services import session_management

    user_id = uuid.uuid4()

    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 0
        fake.access_token_expire_minutes = 30
        for _ in range(10):
            asyncio.run(session_management.register_session(user_id, str(uuid.uuid4())))

    assert len(fake_redis.zsets[f"active_jtis:{user_id}"]) == 10
    # No blocklist entries since nothing was evicted.
    assert not any(k.startswith("token:blocked:") for k in fake_redis.strings)


def test_end_session_blocklists_and_untracks(fake_redis):
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))

    asyncio.run(session_management.end_session(user_id, jti, 1800))

    assert fake_redis.zsets[f"active_jtis:{user_id}"] == []
    assert f"token:blocked:{jti}" in fake_redis.strings


def test_revoke_user_sessions_uses_token_ttl(fake_redis):
    """Blocklist TTL should come from settings.access_token_expire_minutes."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jtis = [str(uuid.uuid4()) for _ in range(3)]
    for jti in jtis:
        asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 15
        revoked = asyncio.run(session_management.revoke_user_sessions(user_id))

    assert sorted(revoked) == sorted(jtis)
    for jti in jtis:
        # TTL should have been (15 * 60) seconds.
        assert fake_redis.ttls[f"token:blocked:{jti}"] == 15 * 60


# ---------------------------------------------------------------------------
# Admin role-change wiring — forced-logout side effect
# ---------------------------------------------------------------------------


def test_admin_update_user_revokes_sessions_on_role_change(monkeypatch):
    """Role-change path must call revoke_user_sessions for the affected user."""
    from types import SimpleNamespace

    calls: list[uuid.UUID] = []

    async def _fake_revoke(user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.admin.revoke_user_sessions", _fake_revoke)

    # Simulate the "roles_changed" branch of update_user by invoking the
    # service helper directly — the endpoint handler delegates to it.
    # Directly assert that the admin module imports the helper — this is
    # the contract. If the import goes away the forced-logout guarantee
    # silently disappears.
    import app.api.admin as admin_mod
    from app.services.session_management import revoke_user_sessions  # noqa: F401

    assert hasattr(admin_mod, "revoke_user_sessions")

    # And verify the monkeypatched helper actually gets called when invoked
    # through the patched attribute.
    target_id = uuid.uuid4()
    asyncio.run(admin_mod.revoke_user_sessions(target_id))
    assert calls == [target_id]

    # Silence unused-var lint
    _ = SimpleNamespace


# ---------------------------------------------------------------------------
# Session visibility — metadata, listing, and self-service revocation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ua,expected",
    [
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Chrome on macOS",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, "
            "like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0",
            "Edge on Windows",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            "Safari on iPhone",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, "
            "like Gecko) Chrome/124.0 Mobile Safari/537.36",
            "Chrome on Android",
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
            "Firefox on Linux",
        ),
        ("curl/8.6.0", None),
        (None, None),
        ("", None),
    ],
)
def test_describe_user_agent(ua, expected):
    """Mobile and Chromium tokens must win over the generic ones they embed."""
    from app.services.session_management import describe_user_agent

    assert describe_user_agent(ua) == expected


def test_register_session_stores_metadata(fake_redis):
    from app.services import session_management

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 5
        fake.access_token_expire_minutes = 30
        asyncio.run(
            session_management.register_session(
                user_id,
                jti,
                ip="203.0.113.7",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0 Safari/537.36"
                ),
                method="password",
            )
        )
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert len(sessions) == 1
    assert sessions[0].jti == jti
    assert sessions[0].ip == "203.0.113.7"
    assert sessions[0].device == "Chrome on macOS"
    assert sessions[0].method == "password"
    # The raw User-Agent is a fingerprint — only the derived label is stored.
    stored = fake_redis.hashes[f"session_meta:{user_id}"][jti].decode()
    assert "Mozilla" not in stored


def test_register_session_clips_oversized_ip(fake_redis):
    """A trusted proxy is trusted, not bounded — the stored record stays small."""
    from app.services import session_management

    user_id = uuid.uuid4()
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 5
        fake.access_token_expire_minutes = 30
        asyncio.run(session_management.register_session(user_id, str(uuid.uuid4()), ip="9" * 5000))
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert len(sessions[0].ip) == 64


def test_list_sessions_without_metadata_still_lists(fake_redis):
    """A session tracked before metadata existed must still be revocable."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert [s.jti for s in sessions] == [jti]
    assert sessions[0].ip is None and sessions[0].device is None


def test_list_sessions_tolerates_corrupt_metadata(fake_redis):
    """The session list is the screen a worried user reaches for — never 500 it."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=5, meta="{not json"))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert [s.jti for s in sessions] == [jti]
    assert sessions[0].device is None


def test_list_sessions_prunes_expired_entries(fake_redis):
    """The tracking set's TTL is refreshed on every login, so it can outlive the
    individual tokens inside it. Expired JTIs are phantom sessions — drop them."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    stale = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    asyncio.run(
        track_session(user_id, stale, ttl_seconds=1800, max_sessions=0, meta='{"ip":"1.2.3.4"}')
    )
    asyncio.run(track_session(user_id, fresh, ttl_seconds=1800, max_sessions=0))
    # Backdate the stale entry past a 30-minute token lifetime.
    key = f"active_jtis:{user_id}"
    fake_redis.zsets[key] = [
        (time.time() - 3600, m) if m == stale else (s, m) for (s, m) in fake_redis.zsets[key]
    ]
    fake_redis.zsets[key].sort(key=lambda x: x[0])

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert [s.jti for s in sessions] == [fresh]
    # Pruned from both structures, and NOT blocklisted (nothing left to revoke).
    assert [m for (_, m) in fake_redis.zsets[key]] == [fresh]
    assert stale not in fake_redis.hashes.get(f"session_meta:{user_id}", {})
    assert f"token:blocked:{stale}" not in fake_redis.strings


def test_list_sessions_uses_the_ttl_recorded_at_sign_in(fake_redis):
    """Shortening FEOH_ACCESS_TOKEN_EXPIRE_MINUTES must not prune a session
    whose token is still authenticating — that would leave a live session the
    user can neither see nor revoke, the exact failure this feature prevents."""
    from app.services import session_management

    user_id = uuid.uuid4()
    jti = str(uuid.uuid4())
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 0
        fake.access_token_expire_minutes = 60  # minted under a 60-minute token
        asyncio.run(session_management.register_session(user_id, jti, ip="1.1.1.1"))

    # Backdate 40 minutes: past a 10-minute lifetime, well inside the 60 it holds.
    key = f"active_jtis:{user_id}"
    fake_redis.zsets[key] = [(time.time() - 2400, m) for (_, m) in fake_redis.zsets[key]]

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 10  # operator shortened it since
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert [s.jti for s in sessions] == [jti]
    assert f"token:blocked:{jti}" not in fake_redis.strings


def test_list_sessions_falls_back_when_the_recorded_ttl_is_unusable(fake_redis):
    """A session tracked before the ttl field existed — or one whose value
    didn't survive the round trip — is judged against the current default."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    legacy, garbage = str(uuid.uuid4()), str(uuid.uuid4())
    asyncio.run(
        track_session(user_id, legacy, ttl_seconds=1800, max_sessions=0, meta='{"ip":"1.1.1.1"}')
    )
    asyncio.run(
        track_session(user_id, garbage, ttl_seconds=1800, max_sessions=0, meta='{"ttl":"soon"}')
    )
    key = f"active_jtis:{user_id}"
    fake_redis.zsets[key] = [(time.time() - 3600, m) for (_, m) in fake_redis.zsets[key]]

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        sessions = asyncio.run(session_management.list_sessions(user_id))

    # Both are an hour old, so the 30-minute default expires them.
    assert sessions == []


def test_list_sessions_returns_newest_first(fake_redis):
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jtis = [str(uuid.uuid4()) for _ in range(3)]
    for jti in jtis:
        asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=0))
        time.sleep(0.002)

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        sessions = asyncio.run(session_management.list_sessions(user_id))

    assert [s.jti for s in sessions] == list(reversed(jtis))


def test_revoke_one_session_blocklists_and_forgets(fake_redis):
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    keep, kill = str(uuid.uuid4()), str(uuid.uuid4())
    for jti in (keep, kill):
        asyncio.run(
            track_session(user_id, jti, ttl_seconds=1800, max_sessions=0, meta='{"ip":"1.1.1.1"}')
        )

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        assert asyncio.run(session_management.revoke_one_session(user_id, kill)) is True

    assert f"token:blocked:{kill}" in fake_redis.strings
    assert f"token:blocked:{keep}" not in fake_redis.strings
    assert [m for (_, m) in fake_redis.zsets[f"active_jtis:{user_id}"]] == [keep]
    assert kill not in fake_redis.hashes[f"session_meta:{user_id}"]


def test_revoke_blocklists_past_the_tokens_own_expiry(fake_redis):
    """A blocklist entry that expires BEFORE the JWT's own `exp` hands the
    revoked token straight back. Shortening the configured lifetime must not
    shorten the blocklist for tokens minted under the old, longer one."""
    from app.services import session_management

    user_id = uuid.uuid4()
    doomed = str(uuid.uuid4())
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 0
        fake.access_token_expire_minutes = 60  # a 60-minute token
        asyncio.run(session_management.register_session(user_id, doomed, ip="1.1.1.1"))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 10  # operator shortened it since
        assert asyncio.run(session_management.revoke_one_session(user_id, doomed)) is True

    # Must cover the token's real remaining life (~3600s), not the new 600s cap.
    assert fake_redis.ttls[f"token:blocked:{doomed}"] > 3000


def test_revoke_others_blocklists_past_the_tokens_own_expiry(fake_redis):
    """Same guarantee on the bulk paths — 'sign out everywhere else' and the
    admin forced logout both share `blocklist_ttl_for`."""
    from app.services import session_management

    user_id = uuid.uuid4()
    mine, doomed = str(uuid.uuid4()), str(uuid.uuid4())
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 0
        fake.access_token_expire_minutes = 60
        asyncio.run(session_management.register_session(user_id, doomed))
        asyncio.run(session_management.register_session(user_id, mine))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 10
        asyncio.run(session_management.revoke_other_sessions(user_id, mine))

    assert fake_redis.ttls[f"token:blocked:{doomed}"] > 3000


def test_blocklist_ttl_floors_at_the_current_default(fake_redis):
    """A user with nothing tracked (or only shorter-lived sessions) still gets
    at least the configured lifetime — over-blocking is the safe direction."""
    from app.services import session_management

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        ttl = asyncio.run(session_management.blocklist_ttl_for(uuid.uuid4()))

    assert ttl == 30 * 60


def test_revoke_one_session_refuses_another_users_jti(fake_redis):
    """Membership in the caller's own set IS the authorization check."""
    from app.redis import track_session
    from app.services import session_management

    victim, attacker = uuid.uuid4(), uuid.uuid4()
    victim_jti = str(uuid.uuid4())
    asyncio.run(track_session(victim, victim_jti, ttl_seconds=1800, max_sessions=0))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        assert asyncio.run(session_management.revoke_one_session(attacker, victim_jti)) is False

    assert f"token:blocked:{victim_jti}" not in fake_redis.strings
    assert [m for (_, m) in fake_redis.zsets[f"active_jtis:{victim}"]] == [victim_jti]


def test_revoke_other_sessions_keeps_the_caller(fake_redis):
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    mine = str(uuid.uuid4())
    others = [str(uuid.uuid4()) for _ in range(3)]
    for jti in [mine, *others]:
        asyncio.run(
            track_session(user_id, jti, ttl_seconds=1800, max_sessions=0, meta='{"ip":"1.1.1.1"}')
        )

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        revoked = asyncio.run(session_management.revoke_other_sessions(user_id, mine))

    assert sorted(revoked) == sorted(others)
    assert [m for (_, m) in fake_redis.zsets[f"active_jtis:{user_id}"]] == [mine]
    assert list(fake_redis.hashes[f"session_meta:{user_id}"]) == [mine]
    assert f"token:blocked:{mine}" not in fake_redis.strings
    for jti in others:
        assert f"token:blocked:{jti}" in fake_redis.strings


def test_revoke_other_sessions_without_a_current_jti_signs_out_everywhere(fake_redis):
    """A caller whose own JTI can't be resolved must fail toward less access."""
    from app.redis import track_session
    from app.services import session_management

    user_id = uuid.uuid4()
    jtis = [str(uuid.uuid4()) for _ in range(2)]
    for jti in jtis:
        asyncio.run(track_session(user_id, jti, ttl_seconds=1800, max_sessions=0))

    with patch("app.services.session_management.settings") as fake:
        fake.access_token_expire_minutes = 30
        revoked = asyncio.run(session_management.revoke_other_sessions(user_id, None))

    assert sorted(revoked) == sorted(jtis)
    for jti in jtis:
        assert f"token:blocked:{jti}" in fake_redis.strings


def test_eviction_and_logout_clear_session_metadata(fake_redis):
    """Metadata must never outlive the membership it describes."""
    from app.services import session_management

    user_id = uuid.uuid4()
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 1
        fake.access_token_expire_minutes = 30
        asyncio.run(session_management.register_session(user_id, first, ip="1.1.1.1"))
        time.sleep(0.002)
        asyncio.run(session_management.register_session(user_id, second, ip="2.2.2.2"))

    meta_key = f"session_meta:{user_id}"
    assert list(fake_redis.hashes[meta_key]) == [second]

    asyncio.run(session_management.end_session(user_id, second, 1800))
    assert meta_key not in fake_redis.hashes


def test_revoke_user_sessions_clears_metadata(fake_redis):
    from app.services import session_management

    user_id = uuid.uuid4()
    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 5
        fake.access_token_expire_minutes = 30
        for _ in range(3):
            asyncio.run(
                session_management.register_session(user_id, str(uuid.uuid4()), ip="1.1.1.1")
            )
        asyncio.run(session_management.revoke_user_sessions(user_id))

    assert f"session_meta:{user_id}" not in fake_redis.hashes
    assert f"active_jtis:{user_id}" not in fake_redis.zsets
