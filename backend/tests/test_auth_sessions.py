"""Self-service session visibility + revocation (`/api/auth/sessions`).

The threat these routes close is the one the rest of the auth module keeps
naming: a leaked or stolen access token. Before them, the tracking set was
invisible to the account it described and the only remedy was an admin
deactivating the user. These tests pin the properties that make the routes a
real remedy rather than a decoration:

  - the caller sees only their OWN sessions, with their current one marked;
  - revoking actually blocklists (the token stops authenticating), it doesn't
    merely forget;
  - a JTI belonging to someone else is a 404, not a cross-account kill switch;
  - "sign out everywhere else" keeps exactly the caller's session.

DB-free: `get_current_user` is overridden with a stub principal so the handlers
and the real session service run end-to-end against an in-memory Redis. The
`session_jti` stashing that the handlers depend on is pinned separately, against
the real dependency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jose import jwt

from app.config import settings
from tests.test_session_management import FakeRedis

# Mock-DB tests: session revocation writes an auth audit row through its own control-plane/tenant
# sessions (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("no_auth_audit_write")

ORG_ID = uuid.uuid4()


@pytest.fixture
def fake_redis(monkeypatch):
    """Rich (zset + hash) Redis stand-in — the autouse conftest fake is
    key/value only and can't back the session structures."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


@pytest.fixture
def audit_calls(monkeypatch):
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.auth.dispatch_auth_audit", _capture)
    return calls


class _Session:
    """An authenticated client whose principal is a stub user."""

    def __init__(self, user_id: uuid.UUID, jti: str | None):
        self.user = SimpleNamespace(
            id=user_id,
            organization_id=ORG_ID,
            is_active=True,
            roles=[],
            effective_permissions=frozenset(),
            session_jti=jti,
        )


@pytest.fixture
def client_for():
    """Factory: build an httpx client authenticated as (user_id, jti)."""
    from app.api.deps import get_current_user
    from app.main import app

    made: list[httpx.AsyncClient] = []

    def _make(user_id: uuid.UUID, jti: str | None):
        principal = _Session(user_id, jti).user

        async def _override():
            return principal

        app.dependency_overrides[get_current_user] = _override
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        made.append(c)
        return c

    yield _make
    app.dependency_overrides.clear()


async def _seed(user_id, jti, *, ip=None, user_agent=None, method=None):
    from app.services.session_management import register_session

    with patch("app.services.session_management.settings") as fake:
        fake.max_concurrent_sessions = 0
        fake.access_token_expire_minutes = 30
        await register_session(user_id, jti, ip=ip, user_agent=user_agent, method=method)


# ---------------------------------------------------------------------------
# The dependency contract the handlers rely on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_user_stashes_the_requesting_jti():
    """`user.session_jti` is what lets the session routes tell the caller's own
    entry apart from the rest — without it "sign out everywhere else" would
    sign the caller out too."""
    from app.api.deps import get_current_user

    jti = str(uuid.uuid4())
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org": str(ORG_ID),
            "typ": "user",
            "jti": jti,
            "exp": datetime.now(UTC) + timedelta(seconds=300),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    fake_user = SimpleNamespace(id=uuid.uuid4(), is_active=True, organization_id=ORG_ID, roles=[])
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=fake_user)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    with patch("app.api.deps.is_token_blocked", AsyncMock(return_value=False)):
        user = await get_current_user(authorization=f"Bearer {token}", db=db)

    assert user.session_jti == jti


# ---------------------------------------------------------------------------
# GET /api/auth/sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_marks_the_current_one(fake_redis, client_for):
    user_id = uuid.uuid4()
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed(
        user_id,
        other,
        ip="198.51.100.9",
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/124.0 Safari/537.36 Edg/124.0",
        method="password",
    )
    await _seed(user_id, mine, ip="203.0.113.4", method="sso:okta")

    client = client_for(user_id, mine)
    async with client:
        res = await client.get("/api/auth/sessions")

    assert res.status_code == 200
    body = res.json()
    assert len(body) == 2
    # Newest first — the caller's session was registered last.
    assert body[0]["id"] == mine
    assert body[0]["current"] is True
    assert body[0]["method"] == "sso:okta"
    assert body[1]["id"] == other
    assert body[1]["current"] is False
    assert body[1]["ip"] == "198.51.100.9"
    assert body[1]["device"] == "Edge on Windows"


@pytest.mark.asyncio
async def test_list_sessions_never_shows_another_users(fake_redis, client_for):
    """Every lookup is keyed on the caller's own id — a neighbouring account's
    sessions are not reachable, not even read-only."""
    me, neighbour = uuid.uuid4(), uuid.uuid4()
    await _seed(me, str(uuid.uuid4()), ip="203.0.113.4")
    neighbour_jti = str(uuid.uuid4())
    await _seed(neighbour, neighbour_jti, ip="192.0.2.55")

    client = client_for(me, None)
    async with client:
        body = (await client.get("/api/auth/sessions")).json()

    assert neighbour_jti not in [s["id"] for s in body]
    assert "192.0.2.55" not in [s["ip"] for s in body]


@pytest.mark.asyncio
async def test_list_sessions_is_empty_when_nothing_is_tracked(fake_redis, client_for):
    client = client_for(uuid.uuid4(), None)
    async with client:
        res = await client.get("/api/auth/sessions")
    assert res.status_code == 200
    assert res.json() == []


@pytest.mark.asyncio
async def test_sessions_endpoints_require_authentication():
    """No override installed — the real dependency must refuse an anonymous call."""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/auth/sessions")).status_code == 401
        assert (await client.post("/api/auth/sessions/revoke-others")).status_code == 401
        assert (await client.delete(f"/api/auth/sessions/{uuid.uuid4()}")).status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/auth/sessions/{jti}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_one_session_blocklists_it(fake_redis, client_for, audit_calls):
    user_id = uuid.uuid4()
    mine, doomed = str(uuid.uuid4()), str(uuid.uuid4())
    await _seed(user_id, mine)
    await _seed(user_id, doomed, ip="192.0.2.10")

    client = client_for(user_id, mine)
    async with client:
        res = await client.delete(f"/api/auth/sessions/{doomed}")
        remaining = (await client.get("/api/auth/sessions")).json()

    assert res.status_code == 200
    assert res.json() == {"revoked": 1}
    assert [s["id"] for s in remaining] == [mine]
    # Blocklisted, not merely forgotten — otherwise the stolen token keeps working.
    assert f"token:blocked:{doomed}" in fake_redis.strings
    assert [c["action"] for c in audit_calls] == ["auth.session.revoked"]
    assert audit_calls[0]["details"] == {"scope": "single", "revoked": 1}


@pytest.mark.asyncio
async def test_revoke_refuses_a_jti_belonging_to_another_user(fake_redis, client_for, audit_calls):
    """The opaque 404 is both the authorization boundary and the anti-probe:
    "not yours" and "already gone" answer identically."""
    attacker, victim = uuid.uuid4(), uuid.uuid4()
    victim_jti = str(uuid.uuid4())
    await _seed(victim, victim_jti)
    await _seed(attacker, str(uuid.uuid4()))

    client = client_for(attacker, None)
    async with client:
        res = await client.delete(f"/api/auth/sessions/{victim_jti}")

    assert res.status_code == 404
    assert f"token:blocked:{victim_jti}" not in fake_redis.strings
    # A refused revoke writes no audit row — only real revocations do.
    assert audit_calls == []


@pytest.mark.asyncio
async def test_revoke_unknown_jti_is_404(fake_redis, client_for):
    user_id = uuid.uuid4()
    await _seed(user_id, str(uuid.uuid4()))
    client = client_for(user_id, None)
    async with client:
        res = await client.delete(f"/api/auth/sessions/{uuid.uuid4()}")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_revoking_your_own_current_session_is_allowed(fake_redis, client_for):
    """Equivalent to logging out — no special case, so the UI can offer it
    without the backend second-guessing which row the user clicked."""
    user_id = uuid.uuid4()
    mine = str(uuid.uuid4())
    await _seed(user_id, mine)

    client = client_for(user_id, mine)
    async with client:
        res = await client.delete(f"/api/auth/sessions/{mine}")

    assert res.status_code == 200
    assert f"token:blocked:{mine}" in fake_redis.strings


# ---------------------------------------------------------------------------
# POST /api/auth/sessions/revoke-others
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_others_keeps_only_the_caller(fake_redis, client_for, audit_calls):
    user_id = uuid.uuid4()
    mine = str(uuid.uuid4())
    others = [str(uuid.uuid4()) for _ in range(3)]
    await _seed(user_id, mine, ip="203.0.113.4")
    for jti in others:
        await _seed(user_id, jti, ip="192.0.2.10")

    client = client_for(user_id, mine)
    async with client:
        res = await client.post("/api/auth/sessions/revoke-others")
        remaining = (await client.get("/api/auth/sessions")).json()

    assert res.status_code == 200
    assert res.json() == {"revoked": 3}
    assert [s["id"] for s in remaining] == [mine]
    assert f"token:blocked:{mine}" not in fake_redis.strings
    for jti in others:
        assert f"token:blocked:{jti}" in fake_redis.strings
    assert audit_calls[0]["details"] == {"scope": "others", "revoked": 3}


@pytest.mark.asyncio
async def test_revoke_others_is_idempotent(fake_redis, client_for):
    user_id = uuid.uuid4()
    mine = str(uuid.uuid4())
    await _seed(user_id, mine)
    await _seed(user_id, str(uuid.uuid4()))

    client = client_for(user_id, mine)
    async with client:
        first = await client.post("/api/auth/sessions/revoke-others")
        second = await client.post("/api/auth/sessions/revoke-others")

    assert first.json() == {"revoked": 1}
    assert second.json() == {"revoked": 0}


@pytest.mark.asyncio
async def test_revoke_others_leaves_another_users_sessions_alone(fake_redis, client_for):
    me, neighbour = uuid.uuid4(), uuid.uuid4()
    mine = str(uuid.uuid4())
    neighbour_jti = str(uuid.uuid4())
    await _seed(me, mine)
    await _seed(me, str(uuid.uuid4()))
    await _seed(neighbour, neighbour_jti)

    client = client_for(me, mine)
    async with client:
        await client.post("/api/auth/sessions/revoke-others")

    assert f"token:blocked:{neighbour_jti}" not in fake_redis.strings
    assert [m for (_, m) in fake_redis.zsets[f"active_jtis:{neighbour}"]] == [neighbour_jti]


# ---------------------------------------------------------------------------
# The same primitive, reused by a self-service password change
#
# `POST /api/auth/change-password` and the `PATCH /api/auth/me` password branch
# both call `_revoke_sessions_after_password_change`. Pinned here against the
# real session service so the behaviour is proven once, where the Redis
# structures actually exist — the handler-level tests (test_password_security.py)
# only assert that each route reaches it.
# ---------------------------------------------------------------------------


@pytest.fixture
def password_audit_calls(monkeypatch):
    calls: list[dict] = []

    async def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.auth.dispatch_auth_audit", _capture)
    return calls


@pytest.mark.asyncio
async def test_password_change_revokes_every_session_but_the_callers(
    fake_redis, password_audit_calls
):
    from app.api.auth import _revoke_sessions_after_password_change

    user_id = uuid.uuid4()
    mine, laptop, phone = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await _seed(user_id, mine)
    await _seed(user_id, laptop)
    await _seed(user_id, phone)

    user = SimpleNamespace(id=user_id, organization_id=ORG_ID, session_jti=mine)
    await _revoke_sessions_after_password_change(user)

    # The stolen sessions stop authenticating; the tab that made the change
    # keeps working.
    assert f"token:blocked:{laptop}" in fake_redis.strings
    assert f"token:blocked:{phone}" in fake_redis.strings
    assert f"token:blocked:{mine}" not in fake_redis.strings

    assert [c["action"] for c in password_audit_calls] == ["auth.session.revoked"]
    details = password_audit_calls[0]["details"]
    assert details["scope"] == "others"
    assert details["revoked"] == 2
    assert details["reason"] == "password_changed"


@pytest.mark.asyncio
async def test_password_change_with_no_other_sessions_still_audits(
    fake_redis, password_audit_calls
):
    """Idempotent: nothing else signed in reports `revoked: 0` rather than
    failing — and never blocklists the caller's own session."""
    from app.api.auth import _revoke_sessions_after_password_change

    user_id = uuid.uuid4()
    mine = str(uuid.uuid4())
    await _seed(user_id, mine)

    user = SimpleNamespace(id=user_id, organization_id=ORG_ID, session_jti=mine)
    await _revoke_sessions_after_password_change(user)

    assert f"token:blocked:{mine}" not in fake_redis.strings
    assert password_audit_calls[0]["details"]["revoked"] == 0
