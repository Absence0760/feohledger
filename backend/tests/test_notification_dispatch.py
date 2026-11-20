"""Real-DB coverage for the notification dispatch hook.

Asserts that the centralized `transition_invoice` hook (and the explicit
`assign_reviewer` hook) create the right `Notification` rows for the right
recipients, respect per-user `notification_prefs`, and never let a failing
email adapter roll back the status transition or its audit row.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import (
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_PAID,
    Notification,
)
from app.models.user import User
from app.models.workflow import AuditLog
from app.services.workflow_engine import transition_invoice


@pytest_asyncio.fixture(autouse=True)
async def _fresh_control_factory(realdb, monkeypatch):
    """Point `control_session_factory` (used by notify_event /
    resolve_role_user_ids) at a fresh engine bound to THIS test's event loop
    AND this slot's own control-plane DB.

    The production code reads control-plane users through the module-global
    `control_session_factory`, whose engine is bound to whichever loop first
    touched it. Across function-scoped async tests that stale binding raises
    asyncpg's "another operation is in progress". Patching it per test mirrors
    how the audit-dispatch tests already isolate `control_session_factory`, and
    leaves the production call path unchanged. Goes through
    ``realdb.control_sessionmaker()`` (not a bare
    ``create_async_engine(settings.database_url)``) because the harness's orgs
    live in this process's per-slot control-plane database, not the real,
    shared one — see ``control_db_name_for_slot`` in conftest.py.
    """
    monkeypatch.setattr("app.database.control_session_factory", realdb.control_sessionmaker())
    yield


async def _add_invoice(mk, org_id, *, uploaded_by_id=None, status=InvoiceStatus.ready_for_review):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            vendor_name="Globex Corp",
            amount=Decimal("500.00"),
            currency="USD",
            status=status,
            uploaded_by_id=uploaded_by_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return inv.id


async def _set_prefs(ctrl_mk, user_id, prefs):
    async with ctrl_mk() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        u.notification_prefs = prefs
        await s.commit()


async def _reset_prefs(ctrl_mk, user_id):
    await _set_prefs(ctrl_mk, user_id, {})


async def _notifications_for(mk, recipient_id, event_type=None):
    async with mk() as s:
        q = select(Notification).where(Notification.recipient_user_id == recipient_id)
        if event_type:
            q = q.where(Notification.event_type == event_type)
        return (await s.execute(q)).scalars().all()


async def test_approved_notifies_uploader(realdb):
    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    await _reset_prefs(ctrl_mk, uploader)

    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.commit()

    rows = await _notifications_for(mk, uploader, EVENT_INVOICE_APPROVED)
    assert len(rows) == 1
    assert rows[0].entity_id == inv_id
    assert "INV-" in rows[0].title


async def test_paid_notifies_uploader_and_ap_managers(realdb):
    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    ap_manager = realdb.info("a").users["ap_manager"]
    await _reset_prefs(ctrl_mk, uploader)
    await _reset_prefs(ctrl_mk, ap_manager)

    # Reach `paid` from `payment_scheduled` (a valid transition).
    inv_id = await _add_invoice(
        mk, org_id, uploaded_by_id=uploader, status=InvoiceStatus.payment_scheduled
    )

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.paid, action_name="invoice.paid")
        await s.commit()

    assert len(await _notifications_for(mk, uploader, EVENT_INVOICE_PAID)) == 1
    assert len(await _notifications_for(mk, ap_manager, EVENT_INVOICE_PAID)) == 1


async def test_in_app_false_suppresses_row(realdb):
    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    await _set_prefs(ctrl_mk, uploader, {"invoice_approved": {"email": True, "in_app": False}})

    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.commit()

    assert await _notifications_for(mk, uploader, EVENT_INVOICE_APPROVED) == []
    await _reset_prefs(ctrl_mk, uploader)


async def test_email_false_suppresses_send_but_keeps_in_app(realdb, monkeypatch):
    sent: list = []

    class _SpyAdapter:
        async def send(self, message):
            sent.append(message)

    monkeypatch.setattr(
        "app.services.notification_dispatch.get_email_adapter",
        lambda: _SpyAdapter(),
        raising=False,
    )
    # get_email_adapter is imported lazily inside _send_email_best_effort, so
    # patch the source module the import resolves to.
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter())

    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    await _set_prefs(ctrl_mk, uploader, {"invoice_approved": {"email": False, "in_app": True}})

    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.commit()

    assert len(await _notifications_for(mk, uploader, EVENT_INVOICE_APPROVED)) == 1
    assert sent == []  # email suppressed by pref
    await _reset_prefs(ctrl_mk, uploader)


async def test_failing_email_does_not_break_transition_or_audit(realdb, monkeypatch):
    class _BoomAdapter:
        async def send(self, message):
            raise RuntimeError("smtp down")

    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _BoomAdapter())

    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    await _reset_prefs(ctrl_mk, uploader)  # email on by default

    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.commit()

    # Transition committed despite the email blowing up.
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.approved
        audit = (
            await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.entity_id == inv_id,
                    AuditLog.action == "invoice.approved",
                )
            )
        ).scalar()
        assert audit == 1
    # In-app row still landed.
    assert len(await _notifications_for(mk, uploader, EVENT_INVOICE_APPROVED)) == 1


async def test_kill_switch_suppresses_all(realdb, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "notifications_enabled", False)

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]

    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.commit()

    assert await _notifications_for(mk, uploader, EVENT_INVOICE_APPROVED) == []


async def test_paid_notification_logs_a_failed_manager_lookup_by_class(monkeypatch, caplog):
    """A failed AP-manager lookup still notifies the uploader, and is logged by
    exception CLASS (never the message, which may carry an address) instead of
    silently dropping the managers."""
    from types import SimpleNamespace

    import app.services.notification_dispatch as dispatch
    import app.services.vendor_notifications as vendor_notifications
    from app.services import workflow_engine

    secret = "manager@example.test"
    sent: dict = {}

    async def _lookup_fails(_org_id, _role):
        raise LookupError(secret)

    async def _capture(_db, **kw):
        sent.update(kw)

    async def _no_vendor(*_a, **_k):
        return None

    monkeypatch.setattr(dispatch, "resolve_role_user_ids", _lookup_fails)
    monkeypatch.setattr(dispatch, "notify_event", _capture)
    monkeypatch.setattr(vendor_notifications, "notify_vendor_of_invoice_event", _no_vendor)

    uploader = uuid.uuid4()
    invoice = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        uploaded_by_id=uploader,
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("10.00"),
        currency="USD",
    )
    with caplog.at_level("WARNING", logger="app.services.workflow_engine"):
        await workflow_engine._maybe_notify_transition(
            None, invoice, InvoiceStatus.paid, actor_id=None, details=None
        )
    assert sent["recipient_user_ids"] == [uploader]
    assert "LookupError" in caplog.text
    assert secret not in caplog.text
