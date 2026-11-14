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
        bills_match_by_reference = False
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


async def test_a_credit_settled_bill_at_sent_to_erp_books_no_cash_and_blocks(realdb):
    """`sent_to_erp` is recordable but not payable here, so it must take the
    credit branch, not fall through to the cash one (decisions §303, §399):
    a QuickBooks bill settled by a vendor credit books no `Payment` and opens
    the payment-blocking `payment_reconciliation` row, invoice unmoved."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    inv_id, corr = await _seed(realdb, status=InvoiceStatus.sent_to_erp)
    report = _report(corr, ErpBillState.paid, reduced_outside=True, settled_by_credit=True)
    async with realdb.sessionmaker("a")() as s:
        outcome = await erp_status_sync.apply_bill_report(
            s,
            org=await _org(realdb),
            report=report,
            realm=REALM,
            erp_type=erp_status_sync.QBO_PROVIDER,
            event_id="ev-credit-sent",
        )
    assert outcome.action == "credit_settlement_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    assert flag.exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES
    assert flag.status == "open"
    assert await _payments(realdb, inv_id) == []
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.sent_to_erp


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
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    paid_on = utc_today() - timedelta(days=1)
    report = await _qbo_report(
        corr,
        "250.00",
        [_qbo_cash("901", "250.00", pay_type=pay_type, day=paid_on)],
        link_type=link_type,
    )
    assert report.reduced_outside and not report.settled_by_credit
    # The cash QuickBooks shows, in the bill's currency (decisions §393).
    assert report.paid_amount == Decimal("250.00") and report.paid_currency == "USD"

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
    assert booked.amount == Decimal("250.00")


# -- QuickBooks' cash, recorded or refused through the shared path (§393) ------

_QBO_CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": "9130"},
}


def _qbo_cash(pid: str, amount: str, *, pay_type="Check", day=None, bill_id=BILL) -> dict:
    """A BillPayment a person made with Pay Bills, in QuickBooks' own shape."""
    day = day or (utc_today() - timedelta(days=1))
    return {
        "Id": pid,
        "TotalAmt": Decimal(amount),
        "PayType": pay_type,
        "TxnDate": day.isoformat(),
        "PrivateNote": "",
        "Line": [{"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]}],
    }


def _qbo_vendor_credit(pid: str, amount: str) -> dict:
    """How QuickBooks records a vendor credit applied to a bill: a $0
    BillPayment with a line on the bill and one on the VendorCredit."""
    return {
        "Id": pid,
        "TotalAmt": Decimal("0"),
        "PayType": "Check",
        "TxnDate": (utc_today() - timedelta(days=1)).isoformat(),
        "PrivateNote": "",
        "Line": [
            {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": BILL, "TxnType": "Bill"}]},
            {
                "Amount": Decimal(amount),
                "LinkedTxn": [{"TxnId": "VC-1", "TxnType": "VendorCredit"}],
            },
        ],
    }


async def _qbo_report(
    corr: str,
    total: str,
    payments: list[dict],
    *,
    currency: str | None = "USD",
    link_type: str | None = None,
) -> ErpBillReport:
    """The real adapter's report on one bill, paid by ``payments``: the bill
    comes down by what their lines applied to it, and links each as QuickBooks
    does, by its pay type (decisions §317), or as ``link_type`` when given."""
    import httpx

    from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
    from app.utils.json_money import dumps_exact_json

    applied = sum(
        (
            line["Amount"]
            for p in payments
            for line in p["Line"]
            if line["LinkedTxn"][0]["TxnType"] == "Bill"
        ),
        Decimal(0),
    )
    bill = {
        "Id": BILL,
        "TotalAmt": Decimal(total),
        "Balance": Decimal(total) - applied,
        "PrivateNote": f"FeohLedger {corr}",
        "LinkedTxn": [
            {
                "TxnId": p["Id"],
                "TxnType": link_type
                or (
                    "BillPaymentCreditCard" if p["PayType"] == "CreditCard" else "BillPaymentCheck"
                ),
            }
            for p in payments
        ],
    }
    if currency is not None:
        bill["CurrencyRef"] = {"value": currency, "name": currency}
    answers = {f"bill/{BILL}": {"Bill": bill}}
    answers.update({f"billpayment/{p['Id']}": {"BillPayment": p} for p in payments})

    async def answer(method_, path, **kw):
        return httpx.Response(200, content=dumps_exact_json(answers[path]).encode())

    with patch.object(QuickBooksOnlineAdapter, "_request", new=AsyncMock(side_effect=answer)):
        return await QuickBooksOnlineAdapter(_QBO_CONFIG).bill_report(BILL)


async def _apply_credit_memo_here(realdb, inv_id, amount: str) -> None:
    """A credit memo applied to the invoice in FeohLedger only: it owes less
    here than the bill QuickBooks holds."""
    from app.models.credit_memo import CreditMemo
    from app.models.vendor import Vendor

    org_id = realdb.info("a").org_id
    async with realdb.sessionmaker("a")() as s:
        vendor = Vendor(name="Credited Vendor", organization_id=org_id)
        s.add(vendor)
        await s.flush()
        # A credit memo nets only against its own vendor's invoice.
        (await s.get(Invoice, inv_id)).vendor_id = vendor.id
        s.add(
            CreditMemo(
                memo_number=f"CM-{uuid.uuid4().hex[:6]}",
                vendor_id=vendor.id,
                invoice_id=inv_id,
                amount=Decimal(amount),
                currency="USD",
                status="applied",
                organization_id=org_id,
            )
        )
        await s.commit()


async def test_a_bill_paid_by_cheque_and_card_is_recorded_at_their_sum(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = await _qbo_report(
        corr,
        "250.00",
        [_qbo_cash("901", "150.00"), _qbo_cash("902", "100.00", pay_type="CreditCard")],
    )
    assert report.paid_amount == Decimal("250.00")
    assert (await _apply(realdb, report)).action == "paid_recorded"
    [booked] = await _payments(realdb, inv_id)
    assert booked.amount == Decimal("250.00")
    # No one rail paid it all, so `method` states none; the split is kept on
    # the payment, and the 1099 reports the cheque part only (§396).
    assert booked.method is None
    assert booked.rail_breakdown == [
        {"method": "check", "amount": "150.00"},
        {"method": "card", "amount": "100.00"},
    ]
    assert await _exceptions(realdb, inv_id) == []
    # The record's audit row says which rails paid how much, and that it stands.
    details = await _recorded_outside_details(realdb, booked.id)
    assert details["rail_breakdown"] == booked.rail_breakdown
    assert details["rail_breakdown_status"] == "applied"
    assert details["method"] is None


async def _recorded_outside_details(realdb, payment_id) -> dict:
    from app.models.workflow import AuditLog

    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.entity_id == payment_id, AuditLog.action == "payment.recorded_outside"
                )
            )
        ).scalar_one()


async def test_a_bill_paid_by_two_cards_is_recorded_as_card_with_its_breakdown(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = await _qbo_report(
        corr,
        "250.00",
        [
            _qbo_cash("901", "200.00", pay_type="CreditCard"),
            _qbo_cash("902", "50.00", pay_type="CreditCard"),
        ],
    )
    assert (await _apply(realdb, report)).action == "paid_recorded"
    [booked] = await _payments(realdb, inv_id)
    assert booked.method == "card"
    assert booked.rail_breakdown == [
        {"method": "card", "amount": "200.00"},
        {"method": "card", "amount": "50.00"},
    ]


async def test_a_breakdown_that_does_not_sum_to_the_payment_is_recorded_reportable(realdb):
    """Fail closed (§396): an adapter whose parts miss the cash figure by a
    cent gets no split and no rail, even the "card" it stated, so the whole
    payment is reportable; the audit row keeps the parts and says why."""
    from app.services.payment_methods import RailPart

    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = _report(
        corr,
        ErpBillState.paid,
        external_payment_ids=("901", "902"),
        payment_method="card",
        paid_amount=Decimal("250.00"),
        paid_currency="USD",
        payment_parts=(RailPart("card", Decimal("150.00")), RailPart("card", Decimal("99.99"))),
    )
    assert (await _apply(realdb, report)).action == "paid_recorded"
    [booked] = await _payments(realdb, inv_id)
    assert booked.amount == Decimal("250.00")
    assert booked.method is None and booked.rail_breakdown is None
    details = await _recorded_outside_details(realdb, booked.id)
    assert details["rail_breakdown_status"] == "unreconciled"
    assert details["rail_breakdown"] == [
        {"method": "card", "amount": "150.00"},
        {"method": "card", "amount": "99.99"},
    ]
    assert details["method_stated"] == "card"


async def test_a_kept_two_rail_payment_is_booked_with_its_split_when_its_blocker_clears(realdb):
    """The re-attempt books the kept facts, the split included, never the
    payment as one unstated rail."""
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
    report = await _qbo_report(
        corr,
        "250.00",
        [_qbo_cash("901", "150.00"), _qbo_cash("902", "100.00", pay_type="CreditCard")],
    )
    await _apply(realdb, report)
    [row] = [e for e in await _exceptions(realdb, inv_id) if e.erp_report]
    assert row.erp_report["payment_parts"] == [
        {"method": "check", "amount": "150.00"},
        {"method": "card", "amount": "100.00"},
    ]
    assert await _payments(realdb, inv_id) == []

    await _dismiss_flag(realdb, blocker)
    [booked] = await _payments(realdb, inv_id)
    assert booked.status == "completed" and booked.method is None
    assert booked.rail_breakdown == row.erp_report["payment_parts"]


async def test_the_one_click_books_the_kept_split_over_a_persons_one_rail(realdb):
    """The dialog asks for one rail; the ERP said two paid it. As with the
    figure, the ERP's breakdown is the record; the person's rail is audited."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            APException(
                organization_id=realdb.info("a").org_id,
                invoice_id=inv_id,
                exception_type="erp_reconciliation",
                severity="warning",
                description="kept",
                status="open",
                erp_report={
                    "kind": "paid_not_recorded",
                    "erp_type": "quickbooks_online",
                    "erp_document_id": BILL,
                    "method": None,
                    "reference": BILL,
                    "paid_on": utc_today().isoformat(),
                    "paid_on_reported": True,
                    "amount": "250.00",
                    "amount_currency": "USD",
                    "payment_parts": [
                        {"method": "check", "amount": "150.00"},
                        {"method": "card", "amount": "100.00"},
                    ],
                    "refusal_code": "external_payment_blocking_exception",
                },
            )
        )
        await s.commit()
    [row] = await _exceptions(realdb, inv_id)
    body = {"method": "check", "reference": BILL, "paid_on": utc_today().isoformat()}
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/exceptions/{row.id}/record-erp-payment", json=body)
    assert resp.status_code == 201, resp.text
    [booked] = await _payments(realdb, inv_id)
    assert booked.method is None
    assert booked.rail_breakdown == [
        {"method": "check", "amount": "150.00"},
        {"method": "card", "amount": "100.00"},
    ]
    details = await _recorded_outside_details(realdb, booked.id)
    assert details["rail_breakdown_status"] == "applied"
    assert details["method_stated"] == "check"


async def test_a_bill_part_paid_in_quickbooks_records_nothing_and_blocks(realdb):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = await _qbo_report(corr, "250.00", [_qbo_cash("901", "100.00")])
    assert report.state is ErpBillState.partially_paid and report.paid_amount is None
    assert (await _apply(realdb, report)).action == "partial_payment_flagged"
    assert await _payments(realdb, inv_id) == []
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"


async def test_a_bill_paid_part_by_a_vendor_credit_books_no_cash(realdb):
    """§303: the cheque beside the credit is not the payment of the bill."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = await _qbo_report(
        corr, "250.00", [_qbo_cash("901", "200.00"), _qbo_vendor_credit("902", "50.00")]
    )
    assert report.settled_by_credit and report.paid_amount is None
    assert (await _apply(realdb, report)).action == "credit_settlement_flagged"
    assert await _payments(realdb, inv_id) == []
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_quickbooks_cash_that_is_not_what_is_owed_here_is_kept_not_booked(realdb):
    """The follow-up's case: a credit memo applied here only, so the invoice
    owes 240.00, while QuickBooks shows the whole 250.00 bill paid by cheque.
    Booking 240.00 would record a payment that is not what left: refused,
    kept with QuickBooks' figure, and BLOCKING, since the bill is paid there."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    await _apply_credit_memo_here(realdb, inv_id, "10.00")
    report = await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")])
    await _apply(realdb, report)

    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch", row.erp_report
    assert row.exception_type == "payment_reconciliation"  # payment-blocking
    assert row.description_code == "exception.erp_paid_unrecorded_amount_mismatch"
    assert row.description_params == {"erp": "QuickBooks Online"}
    assert row.erp_report["kind"] == "paid_not_recorded"
    assert row.erp_report["amount"] == "250.00"
    assert row.erp_report["amount_currency"] == "USD"

    # It blocks: a run refuses the invoice while the row is open.
    from app.services.payment_runs import blocking_exception_types

    async with realdb.sessionmaker("a")() as s:
        assert await blocking_exception_types(s, [inv_id]) == {inv_id: "payment_reconciliation"}

    # The one-click record books QuickBooks' figure or nothing.
    body = {"method": "check", "reference": BILL, "paid_on": utc_today().isoformat()}
    async with realdb.client(key="a", role="admin") as c:
        refused = await c.post(f"/api/exceptions/{row.id}/record-erp-payment", json=body)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "external_payment_amount_mismatch"
    assert await _payments(realdb, inv_id) == []


@pytest.mark.parametrize("currency", ["CAD", None])
async def test_quickbooks_cash_in_another_currency_or_none_is_refused(realdb, currency):
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")], currency=currency)
    assert report.paid_amount == Decimal("250.00") and report.paid_currency == currency
    await _apply(realdb, report)
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch"
    assert row.erp_report["amount_currency"] == currency


async def test_quickbooks_cash_a_payment_states_in_another_currency_is_refused(realdb):
    """The bill says USD, the person's BillPayment says EUR: the figure is in
    no currency we can vouch for, so it is refused, never booked as dollars."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    payment = _qbo_cash("901", "250.00")
    payment["CurrencyRef"] = {"value": "EUR", "name": "Euro"}
    report = await _qbo_report(corr, "250.00", [payment])
    await _apply(realdb, report)
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch"
    assert row.erp_report["amount"] == "250.00"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_outside_cash_beside_our_live_write_back_blocks_and_books_nothing(realdb):
    """We paid 245.00 with a 5.00 discount, wrote it back, then voided it here
    while the QuickBooks void did not land: the invoice is payable again and
    our 245.00 BillPayment still stands. A bookkeeper clears the 5.00 by
    cheque. Only 5.00 left outside us, so booking the 250.00 owed here as
    cash would record money that never moved: it blocks for a person."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.approved)
    ours = _qbo_cash("901", "245.00")
    ours["PrivateNote"] = f"FeohLedger payment {uuid.uuid4()}"
    report = await _qbo_report(corr, "250.00", [ours, _qbo_cash("902", "5.00")])
    assert report.reduced_outside and report.our_payment_applied
    assert report.paid_amount is None
    assert (await _apply(realdb, report)).action == "partial_payment_flagged"
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"
    assert row.description_code == "exception.erp_partly_paid_outside"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.approved


async def test_a_bill_reduced_outside_with_no_payment_listed_still_blocks(realdb):
    """Judged from the balance: a reduction no readable payment explains (a
    journal entry, a credit) is still a reduction outside FeohLedger."""
    inv_id, corr = await _seed(realdb, status=InvoiceStatus.posted_in_erp)
    report = _report(corr, ErpBillState.partially_paid, reduced_outside=True)
    assert (await _apply(realdb, report)).action == "partial_payment_flagged"


# ---------------------------------------------------------------------------
# a QuickBooks row kept before §393 states no figure: re-read, never ours booked
# ---------------------------------------------------------------------------


async def _legacy_qbo_row(realdb, monkeypatch, *, blocking: bool):
    """An invoice posted to a connected QuickBooks company, with the
    ``paid_not_recorded`` row a QuickBooks report left before §393: the
    payment's facts with ``amount: None``. Non-blocking rows were refused by
    another open flag, which is returned; blocking ones by a double-pay
    refusal, now answered (no other flag is open)."""
    monkeypatch.setattr(erp_status_sync, "_unreachable_until", {})
    company = f"realm-{uuid.uuid4().hex[:8]}"
    await _connect(realdb, "a", company)
    inv_id, corr = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, realm=f"quickbooks_online:{company}"
    )
    org_id = realdb.info("a").org_id
    flag_id, row_id = uuid.uuid4(), uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        if not blocking:
            s.add(
                APException(
                    id=flag_id,
                    organization_id=org_id,
                    invoice_id=inv_id,
                    exception_type="duplicate",
                    severity="error",
                    description="stale",
                    status="open",
                )
            )
        s.add(
            APException(
                id=row_id,
                organization_id=org_id,
                invoice_id=inv_id,
                exception_type="payment_reconciliation" if blocking else "erp_reconciliation",
                severity="error",
                description="QuickBooks Online reports this bill paid",
                status="open",
                erp_report={
                    "kind": "paid_not_recorded",
                    "erp_type": "quickbooks_online",
                    "erp_document_id": BILL,
                    "event_id": None,
                    "method": "check",
                    "reference": BILL,
                    "paid_on": (utc_today() - timedelta(days=1)).isoformat(),
                    "paid_on_reported": True,
                    "amount": None,
                    "amount_currency": None,
                    "refusal_code": (
                        "external_payment_in_run"
                        if blocking
                        else "external_payment_blocking_exception"
                    ),
                    "reported_at": "2026-11-01T00:00:00+00:00",
                },
            )
        )
        await s.commit()
    return inv_id, corr, flag_id, row_id


def _bill_reads(answer):
    """QuickBooks' ``bill_report`` answering ``answer`` (a report, or an
    exception to raise) — the read back the row's figure now comes from."""
    from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter

    mock = (
        AsyncMock(side_effect=answer)
        if isinstance(answer, BaseException)
        else AsyncMock(return_value=answer)
    )
    return patch.object(QuickBooksOnlineAdapter, "bill_report", new=mock), mock


async def _dismiss_flag(realdb, flag_id) -> None:
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            f"/api/exceptions/{flag_id}/resolve", json={"action": "dismiss", "resolution": "stale"}
        )
    assert resp.status_code == 200, resp.text


async def test_a_legacy_quickbooks_row_books_the_bills_own_figure_when_its_flag_clears(
    realdb, monkeypatch
):
    inv_id, corr, flag_id, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=False)
    report = await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")])
    patcher, read = _bill_reads(report)
    with patcher:
        await _dismiss_flag(realdb, flag_id)

    read.assert_awaited_once_with(BILL)  # read again, under the lock, before booking
    [booked] = await _payments(realdb, inv_id)
    assert booked.amount == Decimal("250.00") and booked.status == "completed"
    assert booked.method == "check" and booked.reference == BILL
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(APException, row_id)).status == "resolved"


async def test_a_legacy_quickbooks_row_is_never_booked_at_our_own_smaller_figure(
    realdb, monkeypatch
):
    """The review's case: a 10.00 credit memo applied here only, QuickBooks
    shows the 250.00 bill paid by cheque. Before, clearing the flag booked
    240.00 as cash. Now the bill's 250.00 is what is offered to the record,
    which refuses it: blocking, with QuickBooks' figure kept."""
    inv_id, corr, flag_id, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=False)
    await _apply_credit_memo_here(realdb, inv_id, "10.00")
    report = await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")])
    patcher, _ = _bill_reads(report)
    with patcher:
        await _dismiss_flag(realdb, flag_id)

    assert await _payments(realdb, inv_id) == []
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    [live] = [e for e in await _exceptions(realdb, inv_id) if e.status == "open"]
    assert live.exception_type == "payment_reconciliation"
    assert live.erp_report["refusal_code"] == "external_payment_amount_mismatch"
    assert live.erp_report["amount"] == "250.00"
    assert live.erp_report["amount_currency"] == "USD"


@pytest.mark.parametrize("read", ["fails", "states_no_figure"])
async def test_a_legacy_quickbooks_row_the_bill_cannot_confirm_is_held_for_a_person(
    realdb, monkeypatch, read
):
    inv_id, corr, flag_id, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=False)
    answer = (
        RuntimeError("simulated read failure")
        if read == "fails"
        # Now part-paid by a vendor credit beside a cheque: no cash figure.
        else await _qbo_report(
            corr, "250.00", [_qbo_cash("901", "200.00"), _qbo_vendor_credit("902", "50.00")]
        )
    )
    patcher, _ = _bill_reads(answer)
    with patcher:
        await _dismiss_flag(realdb, flag_id)

    assert await _payments(realdb, inv_id) == []
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp
    rows = {e.id: e for e in await _exceptions(realdb, inv_id)}
    # The non-blocking row is replaced by a blocking one with its facts.
    assert rows[row_id].status == "resolved"
    [live] = [e for e in rows.values() if e.status == "open"]
    assert live.exception_type == "payment_reconciliation"
    assert live.erp_report["kind"] == "paid_not_recorded"
    assert live.erp_report["refusal_code"] == "erp_paid_amount_unconfirmed"
    assert live.erp_report["amount"] is None and live.erp_report["method"] == "check"
    assert live.description_code == "exception.erp_paid_retry_failed"
    assert live.description_params == {"erp": "QuickBooks Online"}
    from app.services.payment_runs import blocking_exception_types

    async with realdb.sessionmaker("a")() as s:
        assert await blocking_exception_types(s, [inv_id]) == {inv_id: "payment_reconciliation"}


async def test_the_one_click_on_a_legacy_quickbooks_row_books_only_the_bills_figure(
    realdb, monkeypatch
):
    inv_id, corr, _, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=True)
    body = {"method": "check", "reference": BILL, "paid_on": utc_today().isoformat()}

    # A body figure the bill does not state is refused before anything is booked.
    patcher, read = _bill_reads(await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")]))
    with patcher:
        async with realdb.client(key="a", role="admin") as c:
            wrong = await c.post(
                f"/api/exceptions/{row_id}/record-erp-payment", json={**body, "amount": "240.00"}
            )
            res = await c.post(f"/api/exceptions/{row_id}/record-erp-payment", json=body)
    assert wrong.status_code == 422, wrong.text
    assert wrong.json()["detail"]["code"] == "external_payment_amount_mismatch"
    assert read.await_count == 2
    assert res.status_code == 201, res.text
    [booked] = await _payments(realdb, inv_id)
    assert booked.amount == Decimal("250.00")
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.paid


async def test_the_one_click_refuses_a_legacy_row_whose_bill_shows_another_figure(
    realdb, monkeypatch
):
    inv_id, corr, _, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=True)
    await _apply_credit_memo_here(realdb, inv_id, "10.00")
    body = {"method": "check", "reference": BILL, "paid_on": utc_today().isoformat()}
    patcher, _ = _bill_reads(await _qbo_report(corr, "250.00", [_qbo_cash("901", "250.00")]))
    with patcher:
        async with realdb.client(key="a", role="admin") as c:
            res = await c.post(f"/api/exceptions/{row_id}/record-erp-payment", json=body)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "external_payment_amount_mismatch"
    assert await _payments(realdb, inv_id) == []
    async with realdb.sessionmaker("a")() as s:
        assert (await s.get(APException, row_id)).status == "open"


@pytest.mark.parametrize("blocking", [True, False])
async def test_the_one_click_on_a_legacy_row_the_bill_cannot_confirm_is_refused(
    realdb, monkeypatch, blocking
):
    """Refused with a code, nothing booked; a non-blocking row is re-raised as
    the blocking row it should be, since the bill is paid in QuickBooks."""
    inv_id, _, _, row_id = await _legacy_qbo_row(realdb, monkeypatch, blocking=blocking)
    body = {"method": "check", "reference": BILL, "paid_on": utc_today().isoformat()}
    patcher, _ = _bill_reads(RuntimeError("simulated read failure"))
    with patcher:
        async with realdb.client(key="a", role="admin") as c:
            res = await c.post(f"/api/exceptions/{row_id}/record-erp-payment", json=body)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "erp_paid_amount_unconfirmed"
    assert await _payments(realdb, inv_id) == []
    rows = {e.id: e for e in await _exceptions(realdb, inv_id) if e.erp_report}
    [live] = [e for e in rows.values() if e.status == "open"]
    assert live.exception_type == "payment_reconciliation"
    if blocking:
        assert live.id == row_id  # left as it was: nothing written
        assert live.erp_report["refusal_code"] == "external_payment_in_run"
    else:
        assert rows[row_id].status == "resolved"
        assert live.erp_report["refusal_code"] == "erp_paid_amount_unconfirmed"


async def test_a_row_from_an_erp_that_never_reads_bills_back_needs_no_read():
    """The generic ERP webhook's adapters state no figure on any report, so
    their kept rows are recorded at what is owed here, as their first report
    already is: nothing is read, and nothing is held for a figure."""
    figure = await erp_status_sync.read_back_unstated_figure(
        None, invoice=None, org=None, facts={"erp_type": "netsuite"}
    )
    assert figure == erp_status_sync.ReadBackFigure(needed=False)


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
        bills_match_by_reference = False
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
        bills_match_by_reference = False
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


async def test_a_xero_payment_stating_no_currency_is_refused_as_a_mismatch(realdb):
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("250.00", paid="250.00", currency=""), XERO_BILL)
    assert report.paid_amount == Decimal("250.00") and report.paid_currency is None
    await _apply_xero(realdb, report)
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.erp_report["refusal_code"] == "external_payment_amount_mismatch"


async def test_a_live_claim_is_named_before_a_currency_mismatch(realdb):
    """§309's live-claim refusal wins: a Xero payment in another currency on
    an invoice FeohLedger's own run still holds is first a double payment,
    and the row says so (the run is what a person must cancel)."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    await _hold_in_a_draft_run(realdb, inv_id)
    report = report_from_bill(_xero_bill("250.00", paid="250.00", currency="ZAR"), XERO_BILL)
    await _apply_xero(realdb, report)
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.exception_type == "payment_reconciliation"
    assert flag.erp_report["refusal_code"] == "external_payment_in_run"
    assert flag.erp_report["amount_currency"] == "ZAR"


async def _hold_in_a_draft_run(realdb, inv_id) -> uuid.UUID:
    from app.models.payment import PaymentRun

    info = realdb.info("a")
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
    return run_id


async def test_outside_cash_beside_our_live_xero_write_back_blocks_and_books_nothing(realdb):
    """§393's branch, through Xero's own reading: our 245.00 write-back still
    stands on the bill after a void here whose Xero delete did not land, and a
    bookkeeper paid the remaining 5.00. Only 5.00 left outside us, so the
    250.00 owed here is never booked as cash: it blocks for a person."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.approved, bill_id=XERO_BILL, realm=XERO_REALM
    )
    bill = _xero_bill("250.00", paid="245.00")
    bill["Payments"][0]["Reference"] = f"FeohLedger payment {uuid.uuid4()}"
    bill["Payments"].append(
        {"PaymentID": "xero-pay-2", "Date": bill["Payments"][0]["Date"], "Amount": Decimal("5.00")}
    )
    bill.update(AmountPaid=Decimal("250.00"), AmountDue=Decimal("0"), Status="PAID")
    report = report_from_bill(bill, XERO_BILL)
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.our_payment_applied
    assert report.reduced_outside_amount == Decimal("5.00") and report.paid_amount is None

    assert (await _apply_xero(realdb, report)).action == "partial_payment_flagged"
    assert await _payments(realdb, inv_id) == []
    [row] = await _exceptions(realdb, inv_id)
    assert row.exception_type == "payment_reconciliation"
    assert row.description_code == "exception.erp_partly_paid_outside"
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.approved


async def test_the_one_click_refuses_a_stated_amount_with_no_currency(realdb):
    """The sweep refuses an amount stated in no currency; the one-click must
    refuse the same facts, never compare the figure as a bare number."""
    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    async with realdb.sessionmaker("a")() as s:
        invoice = await s.get(Invoice, inv_id)
        # A blank currency on both sides: the case a bare comparison
        # ("" == "") used to let through.
        invoice.currency = ""
        await s.flush()
        await erp_status_sync.record_erp_reported_payment(
            s,
            invoice,
            org=await _org(realdb),
            erp_type="xero",
            erp_document_id=XERO_BILL,
            event_id=None,
            amount=Decimal("250.00"),
            amount_currency=None,
        )
        await s.commit()
    [row] = await _exceptions(realdb, inv_id)
    assert row.erp_report["amount"] == "250.00" and row.erp_report["amount_currency"] is None
    body = {"method": "ach", "reference": XERO_BILL, "paid_on": utc_today().isoformat()}
    async with realdb.client(key="a", role="admin") as c:
        refused = await c.post(f"/api/exceptions/{row.id}/record-erp-payment", json=body)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "external_payment_amount_mismatch"
    assert await _payments(realdb, inv_id) == []
    assert (await _invoice(realdb, inv_id)).status == InvoiceStatus.posted_in_erp


async def test_a_credit_recording_the_discount_after_our_payment_is_not_paid_twice(realdb):
    """Our write-back paid 248.00 with a 2.00 discount; the bookkeeper records
    the discount in Xero as a credit note, as the write-back's flag asked.
    Nothing left twice, so nothing says it did."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id = await _paid_by_our_rail(realdb, amount="248.00", discount="2.00")
    report = report_from_bill(_xero_bill_with_our_write_back("248.00", credited="2.00"), XERO_BILL)
    assert report.settled_by_credit and report.external_payment_ids == ()
    assert report.our_payment_applied and report.reduced_outside_amount == Decimal("2.00")
    assert (await _apply_xero(realdb, report)).action == "unchanged"
    assert await _exceptions(realdb, inv_id) == []


async def _paid_by_our_rail(realdb, *, amount: str, discount: str | None = None):
    """An invoice FeohLedger's own rail paid (not a payment recorded outside)."""
    inv_id, _ = await _seed(realdb, status=InvoiceStatus.paid, bill_id=XERO_BILL, realm=XERO_REALM)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            Payment(
                invoice_id=inv_id,
                amount=Decimal(amount),
                discount_amount=Decimal(discount) if discount else None,
                method="ach",
                status="completed",
                provider="mock",
            )
        )
        await s.commit()
    return inv_id


def _xero_bill_with_our_write_back(paid: str, *, credited: str) -> dict:
    bill = _xero_bill("250.00", paid=paid, credited=credited)
    bill["Payments"][0]["Reference"] = f"FeohLedger payment {uuid.uuid4()}"
    return bill


async def test_a_credit_settling_a_bill_our_write_back_never_reached_is_paid_twice(realdb):
    """Our ACH paid 250.00 but the write-back was refused (no mapped account),
    so Xero still showed 250.00 due. A bookkeeper then settles it with a
    250.00 vendor credit: the bill has been settled twice, and a credit with
    no payment of anyone else's must not hide that (money-path review, §386)."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id = await _paid_by_our_rail(realdb, amount="250.00")
    report = report_from_bill(_xero_bill("250.00", credited="250.00"), XERO_BILL)
    assert report.settled_by_credit and report.external_payment_ids == ()
    assert report.our_payment_applied is False

    assert (await _apply_xero(realdb, report)).action == "double_payment_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.description_code == "exception.erp_paid_twice"


async def test_a_credit_beyond_the_discount_our_write_back_left_open_is_paid_twice(realdb):
    """Our write-back is on the bill, but the credit took off more than the
    discount FeohLedger's payment took (10.00 against 2.00): the rest is a
    second settlement, not the bookkeeper recording our discount."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id = await _paid_by_our_rail(realdb, amount="248.00", discount="2.00")
    report = report_from_bill(_xero_bill_with_our_write_back("240.00", credited="10.00"), XERO_BILL)
    assert report.our_payment_applied and report.reduced_outside_amount == Decimal("10.00")

    assert (await _apply_xero(realdb, report)).action == "double_payment_flagged"
    [flag] = await _exceptions(realdb, inv_id)
    assert flag.description_code == "exception.erp_paid_twice"


async def test_a_credit_after_our_undiscounted_payment_is_paid_twice(realdb):
    """No discount was taken here, so there is no open discount for a credit to
    record: any credit on top of our write-back is a second settlement."""
    from app.services.erp_adapters.xero import report_from_bill

    inv_id = await _paid_by_our_rail(realdb, amount="248.00")
    report = report_from_bill(_xero_bill_with_our_write_back("248.00", credited="2.00"), XERO_BILL)
    assert (await _apply_xero(realdb, report)).action == "double_payment_flagged"
    assert len(await _exceptions(realdb, inv_id)) == 1


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
        bills_match_by_reference = True
        changed_bills = AsyncMock(return_value=["keyed-by-hand", XERO_BILL])
        bill_report = AsyncMock(
            return_value=ErpBillReport(bill_id=XERO_BILL, state=ErpBillState.open)
        )

    await _sweep_with(realdb, monkeypatch, FakeXero())
    read = [c.args[0] for c in FakeXero.bill_report.await_args_list]
    assert "keyed-by-hand" not in read and XERO_BILL in read


async def test_a_xero_bill_only_a_realmless_push_names_is_never_read(realdb, monkeypatch):
    """A Xero bill is matched by its id only within the recorded organisation,
    so a push that recorded no realm can never match: reading its bill back
    would spend Xero's daily allowance on a certain "unmatched". QuickBooks
    still reads such bills, since its marker can match them."""
    await _connect_xero(realdb)
    await _seed(realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=None)

    class FakeXero:
        erp_type = "xero"
        supports_status_sync = True
        bills_match_by_reference = True
        changed_bills = AsyncMock(return_value=[XERO_BILL])
        bill_report = AsyncMock(
            return_value=ErpBillReport(bill_id=XERO_BILL, state=ErpBillState.open)
        )

    await _sweep_with(realdb, monkeypatch, FakeXero())
    read = [c.args[0] for c in FakeXero.bill_report.await_args_list]
    assert XERO_BILL not in read

    factory = realdb.sessionmaker("a")
    realm = "quickbooks_online:any"
    assert await erp_status_sync._bills_we_posted(factory, [XERO_BILL], realm) == [XERO_BILL]
    assert (
        await erp_status_sync._bills_we_posted(factory, [XERO_BILL], realm, by_reference=True) == []
    )
    assert (
        await erp_status_sync._bills_we_posted(factory, [XERO_BILL], None, by_reference=True) == []
    )


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
    parts = [{"method": "check", "amount": "140.00"}, {"method": "card", "amount": "100.00"}]
    merged = erp_status_sync._merged_report(
        {"amount": "240.00", "paid_on": "2026-10-01", "reported_at": "t0", "payment_parts": parts},
        {"amount": None, "paid_on": "2026-10-01", "reported_at": "t1", "payment_parts": None},
        keep_refusal=False,
    )
    assert merged["amount"] == "240.00"
    # The breakdown of that amount stays with it (§396).
    assert merged["payment_parts"] == parts


async def test_a_bill_paid_in_xero_from_the_bank_then_a_card_is_split(realdb):
    """The follow-up's Xero case (§396): booked as all card before, so the bank
    part was left off the 1099. The adapter now reads every payment's account
    (`test_xero_status_sync_adapter.py`); here its report, as `bill_report`
    completes it, is recorded with both parts."""
    from app.services.erp_adapters.xero import report_from_bill
    from app.services.payment_methods import RailPart

    inv_id, _ = await _seed(
        realdb, status=InvoiceStatus.posted_in_erp, bill_id=XERO_BILL, realm=XERO_REALM
    )
    report = report_from_bill(_xero_bill("250.00", paid="250.00"), XERO_BILL)
    report.payment_parts = (RailPart(None, Decimal("150.00")), RailPart("card", Decimal("100.00")))
    assert (await _apply_xero(realdb, report)).action == "paid_recorded"
    [booked] = await _payments(realdb, inv_id)
    assert booked.method is None
    assert booked.rail_breakdown == [
        {"method": None, "amount": "150.00"},
        {"method": "card", "amount": "100.00"},
    ]


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
        bills_match_by_reference = True
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
