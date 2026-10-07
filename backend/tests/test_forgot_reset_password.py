"""Self-service password recovery — the main AP app's "forgot password" flow
(``POST /api/auth/forgot-password`` + ``POST /api/auth/reset-password``).

Before this, the only way to recover a forgotten password was an authenticated
admin resetting another user's password (`POST /api/admin/users/{id}` with a
`password` field) — a brand-new user or a solo admin locked out of their own
account had no self-service path on either front door. The supplier portal is
deliberately out of scope (a separate `VendorUser` surface); this covers only
the main app's `User` (control-plane) login.

Exercised through the real HTTP surface + real Postgres via the `realdb`
harness. Uses the richer zset+hash `FakeRedis` (not the key/value-only
autouse stub) because the happy path logs in via `/auth/login` (which calls
`register_session` -> `track_session`, needing `hset`) and
`/auth/reset-password` calls `revoke_user_sessions` (needing `zrange`/`hdel`
to actually walk and clear tracked sessions).

Tests that actually CHANGE a password mint their own throwaway control-plane
`User` row rather than reusing one of the four seeded role users
(`realdb.email("a", <role>)`) — those rows are NOT truncated between tests
(only tenant business tables are; control-plane `users` persist across the
whole slot, even across separate pytest invocations), so overwriting a seeded
role's password would leave every other test — and every later run against
this slot — locked out of the seed credential.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.user import User
from app.utils.passwords import pwd_context
from tests.conftest import role_email
from tests.test_session_management import FakeRedis

TENANT = "a"
SEED_PASSWORD = "Passw0rd!xyz"


@pytest.fixture
def fake_redis(monkeypatch):
    """Overrides the autouse key/value-only stub with the richer zset+hash
    fake (fixtures requested by name run after autouse ones) — same pattern
    as `test_vendor_portal_user_password_reset.py::fake_redis`."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _login(client, email: str, password: str):
    return await client.post("/api/auth/login", json={"email": email, "password": password})


async def _create_throwaway_user(realdb, key: str, *, password: str | None) -> str:
    """Insert a standalone control-plane User (no roles needed — plain
    password login doesn't read them) so a password-mutating test never
    touches one of the four persistent seeded role accounts. Returns the
    email."""
    email = f"pwreset-{uuid.uuid4().hex[:10]}@{realdb.info(key).slug}.test"
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        s.add(
            User(
                id=uuid.uuid4(),
                email=email,
                full_name="Reset Test User",
                hashed_password=pwd_context.hash(password) if password is not None else None,
                is_active=True,
                organization_id=realdb.info(key).org_id,
                must_change_password=False,
            )
        )
        await s.commit()
    return email


@pytest.mark.asyncio
async def test_forgot_password_happy_path_and_old_password_stops_working(
    realdb, fake_redis, monkeypatch
):
    """Full round trip: request a reset link for a real user, extract the
    token from the (mocked) email, redeem it, and confirm the login surface
    now accepts the new password and rejects the old one."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "tenant_url_template", "https://{slug}.feohledger.test", raising=True)

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    slug = realdb.info(TENANT).slug
    email = await _create_throwaway_user(realdb, TENANT, password=SEED_PASSWORD)

    async with realdb.client(key=TENANT, role=None) as c:
        # Prove the seed password works before the reset.
        pre_resp = await _login(c, email, SEED_PASSWORD)
        assert pre_resp.status_code == 200, pre_resp.text

        resp = await c.post("/api/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200, resp.text
        assert "sent password reset instructions" in resp.json()["detail"].lower()

        assert len(sent) == 1
        msg = sent[0]
        assert msg.to == email
        assert f"https://{slug}.feohledger.test/login/reset-password?token=" in msg.body_text

        token = msg.body_text.split("token=")[1].split("\n")[0].strip()
        assert token

        new_password = "BrandNewPassw0rd!42"
        reset_resp = await c.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": new_password},
        )
        assert reset_resp.status_code == 200, reset_resp.text
        assert "reset" in reset_resp.json()["detail"].lower()

        # Old password no longer works.
        old_resp = await _login(c, email, SEED_PASSWORD)
        assert old_resp.status_code == 401

        # New password works.
        new_resp = await _login(c, email, new_password)
        assert new_resp.status_code == 200, new_resp.text


@pytest.mark.asyncio
async def test_a_passwordless_sso_user_can_set_a_password_once_the_plan_drops_sso(
    realdb, fake_redis, monkeypatch
):
    """Decisions §258: when a tenant's plan stops granting SSO the password
    reopens. An account the IdP provisioned (JIT or SCIM) has NO password, so
    "reopens" only holds if forgot-password can give it one. The harness org
    holds no subscription — the plan a cancelled tenant falls back to."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.config import settings as cfg
    from app.models.organization import Organization
    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(cfg, "tenant_url_template", "https://{slug}.feohledger.test", raising=True)
    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        org.settings = {
            **(org.settings or {}),
            "sso": {
                "enabled": True,
                "sso_only": True,
                "provider": "entra",
                "discovery_url": "https://login.example.com/.well-known/openid-configuration",
                "client_id": "feoh",
                "client_secret": "s3cret-not-echoed",
            },
        }
        flag_modified(org, "settings")
        await s.commit()
    email = await _create_throwaway_user(realdb, TENANT, password=None)

    async with realdb.client(key=TENANT, role=None) as c:
        assert (await c.post("/api/auth/forgot-password", json={"email": email})).status_code == 200
        assert len(sent) == 1
        token = sent[0].body_text.split("token=")[1].split("\n")[0].strip()
        new_password = "BrandNewPassw0rd!42"
        reset = await c.post(
            "/api/auth/reset-password", json={"token": token, "new_password": new_password}
        )
        assert reset.status_code == 200, reset.text
        signed_in = await _login(c, email, new_password)
    assert signed_in.status_code == 200, signed_in.text


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_same_response_no_email_sent(
    realdb, fake_redis, monkeypatch
):
    """Enumeration resistance: an email that matches no account gets the exact
    same response shape as one that does, and never triggers a send."""
    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    slug = realdb.info(TENANT).slug
    real_email = role_email(slug, "ap_manager")
    fake_email = f"nobody-{uuid.uuid4().hex[:8]}@nowhere.example"

    async with realdb.client(key=TENANT, role=None) as c:
        real_resp = await c.post("/api/auth/forgot-password", json={"email": real_email})
        fake_resp = await c.post("/api/auth/forgot-password", json={"email": fake_email})

    assert real_resp.status_code == fake_resp.status_code == 200
    assert real_resp.json() == fake_resp.json()
    # Only the real account triggers an outbound send.
    assert len(sent) == 1
    assert sent[0].to == real_email


@pytest.mark.asyncio
async def test_reset_password_token_is_single_use(realdb, fake_redis, monkeypatch):
    email = await _create_throwaway_user(realdb, TENANT, password=SEED_PASSWORD)

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post("/api/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200
        token = sent[0].body_text.split("token=")[1].split("\n")[0].strip()

        first = await c.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": "FirstUseOnly!42"},
        )
        assert first.status_code == 200, first.text

        second = await c.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": "SecondUseNope!42"},
        )
        assert second.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_expired_token_rejected(realdb, fake_redis):
    """The fake Redis doesn't enforce TTL (same caveat the shared conftest
    stub documents), so expiry is simulated the way the token's OWN
    consumer sees it: the mapping is simply gone — indistinguishable from a
    genuine TTL eviction, which is the point (`consume_reset_token`'s
    docstring: expired and unknown are deliberately the same outcome)."""
    from app.services import password_reset

    user_id = uuid.uuid4()
    token = await password_reset.issue_reset_token(user_id)
    key = password_reset._reset_key(token)
    # Simulate the Redis-side TTL eviction directly on the fake's keyspace.
    fake_redis.strings.pop(key, None)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": "WontBeApplied!42"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_unknown_token_rejected(realdb, fake_redis):
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            "/api/auth/reset-password",
            json={"token": "not-a-real-token", "new_password": "WontBeApplied!42"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_weak_password_rejected(realdb, fake_redis, monkeypatch):
    email = await _create_throwaway_user(realdb, TENANT, password=SEED_PASSWORD)

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post("/api/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200
        token = sent[0].body_text.split("token=")[1].split("\n")[0].strip()

        weak_resp = await c.post(
            "/api/auth/reset-password",
            json={"token": token, "new_password": "short"},
        )
    assert weak_resp.status_code == 422
