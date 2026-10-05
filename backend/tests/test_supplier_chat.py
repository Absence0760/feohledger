"""Real-DB coverage for embedded supplier chat (AP + portal surfaces).

Covers ``backend/app/api/invoices.py`` chat routes and the portal chat routes
in ``backend/app/api/portal.py`` end-to-end against the live test tenants: lazy
thread creation, the append-only message + audit trail (body NOT in audit
details), @mention notifications, resolve/reopen RBAC, attachment key scheme +
content-type allowlist + cross-tenant download gate, the org feature flag, the
templates endpoint, vendor scoping, AP-author-id masking, and the supplier
email path.

Follows the ``realdb`` conventions in ``test_contracts.py`` /
``test_portal_self_service.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select

from app.api.deps import create_vendor_access_token
from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import Notification
from app.models.supplier_chat import SupplierChatMessage, SupplierChatThread
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog

TENANT = "a"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_control_factory(realdb, monkeypatch):
    """Point `control_session_factory` (used by notify_event /
    resolve_role_user_ids) at a fresh engine bound to THIS test's event loop
    AND this slot's own control-plane DB.

    The production code reads control-plane users through the module-global
    `control_session_factory`, whose engine is bound to whichever loop first
    touched it. Across function-scoped async tests that stale binding raises a
    cross-loop error. Patching it per test mirrors `test_notification_dispatch`
    and leaves the production call path unchanged. Goes through
    ``realdb.control_sessionmaker()`` (not a bare
    ``create_async_engine(settings.database_url)``) because the harness's orgs
    live in this process's per-slot control-plane database, not the real,
    shared one — see ``control_db_name_for_slot`` in conftest.py.
    """
    monkeypatch.setattr("app.database.control_session_factory", realdb.control_sessionmaker())
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _add_vendor(mk, org_id, *, name="Acme Supplies", email=None) -> uuid.UUID:
    vid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vid,
                organization_id=org_id,
                name=name,
                email=email,
                status="active",
                source="manual",
            )
        )
        await s.commit()
    return vid


async def _add_invoice(mk, org_id, *, vendor_id=None, number=None) -> uuid.UUID:
    iid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=iid,
                organization_id=org_id,
                invoice_number=number or f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Acme Supplies",
                amount=Decimal("100.00"),
                status=InvoiceStatus.new,
                vendor_id=vendor_id,
            )
        )
        await s.commit()
    return iid


async def _add_vendor_user(mk, vendor_id) -> uuid.UUID:
    vu_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()
    return vu_id


def _portal_client(realdb, vu_id, vendor_id):
    token = create_vendor_access_token(vu_id, vendor_id)
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _set_chat_enabled(realdb, org_id, enabled: bool) -> None:
    """Flip Organization.settings.supplier_chat.enabled on the control DB."""
    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["supplier_chat"] = {"enabled": enabled}
        org.settings = settings
        await s.commit()
    # Reset to default after the test would be nice, but each test runs against a
    # fresh truncate; settings live on the control DB so we restore explicitly.


async def _clear_chat_flag(realdb, org_id) -> None:
    from app.models.organization import Organization

    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings.pop("supplier_chat", None)
        org.settings = settings
        await s.commit()


# ---------------------------------------------------------------------------
# AP side
# ---------------------------------------------------------------------------


async def test_get_chat_empty_returns_lazy_thread(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.get(f"/api/invoices/{invoice_id}/chat")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] is None
    assert body["status"] == "open"
    assert body["messages"] == []


async def test_post_message_ap_lazy_creates_thread(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Could you confirm the PO number?"},
        )
        assert resp.status_code == 201, resp.text
        msg = resp.json()
        assert msg["author_role"] == "ap_team"
        assert msg["body"] == "Could you confirm the PO number?"

        # A second post does NOT create a second thread.
        await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "Following up."})

    async with mk() as s:
        threads = (
            (
                await s.execute(
                    select(SupplierChatThread).where(SupplierChatThread.invoice_id == invoice_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(threads) == 1
        msgs = (
            (
                await s.execute(
                    select(SupplierChatMessage).where(
                        SupplierChatMessage.thread_id == threads[0].id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(msgs) == 2


async def test_post_message_writes_audit_row(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    secret_body = "SENSITIVE-SECRET-BODY-12345"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": secret_body})

    async with mk() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "chat_message_posted")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_type == "invoice"
        assert row.entity_id == invoice_id
        # Body must NOT be in the audit details (PII rule).
        assert secret_body not in (str(row.details) or "")
        assert row.details["author_role"] == "ap_team"
        assert row.details["has_attachment"] is False


async def test_mention_notifies_user(realdb):
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    invoice_id = await _add_invoice(mk, org_id)
    mentioned = info.users["ap_clerk"]  # a real control-plane user id

    secret_body = "ANOTHER-SECRET-BODY-XYZ"
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": secret_body, "mention_user_ids": [str(mentioned)]},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["mention_user_ids"] == [str(mentioned)]

    async with mk() as s:
        notes = (
            (
                await s.execute(
                    select(Notification).where(
                        Notification.recipient_user_id == mentioned,
                        Notification.event_type == "chat_message",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notes) == 1
        # The notification body is PII-free — never the raw message text.
        assert secret_body not in (notes[0].body or "")
        assert secret_body not in notes[0].title


async def test_resolve_requires_role(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    # Seed a real thread by posting a message first, so resolve exercises the
    # full happy path (resolve must operate on an existing thread, not create
    # an empty one — see get_thread/404 guard in resolve_invoice_chat).
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        posted = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Please confirm the PO number."},
        )
        assert posted.status_code == 201, posted.text

    # A clerk cannot resolve.
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        denied = await c.post(f"/api/invoices/{invoice_id}/chat/resolve")
    assert denied.status_code == 403

    # A manager can.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        ok = await c.post(f"/api/invoices/{invoice_id}/chat/resolve")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert body["resolved_by"] is not None

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "invoice")))
            .scalars()
            .all()
        )
        assert "chat_thread_resolved" in actions


async def test_reopen_clears_resolution(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        posted = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Reopening this thread for follow-up."},
        )
        assert posted.status_code == 201, posted.text
        await c.post(f"/api/invoices/{invoice_id}/chat/resolve")
        reopened = await c.post(f"/api/invoices/{invoice_id}/chat/reopen")
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == "open"
    assert body["resolved_at"] is None
    assert body["resolved_by"] is None

    async with mk() as s:
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "invoice")))
            .scalars()
            .all()
        )
        assert "chat_thread_reopened" in actions


async def test_reopen_requires_role(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    # Seed and resolve a thread so reopen has something to act on.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        posted = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Thread to test the reopen gate."},
        )
        assert posted.status_code == 201, posted.text
        resolved = await c.post(f"/api/invoices/{invoice_id}/chat/resolve")
        assert resolved.status_code == 200, resolved.text

    # A clerk cannot reopen (symmetric with the resolve gate).
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        denied = await c.post(f"/api/invoices/{invoice_id}/chat/reopen")
    assert denied.status_code == 403

    # A manager can.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        ok = await c.post(f"/api/invoices/{invoice_id}/chat/reopen")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "open"


async def test_resolve_threadless_invoice_404s(realdb):
    """Resolving/reopening an invoice that has never had a chat message must
    404, not silently create an empty thread."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resolve = await c.post(f"/api/invoices/{invoice_id}/chat/resolve")
        reopen = await c.post(f"/api/invoices/{invoice_id}/chat/reopen")
    assert resolve.status_code == 404, resolve.text
    assert reopen.status_code == 404, reopen.text

    # No empty thread row was created as a side effect.
    async with mk() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(SupplierChatThread)
                .where(SupplierChatThread.invoice_id == invoice_id)
            )
        ).scalar_one()
    assert count == 0


async def test_attachment_upload_keys_by_org_and_rejects_bad_type(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        # A disallowed content type is rejected.
        bad = await c.post(
            f"/api/invoices/{invoice_id}/chat/attachments",
            files={"file": ("evil.exe", b"MZ\x90\x00", "application/x-msdownload")},
        )
        assert bad.status_code == 400

        # A valid PDF lands under <org_id>/chat/<invoice_id>/...
        ok = await c.post(
            f"/api/invoices/{invoice_id}/chat/attachments",
            files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert ok.status_code == 201, ok.text
        att = ok.json()["attachments"]
        assert len(att) == 1
        file_url = att[0]["file_url"]
        assert f"/chat/file/{org_id}/chat/{invoice_id}/" in file_url

        # The key (stripped from file_url) downloads for the right org.
        file_key = file_url.split("/chat/file/", 1)[1]
        dl = await c.get(f"/api/invoices/chat/file/{file_key}")
        assert dl.status_code == 200
        assert dl.content == b"%PDF-1.4 fake"

        # A wrong-org key 404s (cross-tenant guard).
        foreign_key = f"{uuid.uuid4()}/chat/{invoice_id}/x/doc.pdf"
        bad_dl = await c.get(f"/api/invoices/chat/file/{foreign_key}")
        assert bad_dl.status_code == 404


async def test_attachment_upload_preserves_multiple_mentions(realdb):
    # Regression: the attachment Form param must bind repeated `mention_user_ids`
    # keys (how the frontend fans out a string[]) to a list — not silently keep
    # only the first. See app/api/invoices.py::post_invoice_chat_attachment.
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    invoice_id = await _add_invoice(mk, org_id)
    m1 = str(info.users["ap_clerk"])
    m2 = str(info.users["cfo"])

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        ok = await c.post(
            f"/api/invoices/{invoice_id}/chat/attachments",
            files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
            # Repeated keys — httpx serialises a list value as repeated multipart
            # fields, exactly matching the browser FormData fan-out in api.ts.
            data={"mention_user_ids": [m1, m2]},
        )
        assert ok.status_code == 201, ok.text
        assert sorted(ok.json()["mention_user_ids"]) == sorted([m1, m2])

    async with mk() as s:
        for mentioned in (info.users["ap_clerk"], info.users["cfo"]):
            notes = (
                (
                    await s.execute(
                        select(Notification).where(
                            Notification.recipient_user_id == mentioned,
                            Notification.event_type == "chat_message",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(notes) == 1


async def test_org_flag_disabled_blocks_post(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    invoice_id = await _add_invoice(mk, org_id)
    await _set_chat_enabled(realdb, org_id, False)
    try:
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            posted = await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "blocked"})
            assert posted.status_code == 403
            got = await c.get(f"/api/invoices/{invoice_id}/chat")
            assert got.status_code == 200
            assert got.json()["messages"] == []
            assert got.json()["id"] is None
    finally:
        await _clear_chat_flag(realdb, org_id)


async def test_templates_endpoint_returns_three(realdb):
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get("/api/invoices/chat/templates")
    assert resp.status_code == 200, resp.text
    keys = {t["key"] for t in resp.json()}
    assert keys == {"missing_po", "amount_mismatch", "payment_status"}


# ---------------------------------------------------------------------------
# portal side
# ---------------------------------------------------------------------------


async def test_portal_get_chat_scoped_to_vendor(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    vendor_a = await _add_vendor(mk, org_id, name="Vendor A")
    vendor_b = await _add_vendor(mk, org_id, name="Vendor B")
    vu_a = await _add_vendor_user(mk, vendor_a)
    inv_b = await _add_invoice(mk, org_id, vendor_id=vendor_b)

    # Vendor A asks about Vendor B's invoice → 404 (same as not-found).
    async with _portal_client(realdb, vu_a, vendor_a) as c:
        resp = await c.get(f"/api/portal/invoices/{inv_b}/chat")
        assert resp.status_code == 404
        missing = await c.get(f"/api/portal/invoices/{uuid.uuid4()}/chat")
        assert missing.status_code == 404
        assert resp.json()["detail"] == missing.json()["detail"]


async def test_portal_post_message_records_supplier_author(realdb):
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    org_id = info.org_id
    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as c:
        resp = await c.post(
            f"/api/portal/invoices/{invoice_id}/chat",
            json={"body": "The PO number is PO-4471."},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["author_role"] == "supplier"

    async with mk() as s:
        msg = (
            await s.execute(
                select(SupplierChatMessage).where(SupplierChatMessage.author_role == "supplier")
            )
        ).scalar_one()
        assert msg.author_user_id == vu_id

        # actor_id is None in the audit row (a VendorUser is not a control user).
        audit = (
            await s.execute(select(AuditLog).where(AuditLog.action == "chat_message_posted"))
        ).scalar_one()
        assert audit.actor_id is None

        # AP managers were notified (control-plane Users only).
        notes = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.event_type == "chat_message")
            )
        ).scalar()
        assert notes >= 1


async def test_portal_response_masks_ap_author_id(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id)

    # AP posts a message first.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "Hello from AP"})

    # The supplier reads the thread — the AP message exposes no author_user_id.
    async with _portal_client(realdb, vu_id, vendor_id) as c:
        resp = await c.get(f"/api/portal/invoices/{invoice_id}/chat")
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["author_role"] == "ap_team"
    assert "author_user_id" not in msgs[0]
    assert msgs[0]["author_name"] is not None


async def test_ap_message_emails_supplier_with_portal_link(realdb, monkeypatch):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)

    # Vendor WITH an email gets the portal-link mail.
    vendor_id = await _add_vendor(mk, org_id, email="supplier@example.com")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-EMAIL-1")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        r = await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "ping"})
        assert r.status_code == 201, r.text

    assert len(sent) == 1
    msg = sent[0]
    assert msg.to == "supplier@example.com"
    assert f"/portal/invoices/{invoice_id}/chat" in msg.body_text
    # PII-free: invoice number present, raw message body absent.
    assert "INV-EMAIL-1" in msg.body_text
    assert "ping" not in msg.body_text

    # Vendor WITHOUT an email → no send.
    sent.clear()
    vendor2 = await _add_vendor(mk, org_id, name="No Email Co", email=None)
    invoice2 = await _add_invoice(mk, org_id, vendor_id=vendor2)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await c.post(f"/api/invoices/{invoice2}/chat", json={"body": "ping2"})
    assert sent == []


async def test_ap_message_email_gated_by_notifications_flag(realdb, monkeypatch):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id

    sent: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        sent.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)
    # Turn notifications off — the direct supplier email must not fire.
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "notifications_enabled", False, raising=True)

    vendor_id = await _add_vendor(mk, org_id, email="supplier@example.com")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id)
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "ping"})
    assert sent == []


async def test_attachment_url_is_built_for_the_surface_reading_it(realdb):
    """Each surface sees a URL it can actually fetch, whoever posted the file.

    The persisted ``file_url`` was echoed verbatim, so it named the POSTING
    surface's route: a supplier's attachment reached the AP modal as a
    ``/api/portal/...`` URL an employee JWT is refused on, an AP attachment
    reached the portal as an ``/api/invoices/...`` URL a vendor JWT is refused
    on — and that AP URL wasn't even a route (``/api/invoices/{id}/chat/file``
    didn't exist), so AP-side downloads 404'd for everyone. The URL is now
    rebuilt from ``file_key`` per surface on read (decisions §226).
    """
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as p:
        up = await p.post(
            f"/api/portal/invoices/{invoice_id}/chat/attachments",
            files={"file": ("vendor.pdf", b"%PDF-1.4 from vendor", "application/pdf")},
        )
        assert up.status_code == 201, up.text

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        up = await c.post(
            f"/api/invoices/{invoice_id}/chat/attachments",
            files={"file": ("ap.pdf", b"%PDF-1.4 from ap", "application/pdf")},
        )
        assert up.status_code == 201, up.text

        thread = (await c.get(f"/api/invoices/{invoice_id}/chat")).json()
        ap_urls = [a["file_url"] for m in thread["messages"] for a in m["attachments"]]
        assert len(ap_urls) == 2
        for url in ap_urls:
            assert url.startswith(f"/api/invoices/{invoice_id}/chat/file/{org_id}/chat/")
            dl = await c.get(url)
            assert dl.status_code == 200, (url, dl.status_code)
        assert {(await c.get(u)).content for u in ap_urls} == {
            b"%PDF-1.4 from vendor",
            b"%PDF-1.4 from ap",
        }

    async with _portal_client(realdb, vu_id, vendor_id) as p:
        thread = (await p.get(f"/api/portal/invoices/{invoice_id}/chat")).json()
        portal_urls = [a["file_url"] for m in thread["messages"] for a in m["attachments"]]
        assert len(portal_urls) == 2
        for url in portal_urls:
            assert url.startswith(f"/api/portal/invoices/{invoice_id}/chat/file/")
            dl = await p.get(url)
            assert dl.status_code == 200, (url, dl.status_code)


async def test_portal_attachment_upload_and_download(realdb):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _add_vendor(mk, org_id)
    vu_id = await _add_vendor_user(mk, vendor_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id)

    async with _portal_client(realdb, vu_id, vendor_id) as c:
        up = await c.post(
            f"/api/portal/invoices/{invoice_id}/chat/attachments",
            files={"file": ("inv.pdf", b"%PDF-1.4 vendor", "application/pdf")},
        )
        assert up.status_code == 201, up.text
        att = up.json()["attachments"][0]
        assert att["file_url"].startswith(f"/api/portal/invoices/{invoice_id}/chat/file/")
        file_key = att["file_url"].split("/chat/file/", 1)[1]
        assert file_key.startswith(f"{org_id}/chat/{invoice_id}/")

        dl = await c.get(f"/api/portal/invoices/{invoice_id}/chat/file/{file_key}")
        assert dl.status_code == 200
        assert dl.content == b"%PDF-1.4 vendor"

        # Wrong-org key 404s.
        foreign = f"{uuid.uuid4()}/chat/{invoice_id}/x/inv.pdf"
        bad = await c.get(f"/api/portal/invoices/{invoice_id}/chat/file/{foreign}")
        assert bad.status_code == 404
