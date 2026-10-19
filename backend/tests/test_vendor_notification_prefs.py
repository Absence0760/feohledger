"""Vendor-portal notification preferences — pure mapping, the GET/PATCH portal
endpoints (vendor-scoped, auth enforced), and the substance of the feature: a
paid / rejected invoice emails the vendor's portal users IF (and only if) their
preference allows it, best-effort.

Mirrors `test_portal_self_service.py` (portal client via a vendor JWT) and
`test_notification_dispatch.py` (real-DB transition + spy email adapter).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.deps import create_access_token, create_vendor_access_token
from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import EVENT_INVOICE_PAID, EVENT_INVOICE_REJECTED
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog
from app.services.post_commit import drain_post_commit
from app.services.vendor_notifications import apply_pref_update, prefs_to_response

TENANT = "a"


# ---------------------------------------------------------------------------
# Pure mapping (no DB)
# ---------------------------------------------------------------------------


def test_prefs_to_response_defaults_on():
    # Empty blob = use defaults = everything on (opt-out, not opt-in).
    assert prefs_to_response({}) == {"email_on_payment": True, "email_on_rejection": True}
    assert prefs_to_response(None) == {"email_on_payment": True, "email_on_rejection": True}


def test_prefs_to_response_reads_stored_event_keys():
    stored = {
        EVENT_INVOICE_PAID: {"email": False},
        EVENT_INVOICE_REJECTED: {"email": True},
    }
    assert prefs_to_response(stored) == {
        "email_on_payment": False,
        "email_on_rejection": True,
    }


def test_apply_pref_update_partial_leaves_others_unchanged():
    start = {EVENT_INVOICE_REJECTED: {"email": False}}
    out = apply_pref_update(start, {"email_on_payment": False})
    # The new field is stored under its canonical event key...
    assert out[EVENT_INVOICE_PAID]["email"] is False
    # ...and the untouched one is preserved.
    assert out[EVENT_INVOICE_REJECTED]["email"] is False
    # Round-trips through the response mapping.
    assert prefs_to_response(out) == {"email_on_payment": False, "email_on_rejection": False}


def test_apply_pref_update_ignores_unknown_field():
    out = apply_pref_update({}, {"bogus": True})
    assert out == {}


# ---------------------------------------------------------------------------
# PII discipline on the send path (no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_email_failure_never_logs_the_recipient_address(caplog, monkeypatch):
    """`smtplib.SMTPRecipientsRefused` stringifies as
    `{'supplier@customer.com': (550, b'…')}`, and `logger.exception` attaches
    the traceback — and so that text — whatever the format string names. This
    module's docstring promises failures log "the event type only — never a
    recipient address"; the send guard used `logger.exception`, so the first
    refusal from a supplier's mail server wrote their address into the log
    sink. `notification_dispatch._send_email_best_effort` fixed the identical
    exposure on the control-plane side; this pins the vendor sibling.
    """
    import logging
    import smtplib

    from app.services import vendor_notifications as vn

    class _Boom:
        async def send(self, message):  # noqa: D401
            raise smtplib.SMTPRecipientsRefused({"supplier@customer.example": (550, b"nope")})

    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _Boom())
    caplog.set_level(logging.DEBUG)

    await vn._send_vendor_email_best_effort(
        "supplier@customer.example",
        "subject",
        "body",
        None,
        event_type=EVENT_INVOICE_PAID,
    )

    assert "supplier@customer.example" not in caplog.text
    # …but the failure is still visible enough to act on.
    assert "email send failed" in caplog.text
    assert "SMTPRecipientsRefused" in caplog.text


# ---------------------------------------------------------------------------
# Helpers (real DB)
# ---------------------------------------------------------------------------


async def _seed_vendor_and_user(mk, org_id, *, name="Acme Supply", prefs=None):
    vendor_id = uuid.uuid4()
    vu_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=name,
                organization_id=org_id,
                status="active",
                source="manual",
            )
        )
        s.add(
            VendorUser(
                id=vu_id,
                vendor_id=vendor_id,
                email=f"{vu_id}@portal.test",
                full_name="Portal User",
                hashed_password="x",
                is_active=True,
                notification_prefs=prefs or {},
            )
        )
        await s.commit()
    return vendor_id, vu_id


def _portal_client(realdb, vendor_user_id, vendor_id):
    token = create_vendor_access_token(vendor_user_id, vendor_id)
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _add_invoice(mk, org_id, vendor_id, *, status, uploaded_by_id=None):
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                vendor_id=vendor_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Acme Supply",
                amount=Decimal("500.00"),
                currency="USD",
                status=status,
                uploaded_by_id=uploaded_by_id,
            )
        )
        await s.commit()
    return inv_id


# ---------------------------------------------------------------------------
# Endpoints — GET / PATCH (vendor-scoped, auth enforced)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_preferences_defaults(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    client = _portal_client(realdb, vu_id, vendor_id)
    try:
        resp = await client.get("/api/portal/notification-preferences")
    finally:
        await client.aclose()
        await realdb.cleanup()

    assert resp.status_code == 200
    assert resp.json() == {"email_on_payment": True, "email_on_rejection": True}


@pytest.mark.asyncio
async def test_patch_preferences_persists_and_audits(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    client = _portal_client(realdb, vu_id, vendor_id)
    try:
        resp = await client.patch(
            "/api/portal/notification-preferences",
            json={"email_on_payment": False},
        )
    finally:
        await client.aclose()

    assert resp.status_code == 200
    # Partial update: payment off, rejection untouched (still default-on).
    assert resp.json() == {"email_on_payment": False, "email_on_rejection": True}

    # Persisted on the row.
    async with mk() as s:
        vu = (await s.execute(select(VendorUser).where(VendorUser.id == vu_id))).scalar_one()
        assert vu.notification_prefs[EVENT_INVOICE_PAID]["email"] is False

    # Audit row written, PII-free (field names only).
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "vendor_user.notification_prefs_updated"
                    )
                )
            )
            .scalars()
            .all()
        )
    await realdb.cleanup()
    assert len(rows) == 1
    assert rows[0].entity_id == vu_id
    assert rows[0].details["fields"] == ["email_on_payment"]


@pytest.mark.asyncio
async def test_full_form_save_audits_only_the_toggle_that_flipped(realdb):
    """The settings form sends both toggles on every save. The trail names a
    preference only when its EFFECTIVE value changed (#575) — an unset pref
    reads as on, so re-sending `true` for it is not a change — and a save that
    flips nothing writes no row."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    client = _portal_client(realdb, vu_id, vendor_id)
    try:
        for body in (
            {"email_on_payment": False, "email_on_rejection": True},
            {"email_on_payment": False, "email_on_rejection": True},
        ):
            resp = await client.patch("/api/portal/notification-preferences", json=body)
            assert resp.status_code == 200, resp.text
    finally:
        await client.aclose()

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "vendor_user.notification_prefs_updated",
                        AuditLog.entity_id == vu_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    await realdb.cleanup()
    assert [r.details["fields"] for r in rows] == [["email_on_payment"]]


@pytest.mark.asyncio
async def test_preferences_require_vendor_auth(realdb):
    # No token → 401.
    client = realdb.client(key=TENANT, role=None)
    try:
        resp = await client.get("/api/portal/notification-preferences")
    finally:
        await client.aclose()
        await realdb.cleanup()
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_preferences_reject_employee_token(realdb):
    # An employee (control-plane) JWT must not unlock the portal surface.
    info = realdb.info(TENANT)
    emp_token = create_access_token(info.users["admin"], info.org_id)
    client = realdb.client(key=TENANT, role=None)
    client.headers["Authorization"] = f"Bearer {emp_token}"
    try:
        resp = await client.get("/api/portal/notification-preferences")
    finally:
        await client.aclose()
        await realdb.cleanup()
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_is_scoped_to_caller_only(realdb):
    """A vendor user's PATCH touches ONLY their own row — a sibling portal user
    on the same vendor is unaffected."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)

    sibling_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorUser(
                id=sibling_id,
                vendor_id=vendor_id,
                email=f"{sibling_id}@portal.test",
                full_name="Sibling",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()

    client = _portal_client(realdb, vu_id, vendor_id)
    try:
        resp = await client.patch(
            "/api/portal/notification-preferences",
            json={"email_on_rejection": False},
        )
    finally:
        await client.aclose()
    assert resp.status_code == 200

    async with mk() as s:
        sibling = (
            await s.execute(select(VendorUser).where(VendorUser.id == sibling_id))
        ).scalar_one()
    await realdb.cleanup()
    # Sibling untouched → defaults.
    assert sibling.notification_prefs == {}


# ---------------------------------------------------------------------------
# Dispatch — the substance of the feature
# ---------------------------------------------------------------------------


class _SpyAdapter:
    def __init__(self, sink):
        self._sink = sink

    async def send(self, message):
        self._sink.append(message)


@pytest.mark.asyncio
async def test_paid_emails_vendor_when_pref_on(realdb, monkeypatch):
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)  # default prefs = on
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.payment_scheduled)

    from app.services.workflow_engine import transition_invoice

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.paid, action_name="invoice.paid")
        await s.commit()

    # The outbound email leg runs AFTER the caller's commit (services/post_commit),
    # so wait on it rather than racing it.
    await drain_post_commit()
    await realdb.cleanup()
    # The vendor's portal user is emailed. (The control-plane `paid` fan-out to
    # AP managers may also emit through the same spy adapter — assert on the
    # portal recipient specifically.)
    to_vendor = [m for m in sent if m.to == f"{vu_id}@portal.test"]
    assert len(to_vendor) == 1
    # PII-free template content only.
    assert "INV-" in to_vendor[0].subject
    assert "paid" in to_vendor[0].body_text.lower()


@pytest.mark.asyncio
async def test_rejected_suppressed_when_pref_off(realdb, monkeypatch):
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(
        mk, org_id, prefs={EVENT_INVOICE_REJECTED: {"email": False}}
    )
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.ready_for_review)

    from app.services.workflow_engine import transition_invoice

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.rejected, action_name="invoice.rejected")
        await s.commit()

    # The outbound email leg runs AFTER the caller's commit (services/post_commit),
    # so wait on it rather than racing it.
    await drain_post_commit()
    await realdb.cleanup()
    assert sent == []  # vendor opted out of rejection emails


@pytest.mark.asyncio
async def test_inactive_vendor_user_not_emailed(realdb, monkeypatch):
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    async with mk() as s:
        vu = (await s.execute(select(VendorUser).where(VendorUser.id == vu_id))).scalar_one()
        vu.is_active = False
        await s.commit()
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.payment_scheduled)

    from app.services.workflow_engine import transition_invoice

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.paid, action_name="invoice.paid")
        await s.commit()

    # The outbound email leg runs AFTER the caller's commit (services/post_commit),
    # so wait on it rather than racing it.
    await drain_post_commit()
    await realdb.cleanup()
    # The (now-inactive) portal user is never emailed. (A control-plane AP
    # manager may still receive the `paid` fan-out — assert on the portal one.)
    assert all(m.to != f"{vu_id}@portal.test" for m in sent)


@pytest.mark.asyncio
async def test_failing_email_does_not_break_transition(realdb, monkeypatch):
    class _BoomAdapter:
        async def send(self, message):
            raise RuntimeError("smtp down")

    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _BoomAdapter())

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, vu_id = await _seed_vendor_and_user(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.payment_scheduled)

    from app.services.workflow_engine import transition_invoice

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.paid, action_name="invoice.paid")
        await s.commit()

    # Transition + audit committed despite the failing email.
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status is InvoiceStatus.paid
        audit = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "invoice.paid", AuditLog.entity_id == inv_id
                    )
                )
            )
            .scalars()
            .all()
        )
    await realdb.cleanup()
    assert len(audit) == 1


@pytest.mark.asyncio
async def test_other_vendors_users_not_emailed(realdb, monkeypatch):
    """The fan-out is scoped to the invoice's OWN vendor — a portal user of a
    different vendor never receives the email."""
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_a, vu_a = await _seed_vendor_and_user(mk, org_id, name="Vendor A")
    vendor_b, vu_b = await _seed_vendor_and_user(mk, org_id, name="Vendor B")

    # Invoice belongs to vendor A.
    inv_id = await _add_invoice(mk, org_id, vendor_a, status=InvoiceStatus.payment_scheduled)

    from app.services.workflow_engine import transition_invoice

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.paid, action_name="invoice.paid")
        await s.commit()

    # The outbound email leg runs AFTER the caller's commit (services/post_commit),
    # so wait on it rather than racing it.
    await drain_post_commit()
    await realdb.cleanup()
    # Vendor A's user is emailed; vendor B's user never is.
    recipients = {m.to for m in sent}
    assert f"{vu_a}@portal.test" in recipients
    assert f"{vu_b}@portal.test" not in recipients


@pytest.mark.asyncio
async def test_direct_helper_only_fires_for_vendor_events(realdb):
    """`notify_vendor_of_invoice_event` is a no-op for non paid/rejected
    events even if a vendor user exists."""
    sent: list = []

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id, _ = await _seed_vendor_and_user(mk, org_id)
    inv_id = await _add_invoice(mk, org_id, vendor_id, status=InvoiceStatus.approved)

    import app.services.vendor_notifications as vn

    async def _boom():
        raise AssertionError("adapter should never be reached for invoice_approved")

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        # invoice_approved is not a vendor-facing event → immediate no-op.
        await vn.notify_vendor_of_invoice_event(s, event_type="invoice_approved", invoice=inv)
    await realdb.cleanup()
    assert sent == []
