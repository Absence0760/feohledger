"""AP-admin-triggered supplier-portal user password reset, and the portal
set-password link it and the invite send.

Before `POST /api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password`
existed, the only recovery for a locked-out vendor portal user was AP deleting
and re-inviting them — a new `VendorUser.id` that breaks anything referencing
the old one (chat authorship, audit rows, notification-preferences).

Neither invite nor reset hands out a temporary password any more (decisions
§283): each leaves the account with no password and emails the supplier a
single-use, expiring link bound to this tenant, redeemed by `POST
/api/portal/auth/reset-password`. The link is read out of the **delivered
email**, never the HTTP response — returning a credential for a caller-supplied
address is what let one `ap_manager` mint a supplier login they controlled and
complete a bank redirect alone (`test_vendor_bank_change_provisioner_sod.py`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog
from tests.set_password_mail import capture_outbox, link_mode, link_token, set_password_link
from tests.test_session_management import FakeRedis

TENANT = "a"


@pytest.fixture
def mk(realdb):
    return realdb.sessionmaker(TENANT)


@pytest.fixture
def sent_mail(monkeypatch):
    """Capture what the (dev-default `console`) email adapter would send —
    the only channel a supplier-portal set-password link travels on."""
    return capture_outbox(monkeypatch)


@pytest.fixture
def fake_redis(monkeypatch):
    """A real portal login goes through `session_management.register_session`,
    which needs the richer zset+hash Redis stand-in (`FakeRedis`), not the
    key/value-only stub the autouse `_autouse_fake_redis` fixture installs —
    that one has no `zadd`. A test-requested fixture overrides the autouse one
    (fixtures requested by name run after autouse ones)."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


async def _create_vendor(client) -> str:
    resp = await client.post(
        "/api/vendors",
        json={"name": f"Reset Test Vendor {uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _invite_portal_user(client, vendor_id: str, email: str) -> dict:
    resp = await client.post(
        f"/api/vendors/{vendor_id}/portal-users",
        json={"email": email, "full_name": "Locked Out Vendor"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set_password(anon_client, token: str, password: str):
    return await anon_client.post(
        "/api/portal/auth/reset-password", json={"token": token, "new_password": password}
    )


@pytest.mark.asyncio
async def test_invite_mails_a_tenant_bound_set_link_and_no_password(realdb, mk, sent_mail):
    email = f"invited-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
    vendor_user_id = invited["user"]["id"]

    assert len(sent_mail) == 1
    msg = sent_mail[0]
    assert msg.to == email
    assert "/portal/reset-password?token=" in set_password_link(msg)
    assert link_mode(msg) == "set"
    assert "Password:" not in msg.body_text
    assert link_token(msg) not in str(invited)

    async with mk() as s:
        vu = (
            await s.execute(select(VendorUser).where(VendorUser.id == uuid.UUID(vendor_user_id)))
        ).scalar_one()
    assert vu.hashed_password is None
    assert vu.must_change_password is False
    assert invited["user"]["must_change_password"] is False


@pytest.mark.asyncio
async def test_invite_link_sets_the_password_once_and_the_supplier_signs_in(
    realdb, fake_redis, sent_mail
):
    email = f"invited-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        await _invite_portal_user(client, vendor_id, email)
    token = link_token(sent_mail[0])

    async with realdb.client(key=TENANT, role=None) as anon:
        refused = await anon.post(
            "/api/portal/auth/login", json={"email": email, "password": "Anything-Goes1"}
        )
        assert refused.status_code == 401

        # The page's load is a read: it does not spend the link.
        for _ in range(2):
            check = await anon.post("/api/portal/auth/reset-password/check", json={"token": token})
            assert check.status_code == 200, check.text
            assert check.json()["email"] == email

        weak = await _set_password(anon, token, "short")
        assert weak.status_code == 422
        done = await _set_password(anon, token, "Supplier-Chosen9")
        assert done.status_code == 200, done.text

        assert (await _set_password(anon, token, "Another-Chosen9")).status_code == 400
        gone = await anon.post("/api/portal/auth/reset-password/check", json={"token": token})
        assert gone.status_code == 400

        login = await anon.post(
            "/api/portal/auth/login", json={"email": email, "password": "Supplier-Chosen9"}
        )
    assert login.status_code == 200, login.text
    assert login.json()["must_change_password"] is False


@pytest.mark.asyncio
async def test_a_portal_link_is_refused_on_another_tenant_and_not_spent(
    realdb, fake_redis, sent_mail
):
    """The token is bound to the tenant that minted it: presented on another
    tenant's host it is the same 400 as an unknown link, and it still works
    where it belongs."""
    email = f"invited-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        await _invite_portal_user(client, vendor_id, email)
    token = link_token(sent_mail[0])

    async with realdb.client(key="b", role=None) as other:
        check = await other.post("/api/portal/auth/reset-password/check", json={"token": token})
        assert check.status_code == 400
        assert (await _set_password(other, token, "Supplier-Chosen9")).status_code == 400

    async with realdb.client(key=TENANT, role=None) as anon:
        assert (await _set_password(anon, token, "Supplier-Chosen9")).status_code == 200


@pytest.mark.asyncio
async def test_an_employee_reset_token_is_not_a_portal_token(realdb, sent_mail):
    """Two token families under two key prefixes: neither redemption endpoint
    accepts the other's token, whatever the ids."""
    from app.services import password_reset

    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(
            client, vendor_id, f"invited-{uuid.uuid4().hex[:8]}@vendor.test"
        )
    vu_id = uuid.UUID(invited["user"]["id"])
    employee_token = await password_reset.issue_reset_token(vu_id)
    portal_token = link_token(sent_mail[0])

    async with realdb.client(key=TENANT, role=None) as anon:
        assert (await _set_password(anon, employee_token, "Supplier-Chosen9")).status_code == 400
        employee_side = await anon.post(
            "/api/auth/reset-password",
            json={"token": portal_token, "new_password": "Supplier-Chosen9"},
        )
        assert employee_side.status_code == 400


@pytest.mark.asyncio
async def test_reset_clears_the_password_and_mails_a_new_link(realdb, mk, fake_redis, sent_mail):
    """Reset lands on the SAME VendorUser id, kills the old password at once,
    and mails a fresh link (never returned to the AP caller) that sets a new
    one."""
    email = f"locked-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as admin_client:
        vendor_id = await _create_vendor(admin_client)
        invited = await _invite_portal_user(admin_client, vendor_id, email)
        vendor_user_id = invited["user"]["id"]
        async with realdb.client(key=TENANT, role=None) as anon:
            assert (
                await _set_password(anon, link_token(sent_mail[0]), "Original-Pass9")
            ).status_code == 200
            pre = await anon.post(
                "/api/portal/auth/login", json={"email": email, "password": "Original-Pass9"}
            )
            assert pre.status_code == 200, pre.text

        resp = await admin_client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"] == vendor_user_id
    assert body["user"]["must_change_password"] is False
    assert "temp_password" not in body
    assert len(sent_mail) == 2
    assert sent_mail[1].to == email
    new_token = link_token(sent_mail[1])
    assert link_mode(sent_mail[1]) is None
    assert new_token not in resp.text

    async with mk() as s:
        vu = (
            await s.execute(select(VendorUser).where(VendorUser.id == uuid.UUID(vendor_user_id)))
        ).scalar_one()
    assert vu.hashed_password is None

    async with realdb.client(key=TENANT, role=None) as anon:
        old = await anon.post(
            "/api/portal/auth/login", json={"email": email, "password": "Original-Pass9"}
        )
        assert old.status_code == 401
        assert (await _set_password(anon, new_token, "Replaced-Pass9")).status_code == 200
        new = await anon.post(
            "/api/portal/auth/login", json={"email": email, "password": "Replaced-Pass9"}
        )
    assert new.status_code == 200, new.text


@pytest.mark.asyncio
async def test_reset_email_failure_leaves_the_old_password_working(realdb, mk, monkeypatch):
    from app.utils.passwords import hash_password, verify_password

    capture_outbox(monkeypatch)
    email = f"locked-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
        vendor_user_id = uuid.UUID(invited["user"]["id"])
        async with mk() as s:
            vu = (
                await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
            ).scalar_one()
            vu.hashed_password = await hash_password("Original-Pass9")
            await s.commit()

        capture_outbox(monkeypatch, fail=True)
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "set_password_email_failed"
    async with mk() as s:
        vu = (
            await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
        ).scalar_one()
    assert await verify_password("Original-Pass9", vu.hashed_password)


@pytest.mark.asyncio
async def test_reset_writes_an_audit_row(realdb, mk, sent_mail):
    """Status/credential changes on a supplier-portal identity are audited
    like every other vendor mutation (project invariant — append-only audit
    trail on state changes)."""
    email = f"locked-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
        vendor_user_id = invited["user"]["id"]

        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == "vendor_user",
                        AuditLog.entity_id == uuid.UUID(vendor_user_id),
                        AuditLog.action == "vendor_portal_user.password_reset",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    details = rows[0].details or {}
    assert details.get("vendor_id") == vendor_id
    assert details.get("delivery") == "set_password_link"
    # PII-lean: never the link or the vendor user's email.
    assert link_token(sent_mail[-1]) not in str(details)
    assert email not in str(details)


@pytest.mark.asyncio
async def test_reset_unknown_vendor_user_is_404(realdb, sent_mail):
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{uuid.uuid4()}/reset-password"
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reset_requires_admin_or_ap_manager_role(realdb, sent_mail):
    """Same permission gate as invite/delete — an ap_clerk cannot reset a
    supplier's credential."""
    email = f"locked-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as admin_client:
        vendor_id = await _create_vendor(admin_client)
        invited = await _invite_portal_user(admin_client, vendor_id, email)
    vendor_user_id = invited["user"]["id"]

    async with realdb.client(key=TENANT, role="ap_clerk") as clerk_client:
        resp = await clerk_client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reset_supersedes_the_unused_invite_link(realdb, fake_redis, sent_mail):
    """A 72-hour invite link must not outlive a reset: the reset kills every
    earlier link, so only the newest one sets a password."""
    email = f"locked-{uuid.uuid4().hex[:8]}@vendor.test"
    async with realdb.client(key=TENANT, role="admin") as client:
        vendor_id = await _create_vendor(client)
        invited = await _invite_portal_user(client, vendor_id, email)
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{invited['user']['id']}/reset-password"
        )
    assert resp.status_code == 200, resp.text
    invite_token, reset_token = link_token(sent_mail[0]), link_token(sent_mail[1])

    async with realdb.client(key=TENANT, role=None) as anon:
        stale = await anon.post(
            "/api/portal/auth/reset-password/check", json={"token": invite_token}
        )
        assert stale.status_code == 400
        assert (await _set_password(anon, invite_token, "Stale-Link-Pass9")).status_code == 400
        assert (await _set_password(anon, reset_token, "Fresh-Link-Pass9")).status_code == 200
