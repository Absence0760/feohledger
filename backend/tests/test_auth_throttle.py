"""Per-identity authentication-failure throttle.

The per-IP limiter (`test_rate_limit_security.py`) stops one host hammering an
endpoint. It is blind to the shape these tests cover: ONE account attacked from
many source addresses, each staying under the per-IP cap. The primitives under
test add the missing axis — a failure budget keyed on the identity being
authenticated, shared across every source IP.

Covered here:
  - the pure key derivation (case/whitespace normalisation, multi-part
    tenant scoping, no raw identifier in the key)
  - check / record / clear semantics, including "checking never records"
  - the window rolls off
  - the employee login + MFA-verify wiring in `app/api/auth.py`, incl. the
    two properties that make it safe to sit in front of a login form: an
    unknown address throttles identically (no existence oracle), and a correct
    password clears the budget.
  - the same wiring on the supplier portal (`app/api/portal_auth.py`), whose
    buckets are additionally tenant-scoped.

Both surfaces live here rather than in a `test_portal_*` twin: they are two
call sites of ONE contract, and splitting them would mean maintaining a second
copy of the Redis fake and letting the two halves drift.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

# Mock-DB tests: login writes an auth audit row through its own control-plane/tenant sessions
# (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("no_auth_audit_write")

# ---------------------------------------------------------------------------
# Redis fake (sorted-set semantics + DEL), mirroring test_rate_limit_security
# ---------------------------------------------------------------------------


class _FakeSortedSet:
    def __init__(self):
        self.store: dict[str, list[tuple[str, float]]] = {}

    def zadd(self, key, mapping):
        self.store.setdefault(key, [])
        for member, score in mapping.items():
            self.store[key].append((member, score))

    def zremrangebyscore(self, key, low, high):
        if key in self.store:
            self.store[key] = [(m, s) for m, s in self.store[key] if not (low <= s <= high)]

    def zcard(self, key):
        return len(self.store.get(key, []))

    def zrange(self, key, start, stop, withscores=False):
        items = sorted(self.store.get(key, []), key=lambda t: t[1])
        slice_ = items[start : stop + 1 if stop >= 0 else None]
        if withscores:
            return list(slice_)
        return [m for m, _ in slice_]

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


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

    async def delete(self, *keys):
        return sum(self.sset.delete(k) for k in keys)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.rate_limit.get_redis", _get_redis)
    return fake


def _fake_request(ip: str = "203.0.113.1"):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = ip
    req.headers = {}
    return req


# ---------------------------------------------------------------------------
# auth_identity_key — the pure part
# ---------------------------------------------------------------------------


def test_identity_key_never_contains_the_raw_identifier():
    """An email must not survive into the Redis keyspace."""
    from app.services.rate_limit import auth_identity_key

    key = auth_identity_key("ada@example.com")
    assert "ada" not in key
    assert "@" not in key
    assert len(key) == 32


def test_identity_key_normalises_case_and_whitespace():
    """Otherwise the throttle is side-stepped by varying capitalisation."""
    from app.services.rate_limit import auth_identity_key

    assert auth_identity_key("Ada@Example.COM") == auth_identity_key("  ada@example.com ")


def test_identity_key_is_scoped_by_every_part():
    """Two tenants' suppliers can share an address — they must not share a
    budget, or one tenant's traffic throttles the other's supplier."""
    from app.services.rate_limit import auth_identity_key

    assert auth_identity_key("acme", "ada@example.com") != auth_identity_key(
        "techflow", "ada@example.com"
    )


# ---------------------------------------------------------------------------
# check / record / clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checking_never_records(fake_redis):
    """`check_auth_failures` is read-only: a user who signs in correctly a
    thousand times must never accumulate a budget."""
    from app.services.rate_limit import check_auth_failures

    for _ in range(50):
        await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)

    assert fake_redis.sset.store == {}


@pytest.mark.asyncio
async def test_throttles_once_the_limit_is_reached(fake_redis):
    """Recorded failures below the limit pass; the one that reaches it blocks
    the NEXT attempt (the check is `>= limit`, since it doesn't self-count)."""
    from app.services.rate_limit import check_auth_failures, record_auth_failure

    for _ in range(3):
        await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)
        await record_auth_failure("auth_login", "ident", window_seconds=900)

    with pytest.raises(HTTPException) as exc:
        await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers
    # The SAME coded refusal the per-IP cap answers with — an identity budget
    # must not be told apart from a request cap (nor a known account from an
    # unknown one) by its body. Only the wait varies, and it is the header's.
    assert exc.value.detail["code"] == "rate_limited"
    assert exc.value.detail["params"] == {
        "retry_after_seconds": int(exc.value.headers["Retry-After"])
    }


@pytest.mark.asyncio
async def test_budget_is_per_identity(fake_redis):
    """A spray against one account must not lock every other account out."""
    from app.services.rate_limit import check_auth_failures, record_auth_failure

    for _ in range(5):
        await record_auth_failure("auth_login", "victim", window_seconds=900)

    with pytest.raises(HTTPException):
        await check_auth_failures("auth_login", "victim", limit=3, window_seconds=900)

    # Untouched neighbour still passes.
    await check_auth_failures("auth_login", "bystander", limit=3, window_seconds=900)


@pytest.mark.asyncio
async def test_scopes_are_independent(fake_redis):
    """Burning the login budget must not lock the same person out of the MFA
    step (they are separate credentials with separate keyspaces)."""
    from app.services.rate_limit import check_auth_failures, record_auth_failure

    for _ in range(5):
        await record_auth_failure("auth_login", "ident", window_seconds=900)

    await check_auth_failures("auth_mfa", "ident", limit=3, window_seconds=900)


@pytest.mark.asyncio
async def test_success_clears_the_budget(fake_redis):
    """The right credential wipes earlier typos — a legitimate user is never
    locked out by their own fumbling, or by someone else's spray."""
    from app.services.rate_limit import (
        check_auth_failures,
        clear_auth_failures,
        record_auth_failure,
    )

    for _ in range(5):
        await record_auth_failure("auth_login", "ident", window_seconds=900)
    with pytest.raises(HTTPException):
        await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)

    await clear_auth_failures("auth_login", "ident")
    await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)


@pytest.mark.asyncio
async def test_window_rolls_off(fake_redis):
    """Failures older than the window are trimmed — the throttle is a rolling
    window, never a sticky lock."""
    import time

    from app.services.rate_limit import check_auth_failures, record_auth_failure

    for _ in range(5):
        await record_auth_failure("auth_login", "ident", window_seconds=900)
    with pytest.raises(HTTPException):
        await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)

    # Age every recorded entry past the window.
    stale = time.time() - 5_000
    for key, entries in fake_redis.sset.store.items():
        fake_redis.sset.store[key] = [(m, stale) for m, _ in entries]

    await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)


@pytest.mark.asyncio
async def test_noop_when_the_limiter_is_disabled(fake_redis, monkeypatch):
    """The master switch (CI e2e turns it off) must disable this too."""
    from app.config import settings
    from app.services.rate_limit import check_auth_failures, record_auth_failure

    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    for _ in range(50):
        await record_auth_failure("auth_login", "ident", window_seconds=900)
    await check_auth_failures("auth_login", "ident", limit=3, window_seconds=900)
    assert fake_redis.sset.store == {}


# ---------------------------------------------------------------------------
# Wiring — employee login
# ---------------------------------------------------------------------------


def _login_body(email="ada@example.com", password="wrong-password"):
    body = MagicMock()
    body.email = email
    body.password = password
    return body


def _db_returning(user):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    db.execute = AsyncMock(return_value=result)
    return db


def _user(*, password_hash="$2b$12$fake", org_id=None):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.organization_id = org_id or uuid.uuid4()
    u.hashed_password = password_hash
    u.is_active = True
    u.mfa_enabled = False
    u.must_change_password = False
    return u


@pytest.mark.asyncio
async def test_login_throttles_after_repeated_wrong_passwords(fake_redis, monkeypatch):
    """The distributed-spray case: every attempt arrives from a DIFFERENT IP,
    so the per-IP brake never fires. The per-account budget must still stop it."""
    from app.api import auth as auth_api
    from app.services.rate_limit import LOGIN_FAILURE_LIMIT

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    user = _user()
    for i in range(LOGIN_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await auth_api.login(
                _login_body(), _fake_request(ip=f"198.51.100.{i}"), db=_db_returning(user)
            )
        assert exc.value.status_code == 401, "should still be a plain credential rejection"

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(
            _login_body(), _fake_request(ip="198.51.100.250"), db=_db_returning(user)
        )
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_login_throttle_does_not_leak_account_existence(fake_redis, monkeypatch):
    """The 429 must arrive for an address with NO account too — otherwise the
    throttle itself becomes the enumeration oracle the 401 path avoids."""
    from app.api import auth as auth_api
    from app.services.rate_limit import LOGIN_FAILURE_LIMIT

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", AsyncMock())

    for i in range(LOGIN_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await auth_api.login(
                _login_body(email="nobody@example.com"),
                _fake_request(ip=f"198.51.100.{i}"),
                db=_db_returning(None),
            )
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(
            _login_body(email="nobody@example.com"),
            _fake_request(ip="198.51.100.250"),
            db=_db_returning(None),
        )
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_successful_login_clears_the_budget(fake_redis, monkeypatch):
    """Someone else's spray against your address must not keep you out once you
    supply the right password."""
    from app.api import auth as auth_api
    from app.services.rate_limit import LOGIN_FAILURE_LIMIT

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr(auth_api, "register_session", AsyncMock())
    monkeypatch.setattr(auth_api, "_load_user_org", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", False)

    user = _user()

    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)
    for i in range(LOGIN_FAILURE_LIMIT - 1):
        with pytest.raises(HTTPException):
            await auth_api.login(
                _login_body(), _fake_request(ip=f"198.51.100.{i}"), db=_db_returning(user)
            )

    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: True)
    token = await auth_api.login(_login_body(), _fake_request(), db=_db_returning(user))
    assert token.access_token

    # Budget wiped: a fresh run of wrong guesses gets the full allowance again.
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)
    for i in range(LOGIN_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await auth_api.login(
                _login_body(), _fake_request(ip=f"198.51.100.{i}"), db=_db_returning(user)
            )
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Wiring — employee MFA verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mfa_verify_throttles_totp_guessing(fake_redis, monkeypatch):
    """A 6-digit code is only safe if online guessing is capped per ACCOUNT.
    Per-IP alone lets an attacker holding the password (and a few hundred
    proxies) sweep a meaningful slice of the keyspace inside one challenge TTL.
    """
    from app.api import auth as auth_api
    from app.services.rate_limit import MFA_FAILURE_LIMIT

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", True)

    user = _user()
    user.mfa_enabled = True
    user.mfa_secret = "SECRET"

    claims = MagicMock()
    claims.subject_id = user.id
    claims.jti = "challenge-jti"
    monkeypatch.setattr(auth_api.mfa, "decode_challenge_token", AsyncMock(return_value=claims))
    monkeypatch.setattr(auth_api.mfa, "verify_totp", AsyncMock(return_value=False))

    body = MagicMock()
    body.challenge_token = "tok"
    body.method = "totp"
    body.code = "000000"

    for i in range(MFA_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await auth_api.verify_mfa(
                body, _fake_request(ip=f"198.51.100.{i}"), db=_db_returning(user)
            )
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await auth_api.verify_mfa(body, _fake_request(ip="198.51.100.250"), db=_db_returning(user))
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_mfa_verify_success_clears_the_budget(fake_redis, monkeypatch):
    from app.api import auth as auth_api
    from app.services.rate_limit import MFA_FAILURE_LIMIT, check_auth_failures

    monkeypatch.setattr(auth_api, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr(auth_api, "register_session", AsyncMock())
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", True)

    user = _user()
    user.mfa_enabled = True
    user.mfa_secret = "SECRET"

    claims = MagicMock()
    claims.subject_id = user.id
    claims.jti = "challenge-jti"
    monkeypatch.setattr(auth_api.mfa, "decode_challenge_token", AsyncMock(return_value=claims))
    monkeypatch.setattr(auth_api.mfa, "consume_challenge_token", AsyncMock())
    monkeypatch.setattr(auth_api.mfa, "verify_totp", AsyncMock(return_value=False))

    body = MagicMock()
    body.challenge_token = "tok"
    body.method = "totp"
    body.code = "000000"

    for i in range(MFA_FAILURE_LIMIT - 1):
        with pytest.raises(HTTPException):
            await auth_api.verify_mfa(
                body, _fake_request(ip=f"198.51.100.{i}"), db=_db_returning(user)
            )

    monkeypatch.setattr(auth_api.mfa, "verify_totp", AsyncMock(return_value=True))
    result = await auth_api.verify_mfa(body, _fake_request(), db=_db_returning(user))
    assert result.access_token

    from app.services.rate_limit import auth_identity_key

    await check_auth_failures(
        "auth_mfa",
        auth_identity_key(str(user.id)),
        limit=MFA_FAILURE_LIMIT,
        window_seconds=900,
    )


# ---------------------------------------------------------------------------
# Wiring — supplier portal
# ---------------------------------------------------------------------------


def _vendor_user(*, mfa_enabled=False, mfa_secret=None):
    vu = MagicMock()
    vu.id = uuid.uuid4()
    vu.email = "supplier@example.com"
    vu.vendor_id = uuid.uuid4()
    vu.organization_id = uuid.uuid4()
    vu.is_active = True
    vu.hashed_password = "$2b$12$fake"
    vu.must_change_password = False
    vu.mfa_enabled = mfa_enabled
    vu.mfa_secret = mfa_secret
    return vu


def _portal_db(vu):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = vu
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_portal_login_throttles_per_account(fake_redis, monkeypatch):
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest
    from app.services.rate_limit import LOGIN_FAILURE_LIMIT

    monkeypatch.setattr(portal_auth, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    vu = _vendor_user()
    body = PortalLoginRequest(email=vu.email, password="wrong-password")

    for i in range(LOGIN_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await portal_auth.portal_login(
                body=body,
                request=_fake_request(ip=f"198.51.100.{i}"),
                slug="acme",
                db=_portal_db(vu),
            )
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await portal_auth.portal_login(
            body=body,
            request=_fake_request(ip="198.51.100.250"),
            slug="acme",
            db=_portal_db(vu),
        )
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_portal_login_budget_is_tenant_scoped(fake_redis, monkeypatch):
    """A vendor address is unique only WITHIN a tenant DB. Keyed on the address
    alone, one tenant's traffic would throttle another tenant's supplier."""
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest
    from app.services.rate_limit import LOGIN_FAILURE_LIMIT

    monkeypatch.setattr(portal_auth, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    vu = _vendor_user()
    body = PortalLoginRequest(email=vu.email, password="wrong-password")

    for i in range(LOGIN_FAILURE_LIMIT):
        with pytest.raises(HTTPException):
            await portal_auth.portal_login(
                body=body,
                request=_fake_request(ip=f"198.51.100.{i}"),
                slug="acme",
                db=_portal_db(vu),
            )

    # Same address, different tenant — untouched budget, so still a 401.
    with pytest.raises(HTTPException) as exc:
        await portal_auth.portal_login(
            body=body,
            request=_fake_request(),
            slug="techflow",
            db=_portal_db(vu),
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_portal_login_records_a_failure_audit_row(fake_redis, monkeypatch):
    """A supplier brute-force used to leave no trace at all."""
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest

    # The failure row is QUEUED off the response path, not awaited — otherwise
    # a known supplier address costs a tenant-DB round trip an unknown one does
    # not (`tests/test_login_audit_off_response_path.py`). Same payload.
    audit = MagicMock()
    monkeypatch.setattr(portal_auth, "queue_auth_audit", audit)
    monkeypatch.setattr("app.utils.passwords.pwd_context.verify", lambda *_a, **_k: False)

    vu = _vendor_user()
    with pytest.raises(HTTPException):
        await portal_auth.portal_login(
            body=PortalLoginRequest(email=vu.email, password="wrong-password"),
            request=_fake_request(),
            slug="acme",
            db=_portal_db(vu),
        )

    audit.assert_called_once()
    kwargs = audit.call_args.kwargs
    assert kwargs["action"] == "portal.login.failure"
    assert kwargs["entity_id"] == vu.id
    assert kwargs["details"]["reason"] == "bad_password"
    # PII-lean: the supplier contact's address is not restated on every guess.
    assert vu.email not in str(kwargs["details"])


@pytest.mark.asyncio
async def test_portal_login_audits_a_deactivated_account_too(fake_redis, monkeypatch):
    """Someone hammering a deactivated supplier login is exactly what an admin
    wants to see. The rejection branch is merged (missing / no password /
    inactive), so it's easy to audit only the wrong-password case and leave
    this one silent."""
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest

    audit = MagicMock()
    monkeypatch.setattr(portal_auth, "queue_auth_audit", audit)

    vu = _vendor_user()
    vu.is_active = False

    with pytest.raises(HTTPException) as exc:
        await portal_auth.portal_login(
            body=PortalLoginRequest(email=vu.email, password="whatever-123"),
            request=_fake_request(),
            slug="acme",
            db=_portal_db(vu),
        )
    assert exc.value.status_code == 401

    audit.assert_called_once()
    assert audit.call_args.kwargs["details"]["reason"] == "inactive"


@pytest.mark.asyncio
async def test_portal_login_stays_silent_for_an_unknown_address(fake_redis, monkeypatch):
    """No account means no org, so there is no tenant trail to write into —
    and writing one would be the enumeration signal the 401 avoids.

    The queue is still *called*, with `organization_id=None`: both rejection
    branches reach it identically, which is what keeps them indistinguishable
    by response time. Only the row is dropped."""
    from app.api import portal_auth
    from app.schemas.portal import PortalLoginRequest

    audit = MagicMock()
    monkeypatch.setattr(portal_auth, "queue_auth_audit", audit)
    written = AsyncMock()
    monkeypatch.setattr("app.services.audit_dispatch._write_auth_audit", written)

    with pytest.raises(HTTPException):
        await portal_auth.portal_login(
            body=PortalLoginRequest(email="nobody@example.com", password="whatever-123"),
            request=_fake_request(),
            slug="acme",
            db=_portal_db(None),
        )

    audit.assert_called_once()
    assert audit.call_args.kwargs["organization_id"] is None
    written.assert_not_awaited()


@pytest.mark.asyncio
async def test_portal_mfa_verify_throttles_totp_guessing(fake_redis, monkeypatch):
    from app.api import portal_auth
    from app.schemas.portal import PortalMFAChallengeVerifyRequest
    from app.services.rate_limit import MFA_FAILURE_LIMIT

    monkeypatch.setattr(portal_auth, "dispatch_auth_audit", AsyncMock())
    monkeypatch.setattr(portal_auth.settings, "mfa_enabled", True)

    vu = _vendor_user(mfa_enabled=True, mfa_secret="SECRET")

    claims = MagicMock()
    claims.subject_id = vu.id
    claims.jti = "challenge-jti"
    monkeypatch.setattr(
        portal_auth.mfa, "decode_vendor_challenge_token", AsyncMock(return_value=claims)
    )
    monkeypatch.setattr(portal_auth.mfa, "verify_totp", AsyncMock(return_value=False))

    body = PortalMFAChallengeVerifyRequest(challenge_token="tok", code="000000")

    for i in range(MFA_FAILURE_LIMIT):
        with pytest.raises(HTTPException) as exc:
            await portal_auth.portal_mfa_challenge(
                body=body,
                request=_fake_request(ip=f"198.51.100.{i}"),
                slug="acme",
                db=_portal_db(vu),
            )
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await portal_auth.portal_mfa_challenge(
            body=body,
            request=_fake_request(ip="198.51.100.250"),
            slug="acme",
            db=_portal_db(vu),
        )
    assert exc.value.status_code == 429
