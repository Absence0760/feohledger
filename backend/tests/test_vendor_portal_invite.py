"""Real-DB coverage for the supplier-portal invite email
(``POST /api/vendors/{vendor_id}/portal-users``).

The invite email used to hardcode a non-existent placeholder domain
(``https://{slug}.app.com/portal``) instead of the tenant's real portal
URL. It must build the URL the same way the signup welcome email
(``app/api/signup.py::_tenant_url``) and the supplier-chat portal-link
email (``services/supplier_chat.py``) do — from ``FEOH_TENANT_URL_TEMPLATE``
— and echo it back on the response as ``portal_url``.

Follows the ``realdb`` conventions in ``test_supplier_chat.py``.
"""

from __future__ import annotations

import uuid

import pytest_asyncio

from app.models.vendor import Vendor
from tests.set_password_mail import capture_outbox

TENANT = "a"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_control_factory(realdb, monkeypatch):
    """Mirrors test_supplier_chat.py — point `control_session_factory` at a
    fresh engine bound to this test's event loop and this slot's control DB,
    since the welcome email path resolves the org through it."""
    monkeypatch.setattr("app.database.control_session_factory", realdb.control_sessionmaker())


async def _add_vendor(mk, org_id, *, name="Acme Supplies") -> uuid.UUID:
    vid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vid,
                organization_id=org_id,
                name=name,
                status="active",
                source="manual",
            )
        )
        await s.commit()
    return vid


async def test_portal_invite_email_uses_real_tenant_url(realdb, monkeypatch):
    """The invite email body and the response's `portal_url` must be built
    from `FEOH_TENANT_URL_TEMPLATE`, not a hardcoded placeholder domain."""
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "tenant_url_template", "https://{slug}.feohledger.test", raising=True)

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    slug = realdb.info(TENANT).slug
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": "buyer@supplier.example", "full_name": "Portal User"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    expected_url = f"https://{slug}.feohledger.test/portal"
    # Never the old hardcoded placeholder.
    assert "app.com" not in body["portal_url"]
    assert body["portal_url"] == expected_url

    assert len(sent) == 1
    msg = sent[0]
    assert msg.to == "buyer@supplier.example"
    assert expected_url in msg.body_text
    assert "app.com" not in msg.body_text
    # The set-password link lands on the same real host.
    assert f"{expected_url}/reset-password?token=" in msg.body_text


async def test_portal_invite_without_a_tenant_url_is_refused_before_creating(realdb, monkeypatch):
    """No `FEOH_TENANT_URL_TEMPLATE` configured -> there is nowhere to point the
    supplier's set-password link, so the invite is refused (503) before a
    portal account exists that nobody could ever sign in to — and no URL is
    fabricated from a placeholder domain."""
    from sqlalchemy import select

    from app.config import settings as cfg
    from app.models.vendor_user import VendorUser

    monkeypatch.setattr(cfg, "tenant_url_template", "", raising=True)
    sent = capture_outbox(monkeypatch)

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id, name="No Template Co")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": "buyer2@supplier.example", "full_name": "Portal User 2"},
        )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "set_password_link_unavailable"
    assert sent == []
    async with mk() as s:
        rows = (await s.execute(select(VendorUser).where(VendorUser.vendor_id == vendor_id))).all()
    assert rows == []


async def test_portal_invite_email_failure_creates_no_account(realdb, monkeypatch):
    from sqlalchemy import select

    from app.models.vendor_user import VendorUser

    capture_outbox(monkeypatch, fail=True)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _add_vendor(mk, realdb.info(TENANT).org_id, name="Mail Down Co")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": "buyer3@supplier.example", "full_name": "Portal User 3"},
        )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "set_password_email_failed"
    async with mk() as s:
        rows = (await s.execute(select(VendorUser).where(VendorUser.vendor_id == vendor_id))).all()
    assert rows == []


async def test_deleting_a_portal_user_writes_an_audit_row(realdb):
    """Revoking a supplier's portal credential is an access-control change, and
    it was the one leaving no trace.

    The row is keyed on the VENDOR rather than the deleted `VendorUser`, so it
    stays reachable from the vendor's own trail once the user row is gone, and
    it carries the vendor-user id only — never the supplier's login address.
    """
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key=TENANT, role="admin") as c:
        created = await c.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": "revoke-me@supplier.example", "full_name": "Doomed"},
        )
        assert created.status_code == 201, created.text
        vendor_user_id = created.json()["user"]["id"]

        deleted = await c.delete(f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}")
    assert deleted.status_code in (200, 204), deleted.text

    async with mk() as s:
        rows = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "vendor_user.deleted"))
            ).scalars()
        )
    mine = [r for r in rows if r.details.get("vendor_user_id") == vendor_user_id]
    assert len(mine) == 1, "revoking a portal credential must leave exactly one audit row"
    assert str(mine[0].entity_id) == str(vendor_id)
    assert mine[0].actor_id is not None
    # PII-free: the id, never the supplier's email address.
    assert "revoke-me@supplier.example" not in str(mine[0].details)


async def test_inviting_a_portal_user_writes_an_audit_row(realdb):
    """Provisioning a supplier-portal credential is the one AP action tying an
    AP actor to a vendor identity they created — it must leave a trace
    (invariant #3), keyed on the vendor and PII-free.
    """
    from sqlalchemy import select

    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id, name="Invite Audit Co")

    async with realdb.client(key=TENANT, role="admin") as c:
        created = await c.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": "invited@supplier.example", "full_name": "Invited"},
        )
    assert created.status_code == 201, created.text
    vendor_user_id = created.json()["user"]["id"]

    async with mk() as s:
        rows = list(
            (
                await s.execute(select(AuditLog).where(AuditLog.action == "vendor_user.invited"))
            ).scalars()
        )
    mine = [r for r in rows if r.details.get("vendor_user_id") == vendor_user_id]
    assert len(mine) == 1, "inviting a portal user must leave exactly one audit row"
    assert str(mine[0].entity_id) == str(vendor_id)
    assert mine[0].actor_id is not None
    assert "invited@supplier.example" not in str(mine[0].details)
