"""Rate-limit security tests — sliding-window enforcement on the
unauthenticated abuse surfaces (signup, captcha).

The limiter is a Redis sorted-set per (endpoint, client_ip). Each
request adds a timestamp and trims anything older than the window.
A 429 is raised once `count > limit`.

Tests:
  - Under the cap: every request passes
  - At the cap: the next request 429s with a Retry-After header
  - The Retry-After is bounded by the window
  - Different IPs have independent buckets (no global lockout)
  - Different endpoints have independent buckets (signup vs captcha)
  - Trim drops entries older than the window so the bucket recovers
  - Forwarded-for header is honoured so we don't bucket every user
    behind an ALB into one global limit
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeSortedSet:
    """In-memory stand-in for Redis sorted-set semantics."""

    def __init__(self):
        # key -> list of (member, score)
        self.store: dict[str, list[tuple[str, float]]] = {}

    def zadd(self, key, mapping):
        self.store.setdefault(key, [])
        for member, score in mapping.items():
            self.store[key].append((member, score))

    def zremrangebyscore(self, key, min_score, max_score):
        if key in self.store:
            self.store[key] = [
                (m, s) for m, s in self.store[key] if not (min_score <= s <= max_score)
            ]

    def zcard(self, key):
        return len(self.store.get(key, []))

    def zrange(self, key, start, stop, withscores=False):
        items = sorted(self.store.get(key, []), key=lambda t: t[1])
        slice_ = items[start : stop + 1 if stop >= 0 else None]
        if withscores:
            return [(m, s) for m, s in slice_]
        return [m for m, _ in slice_]


class _FakePipeline:
    def __init__(self, sset: _FakeSortedSet):
        self._sset = sset
        self._calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def zremrangebyscore(self, key, low, high):
        self._calls.append(("zremrangebyscore", key, low, high))

    def zadd(self, key, mapping):
        self._calls.append(("zadd", key, mapping))

    def zcard(self, key):
        self._calls.append(("zcard", key))

    def expire(self, key, ttl):
        self._calls.append(("expire", key, ttl))

    async def execute(self):
        # Apply in order; return list of results matching the pipeline shape.
        results = []
        for call in self._calls:
            op = call[0]
            if op == "zremrangebyscore":
                self._sset.zremrangebyscore(call[1], call[2], call[3])
                results.append(None)
            elif op == "zadd":
                self._sset.zadd(call[1], call[2])
                results.append(None)
            elif op == "zcard":
                results.append(self._sset.zcard(call[1]))
            elif op == "expire":
                results.append(True)
        return results


class _FakeRedis:
    def __init__(self):
        self.sset = _FakeSortedSet()

    def pipeline(self, transaction: bool = True):  # noqa: ARG002
        return _FakePipeline(self.sset)

    async def zrange(self, key, start, stop, withscores=False):
        return self.sset.zrange(key, start, stop, withscores=withscores)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.rate_limit.get_redis", _get_redis)
    return fake


def _fake_request(ip: str = "203.0.113.1", forwarded: str | None = None):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = ip
    req.headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return req


# ---------------------------------------------------------------------------
# Under / at / over the limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rate_limit_passes_under_the_cap(fake_redis):
    """Five attempts at a limit of 5 must all pass — the limit is
    inclusive (`count > limit` raises, so count == limit is fine)."""
    from app.services.rate_limit import check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=3600)


@pytest.mark.asyncio
async def test_check_rate_limit_raises_429_over_the_cap(fake_redis):
    """The sixth attempt within the window must 429."""
    from app.services.rate_limit import RateLimitExceeded, check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=3600)

    with pytest.raises(RateLimitExceeded) as exc:
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=3600)
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_429_includes_retry_after_header(fake_redis):
    """The 429 must carry a Retry-After header so well-behaved
    clients back off. Without it, automated retries hammer the
    server harder than they would with respect for the hint."""
    from app.services.rate_limit import RateLimitExceeded, check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as exc:
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)
    assert "Retry-After" in exc.value.headers
    # The hint must be a positive int seconds — not a float, not 0.
    retry_after = int(exc.value.headers["Retry-After"])
    assert 1 <= retry_after <= 60


@pytest.mark.asyncio
async def test_rate_limit_429_is_a_coded_refusal_stating_the_wait(fake_redis):
    """The 429's detail is the coded `rate_limited` refusal, so a client states
    the wait in the reader's language (issue #635 finding C). The wait it names
    is the Retry-After header's own figure, and the English message keeps the
    sentence a client that reads `detail` as text has always shown."""
    from app.services.rate_limit import RATE_LIMITED, RateLimitExceeded, check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)
    with pytest.raises(RateLimitExceeded) as exc:
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)
    detail = exc.value.detail
    wait = int(exc.value.headers["Retry-After"])
    assert detail == {
        "code": RATE_LIMITED,
        "message": f"Too many requests. Please wait {wait} seconds before trying again.",
        "params": {"retry_after_seconds": wait},
    }


def test_rate_limit_refusal_carries_nothing_beyond_the_wait():
    """Which bucket tripped, its limit and its count stay server-side: the
    params are exactly the Retry-After figure, for every limiter."""
    from app.services.rate_limit import RateLimitExceeded

    exc = RateLimitExceeded(42)
    assert exc.status_code == 429
    assert exc.headers == {"Retry-After": "42"}
    assert set(exc.detail) == {"code", "message", "params"}
    assert exc.detail["params"] == {"retry_after_seconds": 42}


# ---------------------------------------------------------------------------
# Bucket isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_buckets_per_ip(fake_redis):
    """One noisy IP must not lock out another. Hammer one IP to the
    cap; a different IP's first attempt should still succeed."""
    from app.services.rate_limit import check_rate_limit

    for _ in range(5):
        await check_rate_limit(
            "signup",
            _fake_request(ip="203.0.113.1"),
            limit=5,
            window_seconds=3600,
        )
    # Different IP — independent bucket, should pass.
    await check_rate_limit(
        "signup",
        _fake_request(ip="198.51.100.42"),
        limit=5,
        window_seconds=3600,
    )


@pytest.mark.asyncio
async def test_rate_limit_buckets_per_endpoint(fake_redis):
    """Hitting the signup limit doesn't lock out captcha — endpoints
    are independent buckets. The key prefix carries the endpoint
    name."""
    from app.services.rate_limit import check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=3600)
    # Same IP, different endpoint — fresh bucket.
    await check_rate_limit("captcha", _fake_request(), limit=5, window_seconds=3600)


@pytest.mark.asyncio
async def test_rate_limit_recovers_when_entries_age_out(fake_redis):
    """If we mark the existing entries as older than the window, the
    next request should pass — the trim must be effective."""
    from app.services.rate_limit import check_rate_limit

    for _ in range(5):
        await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)

    # Manually walk every entry in the bucket backwards by 120s.
    bucket = next(iter(fake_redis.sset.store.values()))
    fake_redis.sset.store[next(iter(fake_redis.sset.store))] = [(m, s - 120) for m, s in bucket]

    # Under the cap again because the previous entries are stale.
    await check_rate_limit("signup", _fake_request(), limit=5, window_seconds=60)


# ---------------------------------------------------------------------------
# Forwarded-for header — ALB clients map to the real client IP
# ---------------------------------------------------------------------------


@pytest.fixture
def _trust_alb_proxy(monkeypatch):
    """Tell the limiter to trust XFF from the ALB-shaped 10.0.0.0/8 net."""
    from app.config import settings

    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "10.0.0.0/8")


@pytest.mark.asyncio
async def test_rate_limit_uses_forwarded_for_when_proxy_is_trusted(fake_redis, _trust_alb_proxy):
    """Behind an ALB *that's in the trusted_proxy_cidrs list*, the
    original client IP arrives in X-Forwarded-For. The limiter buckets
    by that — not by the ALB's address — otherwise every customer
    collapses into a single global counter and 429s the whole tenant
    at once."""
    from app.services.rate_limit import check_rate_limit

    # ALB at 10.0.0.5, real client A at 198.51.100.10, client B at 198.51.100.20.
    for _ in range(5):
        await check_rate_limit(
            "signup",
            _fake_request(ip="10.0.0.5", forwarded="198.51.100.10"),
            limit=5,
            window_seconds=3600,
        )
    # Client B (same ALB) must still pass — separate per-client bucket.
    await check_rate_limit(
        "signup",
        _fake_request(ip="10.0.0.5", forwarded="198.51.100.20"),
        limit=5,
        window_seconds=3600,
    )


@pytest.mark.asyncio
async def test_rate_limit_picks_first_forwarded_for_hop(fake_redis, _trust_alb_proxy):
    """``X-Forwarded-For: client, proxy1, proxy2`` — the limiter must
    bucket on the leftmost (real client) value, not the rightmost
    (most-trusted-proxy). A regression that flipped this would let an
    attacker rotate proxies freely."""
    from app.services.rate_limit import check_rate_limit

    for _ in range(5):
        await check_rate_limit(
            "signup",
            _fake_request(ip="10.0.0.5", forwarded="198.51.100.10, 10.0.0.1, 10.0.0.2"),
            limit=5,
            window_seconds=3600,
        )


@pytest.mark.asyncio
async def test_rate_limit_ignores_xff_from_untrusted_peer(fake_redis):
    """When the connecting peer is NOT in ``trusted_proxy_cidrs``, the
    limiter must ignore ``X-Forwarded-For`` and bucket on the real peer
    IP. Otherwise a direct attacker rotates through arbitrary spoofed
    XFF values to dodge per-IP limits.
    """
    from app.services.rate_limit import check_rate_limit

    # Default settings → no trusted proxies. The attacker connects from
    # 203.0.113.1 and claims XFF=198.51.100.10. We must bucket on
    # 203.0.113.1, so a rotating XFF doesn't help them.
    for _ in range(5):
        await check_rate_limit(
            "auth_login",
            _fake_request(ip="203.0.113.1", forwarded="198.51.100.10"),
            limit=5,
            window_seconds=3600,
        )
    # Same peer, different forged XFF — must still 429 on the 6th hit
    # because the bucket is keyed on the real peer (203.0.113.1), not
    # the spoofed XFF value.
    from app.services.rate_limit import RateLimitExceeded

    with pytest.raises(RateLimitExceeded):
        await check_rate_limit(
            "auth_login",
            _fake_request(ip="203.0.113.1", forwarded="1.2.3.4"),
            limit=5,
            window_seconds=3600,
        )


# ---------------------------------------------------------------------------
# Signup config — the cap is small enough to throttle abuse
# ---------------------------------------------------------------------------


def test_signup_rate_limit_default_is_modest():
    """`FEOH_SIGNUP_RATE_LIMIT_PER_HOUR` defaults to 5. A regression
    that raised it to e.g. 100 would let a single IP create a
    storm of half-provisioned tenants before the limiter kicked in."""
    from app.config import settings

    assert settings.signup_rate_limit_per_hour <= 10, (
        f"signup rate limit default ({settings.signup_rate_limit_per_hour}/h) is too loose; "
        f"5/h is the documented default"
    )


# ---------------------------------------------------------------------------
# resolve_client_ip — the shared resolver the audit / captcha sites use
# ---------------------------------------------------------------------------


def test_resolve_client_ip_none_without_request_or_peer():
    from app.services.rate_limit import resolve_client_ip

    assert resolve_client_ip(None) is None
    req = MagicMock()
    req.client = None
    assert resolve_client_ip(req) is None


def test_resolve_client_ip_ignores_xff_from_untrusted_peer():
    from app.services.rate_limit import resolve_client_ip

    resolved = resolve_client_ip(_fake_request(ip="203.0.113.1", forwarded="198.51.100.10"))
    assert resolved == "203.0.113.1"


def test_resolve_client_ip_honours_xff_behind_trusted_proxy(_trust_alb_proxy):
    from app.services.rate_limit import resolve_client_ip

    resolved = resolve_client_ip(_fake_request(ip="10.0.0.5", forwarded="198.51.100.10, 10.0.0.1"))
    assert resolved == "198.51.100.10"


def test_auth_audit_ip_goes_through_the_shared_resolver(_trust_alb_proxy):
    """Login/signup/SSO audit rows must record the same client the limiter
    buckets on — behind a trusted proxy (Caddy on the single-VM stack, the
    ALB on ECS), raw request.client.host is the proxy's address."""
    from app.api import auth

    trusted = auth._client_ip(_fake_request(ip="10.0.0.5", forwarded="198.51.100.10"))
    assert trusted == "198.51.100.10"
    # Untrusted direct caller: the spoofable header must stay ignored.
    untrusted = auth._client_ip(_fake_request(ip="203.0.113.1", forwarded="198.51.100.10"))
    assert untrusted == "203.0.113.1"
