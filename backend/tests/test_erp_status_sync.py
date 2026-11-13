"""QuickBooks status sync-back, applied to real rows (decisions §295).

`services/erp_status_sync.apply_bill_report` decides what a bill's state in the
ERP means for the invoice it was posted from. These drive it against the
`realdb` tenant pair — so a bill paid in QuickBooks really reaches `paid`
through the ERP-paid path (with the ERP's own date and rail), and every
disagreement that would let FeohLedger pay a bill QuickBooks already settled,
part-paid or removed really opens the payment-BLOCKING exception the run
builder refuses on.

Also here: the realm index that routes a webhook's company to its tenant (and
refuses a second tenant on one company), the change-data-capture sweep, and
the paid_on / payment_method the generic ERP webhook now accepts.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.workflow import WorkflowDefinition, WorkflowInstance
from app.services import erp_status_sync
from app.services.erp_adapters.base import ErpBillReport, ErpBillState
from app.services.external_payment import EXTERNAL_PAYMENT_PROVIDER
from app.utils.dates import utc_today

REALM = "quickbooks_online:9130"
BILL = "501"


async def _seed(realdb, *, status, bill_id=BILL, realm=REALM, number=None):
    """An invoice posted to QuickBooks as ``bill_id`` under ``realm``."""
    info = realdb.info("a")
    inv_id, corr = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        defn = WorkflowDefinition(organization_id=info.org_id, name="d", steps_config={"steps": []})
        s.add(defn)
        s.add(
            Invoice(
                id=inv_id,
                correlation_id=corr,
                organization_id=info.org_id,
                invoice_number=number or f"QBO-{uuid.uuid4().hex[:8]}",
                vendor_name="Synced Vendor",
                amount=Decimal("250.00"),
                currency="USD",
                status=status,
            )
        )
        await s.flush()
        s.add(
            WorkflowInstance(
                correlation_id=corr,
                definition_id=defn.id,
                invoice_id=inv_id,
                state="completed",
                state_data={"erp_reference": bill_id, "erp_realm": realm},
            )
        )
        await s.commit()
    return inv_id, str(corr)


async def _org(realdb) -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return await s.get(Organization, realdb.info("a").org_id)


async def _apply(realdb, report: ErpBillReport, *, realm=REALM):
    async with realdb.sessionmaker("a")() as s:
        return await erp_status_sync.apply_bill_report(
            s, org=await _org(realdb), report=report, realm=realm, event_id="ev-1"
        )


async def _exceptions(realdb, inv_id):
    async with realdb.sessionmaker("a")() as s:
        return (
            (await s.execute(select(APException).where(APException.invoice_id == inv_id)))
            .scalars()
            .all()
        )


async def _invoice(realdb, inv_id) -> Invoice:
    async with realdb.sessionmaker("a")() as s:
        return await s.get(Invoice, inv_id)


def _report(corr, state, **kw) -> ErpBillReport:
    # A report naming payments a person made is a bill taken down outside us.
    kw.setdefault("reduced_outside", bool(kw.get("external_payment_ids")))
    return ErpBillReport(bill_id=BILL, state=state, correlation_id=corr, **kw)


# ---------------------------------------------------------------------------
# paid in QuickBooks
# ---------------------------------------------------------------------------


async def test_a_bill_paid_in_quickbooks_moves_the_invoice_to_paid(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    paid_on = utc_today() - timedelta(days=3)
    outcome = await _apply(
        realdb,
        _report(
            corr,
            ErpBillState.paid,
            paid_on=paid_on,
            payment_method="card",
            external_payment_ids=("901",),
        ),
    )
    assert outcome.action == "paid_recorded"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid
    async with realdb.sessionmaker("a")() as s:
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER
    assert payment.status == "completed"
    assert payment.reference == BILL
    # The ERP's own date and rail — a company card is off the payer's 1099.
    assert payment.completed_at.date() == paid_on
    assert payment.method == "card"

    # The same report again (a webhook and the sweep both see it) changes nothing.
    again = await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("901",)))
    assert again.action == "unchanged"


async def test_a_bill_paid_only_by_our_own_write_back_is_not_news(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    outcome = await _apply(realdb, _report(corr, ErpBillState.paid, paid_only_by_us=True))
    assert outcome.action == "unchanged"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_paid_in_quickbooks_while_feohledger_pays_too_blocks_the_run(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.payment_scheduled)
    outcome = await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("901",)))
    assert outcome.action == "double_payment_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"  # payment-blocking
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.payment_scheduled


async def test_a_bill_paid_in_quickbooks_after_feohledger_paid_it_is_flagged(realdb):
    """FeohLedger's rail paid the invoice (its write-back missing), then a person
    paid the bill in QuickBooks too: the money left twice, and someone must know."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.paid)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Payment(
                invoice_id=inv_id,
                amount=Decimal("250.00"),
                method="ach",
                status="completed",
                provider="mock",
            )
        )
        await s.commit()
    outcome = await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("9",)))
    assert outcome.action == "double_payment_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "erp_reconciliation"
    assert "paid twice" in flag.description
    assert flag.description_code == "exception.erp_paid_twice"
    # The bill and the source are data now, never spliced into the sentence.
    assert "bill=" not in flag.description and BILL not in flag.description
    assert flag.erp_report["kind"] == "bill_state"
    assert flag.erp_report["erp_document_id"] == BILL
    assert flag.erp_report["bill_state"] == "paid"


async def test_a_bill_recorded_paid_outside_and_paid_in_quickbooks_is_one_payment(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.paid)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Payment(
                invoice_id=inv_id,
                amount=Decimal("250.00"),
                method="check",
                status="completed",
                provider=EXTERNAL_PAYMENT_PROVIDER,
                reference="CHK-1001",
            )
        )
        await s.commit()
    outcome = await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("9",)))
    assert outcome.action == "unchanged"
    assert await _exceptions(realdb, inv_id) == []


async def test_a_partly_paid_bill_blocks_paying_it_in_full(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = _report(corr, ErpBillState.partially_paid, external_payment_ids=("901",))
    assert (await _apply(realdb, report)).action == "partial_payment_flagged"
    # De-duplicated: the sweep reporting the same bill opens nothing more.
    assert (await _apply(realdb, report)).action == "unchanged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"


async def test_a_bill_unpaid_again_after_an_erp_reported_payment_is_flagged(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("901",)))
    outcome = await _apply(realdb, _report(corr, ErpBillState.open))
    assert outcome.action == "unpaid_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "erp_reconciliation"


# ---------------------------------------------------------------------------
# deleted / voided in QuickBooks
# ---------------------------------------------------------------------------


async def test_a_deleted_bill_blocks_a_payable_invoice(realdb):
    inv_id, _ = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    # A deleted bill can no longer be read, so it carries no correlation id:
    # matched by the reference within the push's realm.
    outcome = await _apply(realdb, ErpBillReport(bill_id=BILL, state=ErpBillState.deleted))
    assert outcome.action == "deleted_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    assert "deleted or voided" in flag.description
    assert flag.description_code == "exception.erp_bill_deleted_blocking"
    assert flag.erp_report["source"] == "webhook" and flag.erp_report["event_id"] == "ev-1"


async def test_a_deleted_bill_of_a_closed_invoice_is_surfaced_not_blocking(realdb):
    inv_id, _ = await _seed(realdb, status=InvoiceStatus.done)
    await _apply(realdb, ErpBillReport(bill_id=BILL, state=ErpBillState.deleted))
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "erp_reconciliation"


async def test_a_bill_id_from_another_company_matches_nothing(realdb):
    """QuickBooks numbers bills per company (§284): bill 501 in the sandbox is
    not bill 501 in production."""
    inv_id, _ = await _seed(realdb, status=InvoiceStatus.posted_in_erp, realm="quickbooks_online:1")
    outcome = await _apply(realdb, ErpBillReport(bill_id=BILL, state=ErpBillState.deleted))
    assert outcome.action == "unmatched"
    assert await _exceptions(realdb, inv_id) == []


async def test_a_marker_naming_an_invoice_posted_as_another_bill_matches_nothing(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp, bill_id="777")
    outcome = await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("9",)))
    assert outcome.action == "unmatched"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


# ---------------------------------------------------------------------------
# the realm index: one tenant per company, and the webhook's routing
# ---------------------------------------------------------------------------


async def _connect(realdb, key: str, company: str) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(key).org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "oauth": {
                    "provider": "quickbooks_online",
                    "external_tenant_id": company,
                    "org_id": str(org.id),
                    "connection_id": "c-1",
                },
            },
        }
        await s.commit()


async def test_a_company_routes_to_its_one_tenant_and_a_second_is_refused(realdb):
    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    found = await erp_status_sync.org_for_company("quickbooks_online", company)
    assert found is not None and found.id == realdb.info("a").org_id
    assert await erp_status_sync.org_for_company("quickbooks_online", "unknown-co") is None

    with pytest.raises(IntegrityError):
        await _connect(realdb, "b", company)


async def test_a_tenant_that_switched_erp_is_not_synced(realdb):
    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        erp = dict(org.settings["erp"])
        erp["type"] = "netsuite"  # the OAuth block survives a switch (§270)
        org.settings = {**org.settings, "erp": erp}
        await s.commit()
    assert await erp_status_sync.org_for_company("quickbooks_online", company) is None


# ---------------------------------------------------------------------------
# the change-data-capture sweep
# ---------------------------------------------------------------------------


async def test_the_sweep_reads_changed_bills_and_applies_them(realdb, monkeypatch):
    from app.config import settings

    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    inv_id, corr = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, realm=f"quickbooks_online:{company}"
    )
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)

    class FakeQbo:
        erp_type = "quickbooks_online"
        supports_status_sync = True
        changed_bills = AsyncMock(return_value=[BILL])
        bill_report = AsyncMock(
            return_value=_report(corr, ErpBillState.paid, external_payment_ids=("901",))
        )

    with patch.object(erp_status_sync, "_adapter_or_none", return_value=FakeQbo()):
        result = await erp_status_sync.run_status_sync_once()

    assert result.failures == 0 and result.invoices_changed == 1
    since = FakeQbo.changed_bills.await_args.args[0]
    assert since.tzinfo is not None
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid


async def test_the_sweep_is_off_by_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    result = await erp_status_sync.run_status_sync_once()
    assert result.orgs_scanned == 0


# ---------------------------------------------------------------------------
# paid_on / payment_method on the generic ERP webhook (followups, §295)
# ---------------------------------------------------------------------------


def test_reported_paid_on_takes_a_real_past_date_only():
    yesterday = utc_today() - timedelta(days=1)
    assert erp_status_sync.reported_paid_on(yesterday.isoformat()) == yesterday
    assert erp_status_sync.reported_paid_on("2026-01-02T10:00:00Z") == date(2026, 1, 2)
    # A day of grace: the ERP dates in the company's zone, which can be ahead.
    tomorrow = utc_today() + timedelta(days=1)
    assert erp_status_sync.reported_paid_on(tomorrow.isoformat()) == tomorrow
    assert erp_status_sync.reported_paid_on((utc_today() + timedelta(days=2)).isoformat()) is None
    for junk in (None, "", "31/12/2026", 20261231, "not-a-date"):
        assert erp_status_sync.reported_paid_on(junk) is None


def test_reported_payment_method_takes_a_recordable_rail_only():
    assert erp_status_sync.reported_payment_method("check") == "check"
    assert erp_status_sync.reported_payment_method(" CARD ") == "card"
    # FeohLedger's own card programme is never a way to pay outside it.
    assert erp_status_sync.reported_payment_method("virtual_card") is None
    for junk in (None, "", "bitcoin", 7):
        assert erp_status_sync.reported_payment_method(junk) is None


async def test_the_erp_paid_path_books_the_reported_date_and_rail(realdb):
    inv_id, _ = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    paid_on = date(2026, 1, 15) if utc_today() >= date(2026, 1, 15) else utc_today()
    async with realdb.sessionmaker("a")() as s:
        from app.services.workflow_engine import get_invoice_for_update

        invoice = await get_invoice_for_update(s, inv_id)
        await erp_status_sync.record_erp_reported_payment(
            s,
            invoice,
            org=await _org(realdb),
            erp_type="netsuite",
            erp_document_id="NS-9",
            event_id="ev-9",
            paid_on=paid_on,
            method="wire",
        )
        await s.commit()
    async with realdb.sessionmaker("a")() as s:
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    assert payment.completed_at.date() == paid_on
    assert payment.method == "wire"


# ---------------------------------------------------------------------------
# review round: credits, the high-water mark, voiding an ERP-reported payment
# ---------------------------------------------------------------------------


async def test_a_bill_settled_by_a_vendor_credit_blocks_and_books_no_cash(realdb):
    """A $0 BillPayment applying a vendor credit settles the bill without money
    leaving: never recorded as a (1099-reportable) cash payment, never payable."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = _report(corr, ErpBillState.paid, reduced_outside=True, settled_by_credit=True)
    assert (await _apply(realdb, report)).action == "credit_settlement_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    async with realdb.sessionmaker("a")() as s:
        assert (
            await s.execute(select(Payment).where(Payment.invoice_id == inv_id))
        ).first() is None
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


@pytest.mark.parametrize(
    ("pay_type", "link_type", "method"),
    [("Check", "BillPaymentCheck", "check"), ("CreditCard", "BillPaymentCreditCard", "card")],
)
async def test_a_bill_paid_in_cash_in_real_quickbooks_shape_is_recorded_paid(
    realdb, pay_type, link_type, method
):
    """Regression (production, 2026-10-22): the adapter's report, read from a
    bill shaped as real QuickBooks returns it after Pay Bills, carried into
    `apply_bill_report`. The bill links the payment as `BillPaymentCheck` /
    `BillPaymentCreditCard`; read as anything else it found no cash and opened
    a vendor-credit block instead of recording the payment (decisions §317)."""
    import httpx

    from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
    from app.utils.json_money import dumps_exact_json

    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    paid_on = utc_today() - timedelta(days=1)
    bill = {
        "Id": BILL,
        "TotalAmt": Decimal("250.00"),
        "Balance": Decimal("0"),
        "PrivateNote": f"FeohLedger {corr}",
        "LinkedTxn": [{"TxnId": "901", "TxnType": link_type}],
    }
    payment = {
        "Id": "901",
        "TotalAmt": Decimal("250.00"),
        "PayType": pay_type,
        "TxnDate": paid_on.isoformat(),
        "Line": [{"Amount": Decimal("250.00"), "LinkedTxn": [{"TxnId": BILL, "TxnType": "Bill"}]}],
    }

    async def answer(method_, path, **kw):
        body = {"bill/" + BILL: {"Bill": bill}, "billpayment/901": {"BillPayment": payment}}
        return httpx.Response(200, content=dumps_exact_json(body[path]).encode())

    adapter = QuickBooksOnlineAdapter(
        {
            "type": "quickbooks_online",
            "integration_method": "direct",
            "oauth": {"provider": "quickbooks_online", "external_tenant_id": "9130"},
        }
    )
    with patch.object(QuickBooksOnlineAdapter, "_request", new=AsyncMock(side_effect=answer)):
        report = await adapter.bill_report(BILL)
    assert report.reduced_outside and not report.settled_by_credit

    assert (await _apply(realdb, report)).action == "paid_recorded"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid
    assert await _exceptions(realdb, inv_id) == []
    async with realdb.sessionmaker("a")() as s:
        [booked] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    assert booked.provider == EXTERNAL_PAYMENT_PROVIDER
    assert booked.completed_at.date() == paid_on
    assert booked.method == method


async def test_a_bill_reduced_outside_with_no_payment_listed_still_blocks(realdb):
    """Judged from the balance: a reduction no readable payment explains (a
    journal entry, a credit) is still a reduction outside FeohLedger."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = _report(corr, ErpBillState.partially_paid, reduced_outside=True)
    assert (await _apply(realdb, report)).action == "partial_payment_flagged"


async def _sweep_with(realdb, monkeypatch, fake, *, now=None):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    with patch.object(erp_status_sync, "_adapter_or_none", return_value=fake):
        return await erp_status_sync.run_status_sync_once(now=now)


async def test_the_sweep_keeps_a_mark_and_reads_from_it_after_a_gap(realdb, monkeypatch):
    from datetime import UTC, datetime

    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    await _seed(realdb, status=InvoiceStatus.posted_in_erp, realm=f"quickbooks_online:{company}")

    class FakeQbo:
        erp_type = "quickbooks_online"
        supports_status_sync = True
        changed_bills = AsyncMock(return_value=[])
        bill_report = AsyncMock(return_value=ErpBillReport(bill_id=BILL, state=ErpBillState.open))

    first = datetime(2026, 10, 1, tzinfo=UTC)
    result = await _sweep_with(realdb, monkeypatch, FakeQbo(), now=first)
    # No mark yet: CDC alone cannot vouch for the past, so every payable bill
    # posted to this company is read back.
    assert result.full_reconciliations == 1
    FakeQbo.bill_report.assert_awaited_once_with(BILL)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
    assert org.settings["erp"]["oauth"]["status_sync_through"] == first.isoformat()

    # Five days with no pass: the next reads from the mark, not the 72 h window.
    FakeQbo.bill_report.reset_mock()
    later = first + timedelta(days=5)
    result = await _sweep_with(realdb, monkeypatch, FakeQbo(), now=later)
    assert result.full_reconciliations == 0
    assert FakeQbo.changed_bills.await_args.args[0] == first
    FakeQbo.bill_report.assert_not_awaited()


async def test_a_cdc_answer_too_full_to_trust_falls_back_to_reading_every_bill(realdb, monkeypatch):
    from app.services.erp_adapters.base import ErpListingIncomplete, ListingGap

    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    inv_id, corr = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, realm=f"quickbooks_online:{company}"
    )

    class FakeQbo:
        erp_type = "quickbooks_online"
        supports_status_sync = True
        changed_bills = AsyncMock(
            side_effect=ErpListingIncomplete("QuickBooks Online", "cdc Bill", ListingGap.truncated)
        )
        bill_report = AsyncMock(
            return_value=_report(corr, ErpBillState.paid, external_payment_ids=("901",))
        )

    result = await _sweep_with(realdb, monkeypatch, FakeQbo())
    assert result.failures == 0 and result.full_reconciliations == 1
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid


async def test_voiding_an_erp_reported_payment_holds_the_invoice_out_of_runs(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    await _apply(realdb, _report(corr, ErpBillState.paid, external_payment_ids=("901",)))
    async with realdb.sessionmaker("a")() as s:
        [payment] = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/payments/{payment.id}/void", json={"reason": "entered twice"})
    assert resp.status_code == 200, resp.text
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.approved
    # QuickBooks may still show the bill paid: a person confirms before re-paying.
    flags = [e for e in await _exceptions(realdb, inv_id) if e.status == "open"]
    assert [f.exception_type for f in flags] == ["payment_reconciliation"]
    assert flags[0].description_code == "exception.erp_reported_payment_voided"
    assert "bill=" not in flags[0].description
    assert flags[0].erp_report == {
        "kind": "erp_payment_voided",
        "erp_document_id": BILL,
        "payment_id": str(payment.id),
    }


async def test_a_reconciliation_row_takes_a_finding_or_a_description_never_both():
    """The catalogued `finding` is the norm; plain English is kept only for the
    write-back's uncatalogued failures. Both, or neither, is a programming error
    caught before anything is written."""
    invoice = Invoice(id=uuid.uuid4())
    for kwargs in ({}, {"finding": {"message": "x"}, "description": "x"}):
        with pytest.raises(TypeError):
            await erp_status_sync.open_erp_reconciliation_exception(
                AsyncMock(), invoice, org_id=uuid.uuid4(), **kwargs
            )


@pytest.mark.parametrize(
    ("erp_type", "code", "params"),
    [
        (
            "quickbooks_online",
            "exception.erp_paid_unrecorded_card_live",
            {"erp": "QuickBooks Online"},
        ),
        ("merge_dev", "exception.erp_paid_unrecorded_card_live_unnamed", {}),
        (None, "exception.erp_paid_unrecorded_card_live_unnamed", {}),
    ],
)
def test_the_unrecorded_sentence_names_the_erp_only_when_it_has_a_product_name(
    erp_type, code, params
):
    out = erp_status_sync.unrecorded_payment_finding(erp_type, "external_payment_card_live")
    assert out["code"] == code and out["params"] == params
    assert "external_payment_" not in out["message"]


def test_an_unlisted_refusal_reads_the_generic_clause():
    out = erp_status_sync.unrecorded_payment_finding("xero", "external_payment_from_the_future")
    assert out["code"] == "exception.erp_paid_unrecorded_other"
    out = erp_status_sync.unrecorded_payment_finding("xero", "not_even_prefixed")
    assert out["code"] == "exception.erp_paid_unrecorded_other"


# ---------------------------------------------------------------------------
# a payment the ERP withdrew is never recorded later (decisions §319)
# ---------------------------------------------------------------------------


async def _paid_but_blocked(realdb):
    """A bill paid in QuickBooks while a stale blocker is open: the record is
    refused, and the ERP's payment is kept on a `paid_not_recorded` row."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    blocker = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            APException(
                id=blocker,
                organization_id=realdb.info("a").org_id,
                invoice_id=inv_id,
                exception_type="duplicate",
                severity="error",
                description="stale",
                status="open",
            )
        )
        await s.commit()
    await _apply(
        realdb,
        _report(corr, ErpBillState.paid, external_payment_ids=("901",), payment_method="check"),
    )
    [row] = [e for e in await _exceptions(realdb, inv_id) if e.erp_report]
    assert row.erp_report["kind"] == "paid_not_recorded"
    return inv_id, corr, blocker, row.id


@pytest.mark.parametrize(
    "state", [ErpBillState.open, ErpBillState.deleted, ErpBillState.partially_paid]
)
async def test_a_payment_the_erp_withdrew_is_not_recorded_when_the_blocker_clears(realdb, state):
    inv_id, corr, blocker, row_id = await _paid_but_blocked(realdb)

    # The cheque is voided / the bill deleted / part of it un-paid in QuickBooks.
    await _apply(realdb, _report(corr, state, reduced_outside=state is not ErpBillState.open))
    # Every blocker is cleared — the stale one, and the one the deletion or
    # part-payment opened ("still owed"): clearing must never book the payment
    # the ERP withdrew.
    blockers = [
        e.id for e in await _exceptions(realdb, inv_id) if e.status == "open" and e.id != row_id
    ]
    assert blocker in blockers
    async with realdb.client(key="a", role="admin") as c:
        for exc_id in blockers:
            resp = await c.post(
                f"/api/exceptions/{exc_id}/resolve",
                json={"action": "dismiss", "resolution": "stale"},
            )
            assert resp.status_code == 200, resp.text

    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    async with realdb.sessionmaker("a")() as s:
        payments = (
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )
        row = await s.get(APException, row_id)
    assert payments == []
    # Left open for a person, but no longer something to record.
    assert row.status == "open"
    assert row.erp_report["kind"] == "paid_withdrawn"
    assert row.erp_report["withdrawn_as"] == state.value
    assert "no longer shows it paid" in row.description
    assert row.description_code == "exception.erp_paid_withdrawn"
    assert row.description_params == {"erp": "QuickBooks Online"}


# ---------------------------------------------------------------------------
# Xero (decisions §386): matched by its bill id, recorded at Xero's own amount
# ---------------------------------------------------------------------------

XERO_TENANT = "xero-tenant-0001"
XERO_REALM = f"xero:{XERO_TENANT}"
XERO_BILL = "8a5b2d4e-1111-4c3d-9e8f-000000000501"


def _xero_bill(
    total: str, *, paid: str | None = None, credited: str = "0", day=None, currency="USD"
) -> dict:
    """An ACCPAY bill in Xero's own shape: payments listed on the bill, dates
    as ``/Date(<ms>+0000)/``, credits in ``AmountCredited``."""
    from datetime import UTC, datetime

    day = day or (utc_today() - timedelta(days=2))
    ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    payments = (
        [{"PaymentID": "xero-pay-1", "Date": f"/Date({ms}+0000)/", "Amount": Decimal(paid)}]
        if paid
        else []
    )
    total_d, paid_d, credited_d = Decimal(total), Decimal(paid or "0"), Decimal(credited)
    due = total_d - paid_d - credited_d
    return {
        "InvoiceID": XERO_BILL,
        "Type": "ACCPAY",
        "Status": "PAID" if due == 0 else "AUTHORISED",
        "Total": total_d,
        "AmountDue": due,
        "AmountPaid": paid_d,
        "AmountCredited": credited_d,
        "CurrencyCode": currency,
        "Payments": payments,
    }


async def _apply_xero(realdb, report: ErpBillReport):
    async with realdb.sessionmaker("a")() as s:
        return await erp_status_sync.apply_bill_report(
            s, org=await _org(realdb), report=report, realm=XERO_REALM, erp_type="xero"
        )


async def _payments(realdb, inv_id) -> list[Payment]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (await s.execute(select(Payment).where(Payment.invoice_id == inv_id))).scalars().all()
        )


async def test_a_bill_paid_in_xero_closes_the_invoice_at_xeros_own_amount(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    paid_on = utc_today() - timedelta(days=2)
    report = report_from_bill(_xero_bill("250.00", paid="250.00", day=paid_on), XERO_BILL)
    # No marker of ours on a Xero bill: matched by its never-reused id.
    assert report.correlation_id is None and report.match_by_reference
    assert report.paid_amount == Decimal("250.00")

    outcome = await _apply_xero(realdb, report)
    assert outcome.action == "paid_recorded"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid
    [payment] = await _payments(realdb, inv_id)
    assert payment.provider == EXTERNAL_PAYMENT_PROVIDER and payment.status == "completed"
    assert payment.amount == Decimal("250.00")
    assert payment.reference == XERO_BILL
    assert payment.completed_at.date() == paid_on
    # Read again (the next tick, or an admin's "check now"): nothing more.
    assert (await _apply_xero(realdb, report)).action == "unchanged"
    assert len(await _payments(realdb, inv_id)) == 1


async def test_a_xero_payment_that_is_not_what_is_owed_here_is_kept_not_booked(realdb):
    """Xero shows 240.00 paid in full; the invoice here owes 250.00. Booking
    250.00 would record money that never left: refused, kept for a person,
    and BLOCKING, since the bill is paid there and a run would pay it again."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("240.00", paid="240.00"), XERO_BILL)
    await _apply_xero(realdb, report)

    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"  # payment-blocking
    assert row.description_code == "exception.erp_paid_unrecorded_amount_mismatch"
    assert row.description_params == {"erp": "Xero"}
    assert row.erp_report["kind"] == "paid_not_recorded"
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch"
    assert row.erp_report["amount"] == "240.00"
    assert row.erp_report["amount_currency"] == "USD"

    # The one-click record books Xero's figure or nothing: never the 250.00
    # FeohLedger reckons is owed, which would close the row on a payment that
    # did not happen.
    body = {"method": "ach", "reference": XERO_BILL, "paid_on": utc_today().isoformat()}
    async with realdb.client(key="a", role="admin") as c:
        refused = await c.post(f"/api/exceptions/{row.id}/record-erp-payment", json=body)
        disagreeing = await c.post(
            f"/api/exceptions/{row.id}/record-erp-payment", json={**body, "amount": "250.00"}
        )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "external_payment_amount_mismatch"
    assert disagreeing.status_code == 422, disagreeing.text
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.status == "open"


async def test_a_xero_payment_in_another_currency_is_never_compared_as_a_number(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("250.00", paid="250.00", currency="ZAR"), XERO_BILL)
    assert report.paid_currency == "ZAR"
    await _apply_xero(realdb, report)
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch"
    assert row.erp_report["amount_currency"] == "ZAR"


async def test_a_credit_recording_the_discount_after_our_payment_is_not_paid_twice(realdb):
    """Our write-back paid 248.00 with a 2.00 discount; the bookkeeper records
    the discount in Xero as a credit note, as the write-back's flag asked.
    Nothing left twice, so nothing says it did."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(realdb, status=InvoiceStatus.paid, bill_id=XERO_BILL, realm=XERO_REALM)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Payment(
                invoice_id=inv_id,
                amount=Decimal("248.00"),
                method="ach",
                status="completed",
                provider="mock",
            )
        )
        await s.commit()
    bill = _xero_bill("250.00", paid="248.00", credited="2.00")
    bill["Payments"][0]["Reference"] = f"FeohLedger payment {uuid.uuid4()}"
    report = report_from_bill(bill, XERO_BILL)
    assert report.settled_by_credit and report.external_payment_ids == ()
    assert (await _apply_xero(realdb, report)).action == "unchanged"
    assert await _exceptions(realdb, inv_id) == []


async def test_a_changed_bill_no_invoice_was_pushed_as_is_never_read(realdb, monkeypatch):
    """Xero's change listing names every bill the organisation changed; reading
    the ones keyed there by hand spends its daily allowance for nothing."""
    await _connect_xero(realdb)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
    realm = f"xero:{org.settings['erp']['oauth']['external_tenant_id']}"
    await _seed(realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=realm)

    class FakeXero:
        erp_type = "xero"
        supports_status_sync = True
        changed_bills = AsyncMock(return_value=["keyed-by-hand", XERO_BILL])
        bill_report = AsyncMock(
            return_value=ErpBillReport(bill_id=XERO_BILL, state=ErpBillState.open)
        )

    await _sweep_with(realdb, monkeypatch, FakeXero())
    read = [c.args[0] for c in FakeXero.bill_report.await_args_list]
    assert "keyed-by-hand" not in read and XERO_BILL in read


async def test_a_bill_paid_in_xero_while_feohledger_holds_it_in_a_run_blocks(realdb):
    """§309's live claim: FeohLedger's own draft run still holds the invoice, so
    recording Xero's payment would leave that run to pay it a second time."""
    from app.models.payment import PaymentRun
    from app.services.erp_adapters.xero import report_from_bill

    info = realdb.info("a")
    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    run_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=info.org_id,
                status="draft",
                total_amount=Decimal("250.00"),
                initiated_by=info.users["ap_manager"],
            )
        )
        await s.flush()
        s.add(
            Payment(
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("250.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()

    report = report_from_bill(_xero_bill("250.00", paid="250.00"), XERO_BILL)
    await _apply_xero(realdb, report)

    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    [ours] = await _payments(realdb, inv_id)
    assert ours.payment_run_id == run_id and ours.status == "pending"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"  # payment-blocking
    assert flag.erp_report["refusal_code"] == "external_payment_in_run"
    # The facts are kept, Xero's amount included, for the one-click record.
    assert flag.erp_report["amount"] == "250.00"


async def test_a_xero_bill_settled_by_a_credit_note_states_no_cash_amount(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("250.00", credited="250.00"), XERO_BILL)
    assert report.settled_by_credit and report.paid_amount is None
    assert (await _apply_xero(realdb, report)).action == "credit_settlement_flagged"
    assert await _payments(realdb, inv_id) == []


async def test_a_xero_bill_id_is_matched_only_within_the_recorded_organisation(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm="xero:other-org"
    )
    report = report_from_bill(_xero_bill("250.00", paid="250.00"), XERO_BILL)
    assert (await _apply_xero(realdb, report)).action == "unmatched"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_two_invoices_naming_one_xero_bill_match_neither(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    first, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    second, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("250.00", paid="250.00"), XERO_BILL)
    assert (await _apply_xero(realdb, report)).action == "unmatched"
    for inv_id in (first, second):
        assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


def test_a_later_report_without_an_amount_keeps_the_stated_one():
    merged = erp_status_sync._merged_report(
        {"amount": "240.00", "paid_on": "2026-10-01", "reported_at": "t0"},
        {"amount": None, "paid_on": "2026-10-01", "reported_at": "t1"},
        keep_refusal=False,
    )
    assert merged["amount"] == "240.00"


async def _connect_xero(realdb, **oauth_extra) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        org.settings = {
            **(org.settings or {}),
            "erp": {
                "type": "xero",
                "integration_method": "direct",
                "oauth": {
                    "provider": "xero",
                    "external_tenant_id": f"xt-{uuid.uuid4().hex[:8]}",
                    "org_id": str(org.id),
                    "connection_id": "c-x",
                    **oauth_extra,
                },
            },
        }
        await s.commit()


async def test_the_sweep_covers_xero_tenants(realdb, monkeypatch):
    await _connect_xero(realdb)

    class FakeXero:
        erp_type = "xero"
        supports_status_sync = True
        changed_bills = AsyncMock(return_value=[])
        bill_report = AsyncMock()

    result = await _sweep_with(realdb, monkeypatch, FakeXero())
    assert result.orgs_scanned >= 1 and result.failures == 0
    FakeXero.changed_bills.assert_awaited()
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
    # The mark is written on the Xero connection, not a QuickBooks one.
    assert org.settings["erp"]["oauth"].get("status_sync_through")


async def test_an_on_request_pass_is_refused_with_a_code(realdb, monkeypatch):
    from app.config import settings

    await _connect_xero(realdb)
    org = await _org(realdb)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    assert await erp_status_sync.status_sync_now_refusal(org) == "status_sync_off"
    monkeypatch.setattr(settings, "erp_status_sync_enabled", True)
    assert await erp_status_sync.status_sync_now_refusal(org) is None

    await _connect_xero(realdb, needs_reconnect=True)
    org = await _org(realdb)
    assert await erp_status_sync.status_sync_now_refusal(org) == "status_sync_needs_reconnect"

    async with realdb.control_sessionmaker()() as s:
        row = await s.get(Organization, realdb.info("a").org_id)
        erp = dict(row.settings["erp"])
        erp["oauth"] = {**erp["oauth"], "provider": "sage_accounting", "needs_reconnect": False}
        row.settings = {**row.settings, "erp": erp}
        await s.commit()
    org = await _org(realdb)
    assert await erp_status_sync.status_sync_now_refusal(org) == "status_sync_unsupported"


async def test_an_on_request_pass_runs_once_per_tenant_at_a_time(monkeypatch):
    """One pass per tenant: on this worker, and across workers by a Redis
    claim the pass releases when it ends."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(org_id, actor_id):
        started.set()
        await release.wait()
        erp_status_sync._now_running.discard(org_id)
        await erp_status_sync._release_now_lock(org_id)

    monkeypatch.setattr(erp_status_sync, "_sync_org_now_task", slow)
    org_id = uuid.uuid4()
    assert await erp_status_sync.dispatch_sync_org_now(org_id, actor_id=None) is True
    await started.wait()
    assert await erp_status_sync.dispatch_sync_org_now(org_id, actor_id=None) is False
    # Another worker: nothing running in its process, but the claim is held.
    erp_status_sync._now_running.discard(org_id)
    assert await erp_status_sync.dispatch_sync_org_now(org_id, actor_id=None) is False
    erp_status_sync._now_running.add(org_id)
    release.set()
    await asyncio.gather(*erp_status_sync._now_tasks)
    started.clear()
    assert await erp_status_sync.dispatch_sync_org_now(org_id, actor_id=None) is True
    await asyncio.gather(*erp_status_sync._now_tasks)


async def test_an_on_request_pass_refused_at_start_is_still_audited(realdb, monkeypatch):
    from app.config import settings
    from app.models.workflow import AuditLog

    await _connect_xero(realdb)
    monkeypatch.setattr(settings, "erp_status_sync_enabled", False)
    org_id = realdb.info("a").org_id
    await erp_status_sync._sync_org_now_task(org_id, None)
    async with realdb.sessionmaker("a")() as s:
        [row] = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "organization.erp_status_synced")
                )
            )
            .scalars()
            .all()
        )
    assert row.details["outcome"] == "refused"
    assert row.details["refusal"] == "status_sync_off"
