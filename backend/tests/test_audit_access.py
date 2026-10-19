"""Access-control auditing (SOX): log who VIEWED regulated records.

Covers the `log_access` helper's field-diff sibling `build_field_diff` (money
stays string-Decimal) and the instrumented sensitive reads (vendor detail,
payment detail, the audit-trail view). The PII invariant is the load-bearing
assertion: a view-event records field-NAMES, never the regulated VALUES.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.audit_access import build_field_diff

# --------------------------------------------------------------------------
# build_field_diff — pure unit (no DB)
# --------------------------------------------------------------------------


def test_build_field_diff_only_includes_changed_fields():
    before = {"vendor_name": "Acme", "amount": Decimal("90.00")}
    after = {"vendor_name": "Acme", "amount": Decimal("100.00")}
    diff = build_field_diff(before, after, ["vendor_name", "amount"])
    assert diff == {"amount": {"old": "90.00", "new": "100.00"}}


def test_build_field_diff_treats_null_and_blank_as_the_same_value():
    """NULL and "" are the same absence of a value: one becoming the other is
    not an edit (it wrote phantom `null -> ""` rows onto the SOX trail). A
    blank becoming a real value, or the reverse, still is."""
    before = {"po_number": None, "notes": "", "description": None, "project": "P1"}
    after = {"po_number": "", "notes": None, "description": "set", "project": ""}
    diff = build_field_diff(before, after, list(before))
    assert diff == {
        "description": {"old": None, "new": "set"},
        "project": {"old": "P1", "new": ""},
    }


def test_build_field_diff_serialises_money_as_string_not_float():
    diff = build_field_diff(
        {"amount": Decimal("1234567.89")}, {"amount": Decimal("0.01")}, ["amount"]
    )
    assert diff["amount"] == {"old": "1234567.89", "new": "0.01"}
    assert isinstance(diff["amount"]["old"], str)
    assert isinstance(diff["amount"]["new"], str)


def test_build_field_diff_coerces_float_through_decimal():
    """A stray float (mis-typed money) must not leak as a lossy float."""
    diff = build_field_diff({"x": 0.1}, {"x": 0.2}, ["x"])
    assert diff["x"] == {"old": "0.1", "new": "0.2"}


# --------------------------------------------------------------------------
# Vendor detail view writes a vendor.viewed row with field-NAMES, not values
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_view_logs_field_names_not_values(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = uuid.uuid4()
    secret_tax = "98-7654321"
    secret_bank = {"account_number": "000123456789", "routing": "021000021"}
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name="Globex",
                tax_id=secret_tax,
                bank_details=secret_bank,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/vendors/{vendor_id}")
    assert resp.status_code == 200

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "vendor.viewed",
                        AuditLog.entity_id == vendor_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "viewing a vendor must write a vendor.viewed row"
    details = rows[-1].details or {}
    # Field-names recorded …
    assert set(details.get("fields", [])) == {"tax_id", "bank_details"}
    # … but NO regulated VALUE anywhere in the audit payload.
    blob = str(details)
    assert secret_tax not in blob
    assert "000123456789" not in blob
    assert "021000021" not in blob


@pytest.mark.asyncio
async def test_payment_detail_view_logs_payment_viewed(realdb):
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.payment import Payment

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=invoice_id,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("500.00"),
                status=InvoiceStatus.approved,
            )
        )
        s.add(
            Payment(
                id=payment_id,
                invoice_id=invoice_id,
                correlation_id=uuid.uuid4(),
                amount=Decimal("500.00"),
                status="completed",
                method="ach",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/payments/{payment_id}")
    assert resp.status_code == 200

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "payment.viewed",
                        AuditLog.entity_id == payment_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "viewing a payment must write a payment.viewed row"


@pytest.mark.asyncio
async def test_audit_trail_view_is_itself_audited(realdb):
    """Viewing an invoice's audit trail writes an audit.viewed row (SOX: log
    access to the audit log itself)."""
    from app.models.invoice import Invoice, InvoiceStatus

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id = uuid.uuid4()
    corr = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=corr,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("10.00"),
                status=InvoiceStatus.new,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(f"/api/invoices/{inv_id}/audit-log")
    assert resp.status_code == 200

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "audit.viewed",
                        AuditLog.entity_id == inv_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "viewing the audit trail must itself be audited"


def test_field_diff_keeps_list_values_as_real_lists():
    """A list-valued field (e.g. the GL codes on a line-item edit) must land in
    the JSONB as a list, not as the opaque repr `"['6100']"` — and its Decimal
    members must still serialise exactly."""
    from decimal import Decimal

    from app.services.audit_access import build_field_diff

    diff = build_field_diff(
        {"gl_accounts": ["6100"], "totals": [Decimal("10.50")]},
        {"gl_accounts": ["6200", "6300"], "totals": [Decimal("11.50")]},
        ["gl_accounts", "totals"],
    )
    assert diff["gl_accounts"] == {"old": ["6100"], "new": ["6200", "6300"]}
    assert diff["totals"] == {"old": ["10.50"], "new": ["11.50"]}
