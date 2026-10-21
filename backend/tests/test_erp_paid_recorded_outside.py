"""An ERP `Paid` for an invoice FeohLedger never paid — recorded, over a real DB.

`services/erp_reported_status.record_erp_reported_payment` (issue #517,
decisions §303): the ERP-led path of the no-rail pilot, shared by the ERP
webhook and the ERP status sweep. The handler's routing to it is pinned
(mock-based) in `test_erp_webhook_transitions.py`; this drives the helper itself
against real rows, through `services/external_payment` — so the invoice really
reaches `paid` with a `completed` external payment behind it, and a refusal
really opens the BLOCKING `payment_reconciliation` exception instead of
vanishing.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import AuditLog
from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER
from app.services.workflow_engine import get_invoice_for_update

pytestmark = pytest.mark.asyncio


async def _seed(mk, org_id, *, number, status) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="ERP Paid Vendor",
                amount=Decimal("410.00"),
                currency="USD",
                status=status,
            )
        )
        await s.commit()
    return inv_id


async def _org(realdb, key="a") -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return await s.get(Organization, realdb.info(key).org_id)


async def _drive(realdb, inv_id, *, erp_document_id="BILLPAY-77"):
    from app.services.erp_reported_status import record_erp_reported_payment

    org = await _org(realdb)
    async with realdb.sessionmaker("a")() as s:
        invoice = await get_invoice_for_update(s, inv_id)
        await record_erp_reported_payment(
            s,
            invoice,
            org=org,
            erp_type="netsuite",
            erp_document_id=erp_document_id,
            event_id="ev-1",
        )
        await s.commit()


@pytest.mark.parametrize("status", [InvoiceStatus.posted_in_erp, InvoiceStatus.sent_to_erp])
async def test_erp_paid_records_an_external_payment(realdb, status):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed(mk, info.org_id, number=f"ERP-PAID-{status.value}", status=status)

    await _drive(realdb, inv_id)

    async with mk() as s:
        invoice = await s.get(Invoice, inv_id)
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
        actions = set(
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == payment.id)))
            .scalars()
            .all()
        )
        invoice_actions = set(
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == inv_id)))
            .scalars()
            .all()
        )
    assert invoice.status == InvoiceStatus.paid
    assert payment.status == "completed"
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert payment.reference == "BILLPAY-77"
    assert payment.amount == Decimal("410.00")
    assert payment.method is None  # the ERP doesn't say; NULL counts as 1099-reportable
    assert "payment.recorded_outside" in actions
    assert "invoice.paid_via_erp_report" in invoice_actions
    if status == InvoiceStatus.sent_to_erp:
        assert "invoice.erp_status_posted_in_erp" in invoice_actions


@pytest.mark.parametrize("status", [InvoiceStatus.posted_in_erp, InvoiceStatus.sent_to_erp])
async def test_a_refusal_opens_a_blocking_reconciliation_exception(realdb, status):
    """An open duplicate flag: recording would bury it under a closed item, so
    the ERP's report becomes a human's reconciliation task — never silent.

    The flag BLOCKS payment (decisions §303): the ERP says it paid, so once
    someone dismisses the duplicate flag nothing else may let FeohLedger pay the
    supplier again. And a refused record leaves a pushed invoice at
    `sent_to_erp` — never at the payable `posted_in_erp`."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed(mk, info.org_id, number=f"ERP-PAID-BLOCKED-{status.value}", status=status)
    async with mk() as s:
        s.add(
            APException(
                id=uuid.uuid4(),
                organization_id=info.org_id,
                invoice_id=inv_id,
                exception_type="duplicate",
                severity="error",
                description="seeded by test",
                status="open",
            )
        )
        await s.commit()

    await _drive(realdb, inv_id)

    async with mk() as s:
        invoice = await s.get(Invoice, inv_id)
        payments = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
        [recon] = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv_id,
                        APException.exception_type == "payment_reconciliation",
                    )
                )
            )
            .scalars()
            .all()
        )
        invoice_actions = set(
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_id == inv_id)))
            .scalars()
            .all()
        )
    assert invoice.status == status
    assert "invoice.erp_status_posted_in_erp" not in invoice_actions
    assert payments == []
    assert "external_payment_blocking_exception" in recon.description
    assert recon.status == "open"


async def test_erp_paid_for_an_invoice_in_a_run_blocks_the_run(realdb):
    """The ERP paid an invoice FeohLedger still holds in a draft run. Executing
    that run now would pay the supplier twice, so the flag must be the BLOCKING
    `payment_reconciliation`, and `/execute` must refuse the payment."""
    from app.models.payment import PaymentRun

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed(
        mk, info.org_id, number="ERP-PAID-IN-RUN", status=InvoiceStatus.posted_in_erp
    )
    run_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=info.org_id,
                status="draft",
                total_amount=Decimal("410.00"),
                initiated_by=info.users["ap_manager"],
            )
        )
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("410.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    await _drive(realdb, inv_id)

    async with mk() as s:
        [flag] = (
            (await s.execute(select(APException).where(APException.invoice_id == inv_id)))
            .scalars()
            .all()
        )
    assert flag.exception_type == "payment_reconciliation"
    assert "external_payment_in_run" in flag.description

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 200, resp.text
    async with mk() as s:
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    assert payment.status == "failed"
    assert payment.failure_reason == "invoice_blocked:payment_reconciliation"
