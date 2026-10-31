"""Phase 4: a settled FeohLedger payment written back to QuickBooks (§295).

Driven end to end through `payment_erp_sync._sync_payments` over the `realdb`
tenant pair, with the ERP adapter faked at `get_erp_adapter`: the leg moves the
invoice to `paid`, THEN writes the payment back against the bill the invoice was
posted as, from the pay-from account the org mapped (our GL code, resolved to
its QuickBooks id under the current realm). A refusal is recorded coded and
flagged, never undoes the transition; a re-run never writes twice; a bill in
another QuickBooks company is never written to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.workflow import AuditLog, WorkflowDefinition, WorkflowInstance
from app.services import erp_payment_writeback
from app.services.erp_adapters.base import ErpPostResult, erp_refusal
from app.services.payment_erp_sync import _sync_payments

COMPANY = "9130"
REALM = f"quickbooks_online:{COMPANY}"


@pytest.fixture(autouse=True)
def _control_db(realdb, monkeypatch):
    # `_sync_payments` builds its own engines off `settings.database_url`
    # (see test_payment_erp_sync.py's fixture of the same name).
    monkeypatch.setattr(settings, "database_url", realdb.control_db_url())


class FakeQbo:
    erp_type = "quickbooks_online"
    supports_status_sync = True

    def __init__(self, result: ErpPostResult | None = None):
        self.post_payment = AsyncMock(
            return_value=result
            or ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal(0))
        )
        self.void_payment = AsyncMock(
            return_value=ErpPostResult(success=True, erp_document_id="BP-9")
        )


async def _configure(realdb, **erp_extra) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "bill_payment_bank_account": "1010",
                "bill_payment_card_account": "2050",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": COMPANY,
                    "org_id": str(org.id),
                    "connection_id": "conn-1",
                },
                **erp_extra,
            },
        }
        await s.commit()


async def _seed(realdb, *, method="ach", realm=REALM, bill="501"):
    info = realdb.info("a")
    run_id, inv_id, pay_id, corr = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(PaymentRun(id=run_id, status="completed", organization_id=info.org_id))
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                invoice_number=f"WB-{uuid.uuid4().hex[:6]}",
                vendor_name="Vendor Co",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.payment_scheduled,
                organization_id=info.org_id,
            )
        )
        for code, erp_id in (("1010", "35"), ("2050", "41")):
            s.add(
                GLAccount(
                    organization_id=info.org_id,
                    code=code,
                    name=code,
                    erp_account_id=erp_id,
                    erp_link_realm=REALM,
                )
            )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": bill, "erp_realm": realm},
            )
        )
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method=method,
                status="completed",
                completed_at=datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
            )
        )
        await s.commit()
    return run_id, inv_id, pay_id


async def _sync(realdb, run_id, adapter):
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        return await _sync_payments(run_id, realdb.info("a").org_id)


async def _rows(realdb, model, **where):
    async with realdb.sessionmaker("a")() as s:
        stmt = select(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return (await s.execute(stmt)).scalars().all()


async def test_a_settled_payment_is_written_back_against_its_bill(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()

    result = await _sync(realdb, run_id, adapter)

    assert result.transitioned == 1
    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid
    (payload,) = adapter.post_payment.await_args.args
    assert payload.payment_id == str(pay_id)
    assert payload.bill_id == "501"
    assert payload.amount == Decimal("100.00") and payload.currency == "USD"
    assert payload.paid_on.isoformat() == "2026-10-02"
    # The org's mapped bank account, as QuickBooks knows it.
    assert payload.pay_from_account_erp_id == "35" and payload.card is False
    actions = {r.action for r in await _rows(realdb, AuditLog, entity_id=pay_id)}
    assert "payment.erp_posted" in actions
    assert await _rows(realdb, APException, invoice_id=inv_id) == []


async def test_a_card_payment_uses_the_card_account(realdb):
    await _configure(realdb)
    run_id, _, _ = await _seed(realdb, method="virtual_card")
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    (payload,) = adapter.post_payment.await_args.args
    assert payload.card is True and payload.pay_from_account_erp_id == "41"


async def test_an_unlinked_pay_from_account_reaches_the_adapter_as_unmapped(realdb):
    await _configure(realdb, bill_payment_bank_account="9999")
    run_id, _, _ = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    (payload,) = adapter.post_payment.await_args.args
    assert payload.pay_from_account_erp_id is None  # the adapter refuses it


async def test_a_refused_write_back_is_coded_flagged_and_keeps_the_invoice_paid(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    refused = erp_refusal("QuickBooks Online", "payment_account_not_mapped")
    await _sync(realdb, run_id, FakeQbo(refused))

    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid  # the money moved
    [row] = [r for r in await _rows(realdb, AuditLog, entity_id=pay_id) if "erp_post" in r.action]
    assert row.action == "payment.erp_post_failed"
    assert row.details["refusal"]["code"] == "payment_account_not_mapped"
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    # The sentence is for an AP user, in a catalogue code; the refusal, payment,
    # run and bill are data on erp_report (decisions §319, §331).
    assert flag.description_code == "exception.erp_payment_writeback_failed"
    assert flag.description_params == {"erp": "QuickBooks Online"}
    for leaked in ("payment_account_not_mapped", str(pay_id), str(run_id), "/api/", "501"):
        assert leaked not in flag.description
    report = flag.erp_report
    assert report["kind"] == "payment_writeback_failed"
    assert (report["payment_id"], report["payment_run_id"]) == (str(pay_id), str(run_id))
    assert report["erp_document_id"] == "501"
    assert report["failure"] == "payment_account_not_mapped"
    assert report["refusal"]["code"] == "payment_account_not_mapped"


async def test_a_failed_write_back_with_no_refusal_code_is_still_explained(realdb):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    await _sync(realdb, run_id, FakeQbo(ErpPostResult(success=False, message="x", retryable=True)))
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.description_code == "exception.erp_payment_writeback_failed"
    assert flag.erp_report["failure"] == "erp_post_failed"
    assert flag.erp_report["refusal"] is None
    assert flag.erp_report["retryable"] is True


async def test_a_bill_left_partly_open_in_the_erp_is_flagged(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb)
    left = ErpPostResult(success=True, erp_document_id="BP-9", bill_balance_after=Decimal("2.00"))
    await _sync(realdb, run_id, FakeQbo(left))
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    assert flag.description_code == "exception.erp_payment_writeback_balance_open"
    assert flag.description_params == {
        "erp": "QuickBooks Online",
        "amount": "2.00",
        "currency": "USD",
    }
    assert "2.00 USD" in flag.description and "bill=" not in flag.description
    assert flag.erp_report["kind"] == "payment_writeback_balance_open"
    assert flag.erp_report["bill_balance_after"] == "2.00"
    assert flag.erp_report["erp_payment_id"] == "BP-9"


async def test_a_bill_in_another_quickbooks_company_is_never_written_to(realdb):
    await _configure(realdb)
    run_id, inv_id, _ = await _seed(realdb, realm="quickbooks_online:sandbox-1")
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    adapter.post_payment.assert_not_awaited()
    [invoice] = await _rows(realdb, Invoice, id=inv_id)
    assert invoice.status == InvoiceStatus.paid


async def test_a_retry_of_the_sync_reaches_the_adapter_with_the_same_key(realdb):
    """`/sync-erp` re-runs the pass; the invoice is already paid, the leg
    writes back again, and the adapter's own idempotency (the payment id) is
    what keeps QuickBooks at one bill payment."""
    await _configure(realdb)
    run_id, _, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)
    await _sync(realdb, run_id, adapter)
    keys = [call.args[0].payment_id for call in adapter.post_payment.await_args_list]
    assert keys == [str(pay_id), str(pay_id)]


async def test_voiding_the_payment_voids_what_was_written(realdb, monkeypatch):
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)

    # The void write-back reads the org through the shared control-plane
    # factory, which the harness points at this slot.
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.WROTE
    (payload,) = adapter.void_payment.await_args.args
    assert payload.payment_id == str(pay_id) and payload.bill_id == "501"
    actions = {r.action for r in await _rows(realdb, AuditLog, entity_id=pay_id)}
    assert "payment.erp_voided" in actions

    adapter.void_payment.return_value = ErpPostResult(success=False, message="x", retryable=True)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.void_written_payment(realdb.info("a").org_id, pay_id)
    assert outcome == erp_payment_writeback.FAILED
    [flag] = await _rows(realdb, APException, invoice_id=inv_id)
    assert flag.exception_type == "erp_reconciliation"
    assert flag.description_code == "exception.erp_payment_void_writeback_failed"
    assert str(pay_id) not in flag.description and "erp_void_failed" not in flag.description
    assert flag.erp_report["kind"] == "payment_void_writeback_failed"
    assert flag.erp_report["payment_id"] == str(pay_id)
    assert flag.erp_report["payment_run_id"] == str(run_id)
    assert flag.erp_report["failure"] == "erp_void_failed"


async def test_a_payment_voided_before_the_write_back_is_not_written(realdb):
    """Re-read before the ERP call: a void that committed after the sync's
    transition must not leave a BillPayment for a voided payment."""
    await _configure(realdb)
    _, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    async with realdb.sessionmaker("a")() as s:
        payment = await s.get(Payment, pay_id)
        invoice = await s.get(Invoice, inv_id)
        await s.execute(
            Payment.__table__.update().where(Payment.id == pay_id).values(status="voided")
        )
        await s.commit()
        state = {"erp_reference": "501", "erp_realm": REALM}
        async with realdb.control_sessionmaker()() as c:
            org = await c.get(Organization, realdb.info("a").org_id)
        outcome = await erp_payment_writeback.write_back_payment(
            s,
            adapter=adapter,
            erp_config=org.settings["erp"],
            payment=payment,
            invoice=invoice,
            state_data=state,
        )
    assert outcome == erp_payment_writeback.SKIPPED
    adapter.post_payment.assert_not_awaited()


async def test_the_void_records_its_erp_void_and_the_sweep_finishes_it(realdb):
    """The intent to void in the ERP is written in the void's own transaction,
    so a process that dies before the background task still leaves it to the
    status-sync sweep (money-path review, §295)."""
    await _configure(realdb)
    run_id, inv_id, pay_id = await _seed(realdb)
    adapter = FakeQbo()
    await _sync(realdb, run_id, adapter)

    # The void itself, with the background task suppressed (the process "died").
    with patch("app.services.erp_payment_writeback.dispatch_void_writeback") as dispatched:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/{pay_id}/void", json={"reason": "wrong vendor"})
    assert resp.status_code == 200, resp.text
    dispatched.assert_called_once()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert instance.state_data[erp_payment_writeback.PENDING_VOIDS_KEY] == [str(pay_id)]

    async with realdb.control_sessionmaker()() as c:
        org = await c.get(Organization, realdb.info("a").org_id)
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        left = await erp_payment_writeback.resume_pending_voids(org)
    assert left == 0
    adapter.void_payment.assert_awaited_once()
    [instance] = await _rows(realdb, WorkflowInstance, invoice_id=inv_id)
    assert erp_payment_writeback.PENDING_VOIDS_KEY not in instance.state_data


async def test_an_accepted_short_settlement_writes_back_what_was_settled(realdb):
    await _configure(realdb)
    _, inv_id, pay_id = await _seed(realdb)
    async with realdb.sessionmaker("a")() as s:
        await s.execute(
            Payment.__table__.update()
            .where(Payment.id == pay_id)
            .values(settled_amount=Decimal("95.00"), settled_currency="USD")
        )
        await s.execute(
            Invoice.__table__.update().where(Invoice.id == inv_id).values(status="paid")
        )
        await s.commit()
    adapter = FakeQbo()
    with patch("app.services.erp_adapters.get_erp_adapter", return_value=adapter):
        outcome = await erp_payment_writeback.write_back_settled_payment(
            realdb.info("a").org_id, pay_id
        )
    assert outcome == erp_payment_writeback.WROTE
    (payload,) = adapter.post_payment.await_args.args
    assert payload.amount == Decimal("95.00")  # what reached the supplier
