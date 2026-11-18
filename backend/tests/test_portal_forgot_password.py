"""Supplier self-service password recovery — `POST /api/portal/auth/forgot-password`.

The portal twin of `/api/auth/forgot-password` (`test_forgot_reset_password.py`).
It mints the same tenant-bound portal token the AP team's reset mails, with the
forgot-password lifetime, redeemed at `POST /api/portal/auth/reset-password`.
The link is read out of the delivered email, the way the supplier's click would.

The send is detached (so response time does not reveal a match);
`_drain_sends` awaits the module's own task set — the real completion signal —
before a test reads the outbox.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog
from tests.set_password_mail import capture_outbox, link_mode, link_token, set_password_link
from tests.test_session_management import FakeRedis

TENANT = "a"
OTHER_TENANT = "b"
FORGOT = "/api/portal/auth/forgot-password"


@pytest.fixture
def fake_redis(monkeypatch):
    """The zset+hash stand-in a real portal login needs, shared with the token
    store so a test can read the TTL a token was minted with."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    monkeypatch.setattr("app.services.password_reset.get_redis", _get_redis)
    return fake


@pytest.fixture
def sent_mail(monkeypatch):
    return capture_outbox(monkeypatch)


async def _drain_sends() -> None:
    from app.api import portal_auth

    if portal_auth._background_tasks:
        await asyncio.gather(*list(portal_auth._background_tasks))


async def _supplier_with_password(
    realdb, key: str, password: str, outbox: list
) -> tuple[str, uuid.UUID]:
    """Invite a supplier through the AP route and set their password through
    the invite link, so the account is exactly what production holds. Leaves
    ``outbox`` empty. Returns (email, vendor_user_id)."""
    from tests.test_vendor_portal_user_password_reset import (
        _create_vendor,
        _invite_portal_user,
        _set_password,
    )

    email = f"forgot-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=key, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
    async with realdb.client(key=key, role=None) as anon:
        assert (await _set_password(anon, link_token(outbox[-1]), password)).status_code == 200
    outbox.clear()
    return email, uuid.UUID(invited["user"]["id"])


async def _login(client, email: str, password: str):
    return await client.post("/api/portal/auth/login", json={"email": email, "password": password})


@pytest.mark.asyncio
async def test_forgot_password_mails_a_short_lived_link_that_works_once(
    realdb, fake_redis, sent_mail
):
    email, vu_id = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)

    async with realdb.client(key=TENANT, role=None) as anon:
        resp = await anon.post(FORGOT, json={"email": email.upper()})
        assert resp.status_code == 200, resp.text
        await _drain_sends()

        assert len(sent_mail) == 1
        msg = sent_mail[0]
        assert msg.to == email
        assert "/portal/reset-password?token=" in set_password_link(msg)
        assert link_mode(msg) is None  # a reset, not an invite's "set" copy
        token = link_token(msg)
        assert token not in resp.text

        # The forgot-password lifetime, not the 72-hour set-password one.
        ttls = [ttl for key, ttl in fake_redis.ttls.items() if key.startswith("portal:pwreset:")]
        assert settings.password_reset_ttl_minutes * 60 in ttls

        # Requesting a link changes nothing until it is used.
        assert (await _login(anon, email, "Original-Chosen9")).status_code == 200

        done = await anon.post(
            "/api/portal/auth/reset-password",
            json={"token": token, "new_password": "Recovered-Chosen9"},
        )
        assert done.status_code == 200, done.text
        again = await anon.post(
            "/api/portal/auth/reset-password",
            json={"token": token, "new_password": "Twice-Chosen9"},
        )
        assert again.status_code == 400

        assert (await _login(anon, email, "Original-Chosen9")).status_code == 401
        assert (await _login(anon, email, "Recovered-Chosen9")).status_code == 200

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == vu_id,
                        AuditLog.action == "portal.password_reset.requested",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor_id is None
    assert set((rows[0].details or {}).keys()) == {"ip"}
    assert email not in repr(rows[0].details)


@pytest.mark.asyncio
async def test_unknown_address_gets_the_same_response_and_no_mail(realdb, fake_redis, sent_mail):
    email, _ = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)

    async with realdb.client(key=TENANT, role=None) as anon:
        known = await anon.post(FORGOT, json={"email": email})
        unknown = await anon.post(
            FORGOT, json={"email": f"nobody-{uuid.uuid4().hex[:8]}@nowhere.example"}
        )
        await _drain_sends()

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m.to for m in sent_mail] == [email]


@pytest.mark.asyncio
async def test_a_deactivated_supplier_gets_no_link(realdb, fake_redis, sent_mail):
    email, vu_id = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)
    async with realdb.sessionmaker(TENANT)() as s:
        vu = (await s.execute(select(VendorUser).where(VendorUser.id == vu_id))).scalar_one()
        vu.is_active = False
        await s.commit()

    async with realdb.client(key=TENANT, role=None) as anon:
        resp = await anon.post(FORGOT, json={"email": email})
        await _drain_sends()
    assert resp.status_code == 200
    assert sent_mail == []


@pytest.mark.asyncio
async def test_another_tenants_supplier_is_unknown_here_and_their_link_is_refused(
    realdb, fake_redis, sent_mail
):
    """Tenant-scoped both ways: the address is looked up only in the requesting
    tenant's database, and a link minted for tenant A is refused unspent on
    tenant B's host."""
    email, _ = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)

    async with realdb.client(key=OTHER_TENANT, role=None) as other:
        resp = await other.post(FORGOT, json={"email": email})
        await _drain_sends()
    assert resp.status_code == 200
    assert sent_mail == []

    async with realdb.client(key=TENANT, role=None) as anon:
        assert (await anon.post(FORGOT, json={"email": email})).status_code == 200
        await _drain_sends()
    token = link_token(sent_mail[0])
    assert f"//{realdb.info(TENANT).slug}." in set_password_link(sent_mail[0])

    async with realdb.client(key=OTHER_TENANT, role=None) as other:
        refused = await other.post(
            "/api/portal/auth/reset-password",
            json={"token": token, "new_password": "Recovered-Chosen9"},
        )
        assert refused.status_code == 400
    async with realdb.client(key=TENANT, role=None) as anon:
        ok = await anon.post(
            "/api/portal/auth/reset-password",
            json={"token": token, "new_password": "Recovered-Chosen9"},
        )
        assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_forgot_password_never_restamps_the_provisioner(realdb, fake_redis, sent_mail):
    """`provisioned_by_user_id` is the bank-change SoD record of which AP actor
    chose this login's address. A supplier asking for their own link is not an
    AP actor, so the stamp stays exactly as the invite left it."""
    email, vu_id = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)
    async with realdb.sessionmaker(TENANT)() as s:
        before = (
            await s.execute(select(VendorUser.provisioned_by_user_id).where(VendorUser.id == vu_id))
        ).scalar_one()
    assert before is not None

    async with realdb.client(key=TENANT, role=None) as anon:
        await anon.post(FORGOT, json={"email": email})
        await _drain_sends()
        token = link_token(sent_mail[-1])
        await anon.post(
            "/api/portal/auth/reset-password",
            json={"token": token, "new_password": "Recovered-Chosen9"},
        )

    async with realdb.sessionmaker(TENANT)() as s:
        after = (
            await s.execute(select(VendorUser.provisioned_by_user_id).where(VendorUser.id == vu_id))
        ).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_per_address_budget_throttles_known_and_unknown_alike(
    realdb, fake_redis, sent_mail, monkeypatch
):
    """3 an hour per (tenant, address), checked before the lookup, so the 429
    is no oracle; a different address is a different bucket."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    email, _ = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)
    unknown = f"nobody-{uuid.uuid4().hex[:8]}@nowhere.example"

    async with realdb.client(key=TENANT, role=None) as anon:
        known_codes = [
            (await anon.post(FORGOT, json={"email": email})).status_code for _ in range(4)
        ]
    # A fresh IP bucket for the second address, so only the address budget is
    # under test here.
    monkeypatch.setattr("app.services.rate_limit._client_ip", lambda request: "198.51.100.7")
    async with realdb.client(key=TENANT, role=None) as anon:
        unknown_codes = [
            (await anon.post(FORGOT, json={"email": unknown})).status_code for _ in range(4)
        ]
        await _drain_sends()

    assert known_codes == [200, 200, 200, 429]
    assert unknown_codes == known_codes


@pytest.mark.asyncio
async def test_per_ip_budget_caps_address_rotation(realdb, fake_redis, sent_mail, monkeypatch):
    """5 an hour per IP, whatever address each request names."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    async with realdb.client(key=TENANT, role=None) as anon:
        codes = [
            (
                await anon.post(
                    FORGOT, json={"email": f"rotate-{uuid.uuid4().hex[:8]}@nowhere.example"}
                )
            ).status_code
            for _ in range(6)
        ]
    assert codes == [200] * 5 + [429]


@pytest.mark.asyncio
async def test_a_token_store_failure_answers_like_any_other_request(
    realdb, fake_redis, sent_mail, monkeypatch
):
    """Only a real address reaches the token store, so a 500 there would say
    the address exists. It is logged and answered with the generic 200."""
    email, _ = await _supplier_with_password(realdb, TENANT, "Original-Chosen9", sent_mail)

    async def _down(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.services.password_reset.issue_portal_reset_token", _down)
    async with realdb.client(key=TENANT, role=None) as anon:
        resp = await anon.post(FORGOT, json={"email": email})
        await _drain_sends()
    assert resp.status_code == 200
    assert sent_mail == []
