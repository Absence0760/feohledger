"""Xero status sync-back and payment write-back, adapter half (decisions §320):
`bill_report`, `changed_bills`, `post_payment`, `void_payment`.

HTTP goes through a `httpx.MockTransport` in front of a small stateful Xero,
patched in as the adapter's `oauth_client`, so every request the adapter makes
is a real `httpx.Request` and every answer a real JSON body parsed by the
adapter's Decimal-preserving reader.

The double holds bills and payments in the shapes Xero's Accounting API
documents (Xero-OpenAPI `xero_accounting.yaml`): a bill lists its live payments
as `{PaymentID, Date, Amount, Reference}` with `Date` as `/Date(<ms>+0000)/`;
`AmountPaid` is the sum of those payments; credit notes, overpayments and
prepayments land in `AmountCredited` and never among the payments; a deleted
payment leaves the bill's list but stays readable from `/Payments` as
`Status: DELETED`.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.erp_adapters.base import (
    BILL_NOT_IN_ERP,
    BILL_PAYMENT_VOIDED,
    PAYMENT_ACCOUNT_NOT_MAPPED,
    PAYMENT_EXCEEDS_BALANCE,
    ErpBillState,
    ErpListingIncomplete,
    PaymentPayload,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter, status_sync_adapter_keys
from app.services.erp_adapters.xero import (
    CURRENCY_MISMATCH,
    XeroAdapter,
    XeroRequestError,
    report_from_bill,
)

TENANT = "xero-tenant-0001"
CONFIG = {
    "type": "xero",
    "integration_method": "direct",
    "oauth": {"provider": "xero", "external_tenant_id": TENANT},
}
BILL = "8a5b2d4e-1111-4c3d-9e8f-000000000501"
PAYMENT_ID = "0d4c1f7a-2b1e-4c55-8f0a-9e3b6a2d7c41"
BANK = "acc-bank-090"
CARD = "acc-card-800"


def _ms(day: date) -> str:
    epoch = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()
    return f"/Date({int(epoch * 1000)}+0000)/"


class _Xero:
    """A Xero organisation holding ACCPAY bills and their payments."""

    def __init__(self):
        self.bills: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.replays: dict[str, dict] = {}
        self.accounts = {BANK: "BANK", CARD: "CREDITCARD"}
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], httpx.Response] = {}
        self.extra_changed: list[dict] = []
        self.lose_next_put = False
        self.next_id = 1

    # -- state ---------------------------------------------------------------

    def bill(self, bill_id=BILL, total="250.00", status="AUTHORISED", currency="ZAR"):
        self.bills[bill_id] = {
            "InvoiceID": bill_id,
            "Type": "ACCPAY",
            "InvoiceNumber": "SUP-1",
            "Status": status,
            "CurrencyCode": currency,
            "Total": Decimal(total),
            "AmountDue": Decimal(total),
            "AmountPaid": Decimal("0"),
            "AmountCredited": Decimal("0"),
            "Payments": [],
            "CreditNotes": [],
        }
        return self.bills[bill_id]

    def _settle(self, bill: dict) -> None:
        bill["AmountDue"] = bill["Total"] - bill["AmountPaid"] - bill["AmountCredited"]
        if bill["Status"] in ("AUTHORISED", "PAID"):
            bill["Status"] = "PAID" if bill["AmountDue"] == 0 else "AUTHORISED"

    def pay(self, bill_id, amount, *, reference="", account=BANK, day=date(2026, 10, 2)):
        pid = f"pay-{self.next_id}"
        self.next_id += 1
        self.payments[pid] = {
            "PaymentID": pid,
            "Date": _ms(day),
            "Amount": Decimal(amount),
            "Reference": reference,
            "Status": "AUTHORISED",
            "PaymentType": "ACCPAYPAYMENT",
            "Account": {"AccountID": account},
            "Invoice": {"InvoiceID": bill_id, "Type": "ACCPAY"},
        }
        bill = self.bills[bill_id]
        bill["Payments"].append(
            {"PaymentID": pid, "Date": _ms(day), "Amount": Decimal(amount), "Reference": reference}
        )
        bill["AmountPaid"] += Decimal(amount)
        self._settle(bill)
        return pid

    def credit(self, bill_id, amount):
        bill = self.bills[bill_id]
        bill["CreditNotes"].append({"CreditNoteID": f"cn-{len(bill['CreditNotes']) + 1}"})
        bill["AmountCredited"] += Decimal(amount)
        self._settle(bill)

    def delete_payment(self, pid):
        record = self.payments[pid]
        record["Status"] = "DELETED"
        bill = self.bills[record["Invoice"]["InvoiceID"]]
        bill["Payments"] = [p for p in bill["Payments"] if p["PaymentID"] != pid]
        bill["AmountPaid"] -= record["Amount"]
        self._settle(bill)

    # -- HTTP ----------------------------------------------------------------

    @staticmethod
    def _json(status: int, body: dict) -> httpx.Response:
        def default(value):
            if isinstance(value, Decimal):
                return float(value)
            raise TypeError(value)

        return httpx.Response(status, content=json.dumps(body, default=default).encode())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Xero-Tenant-Id"] == TENANT
        path = request.url.path.split("/api.xro/2.0/", 1)[1]
        canned = self.answers.get((request.method, path.split("/")[0]))
        if canned is not None:
            return canned
        q = request.url.params
        if request.method == "GET" and path.startswith("Invoices/"):
            bill = self.bills.get(path.split("/", 1)[1])
            return self._json(200, {"Invoices": [bill]}) if bill else self._json(404, {})
        if request.method == "GET" and path == "Invoices":
            rows = [{"InvoiceID": b} for b in self.bills] + [
                r for r in self.extra_changed if "InvoiceID" in r
            ]
            return self._json(200, {"Invoices": self._page(q, rows)})
        if request.method == "GET" and path == "Payments":
            rows = list(self.payments.values())
            for field, value in re.findall(r'(Reference|PaymentType)=="([^"]*)"', q.get("where", "")):
                rows = [r for r in rows if str(r.get(field)) == value]
            return self._json(200, {"Payments": self._page(q, rows)})
        if request.method == "GET" and path.startswith("Payments/"):
            record = self.payments.get(path.split("/", 1)[1])
            return self._json(200, {"Payments": [record]}) if record else self._json(404, {})
        if request.method == "GET" and path.startswith("Accounts/"):
            kind = self.accounts.get(path.split("/", 1)[1])
            return self._json(200, {"Accounts": [{"Type": kind}]}) if kind else self._json(404, {})
        if request.method == "PUT" and path == "Payments":
            key = request.headers.get("Idempotency-Key")
            if key in self.replays:
                return self._json(200, self.replays[key])
            [item] = json.loads(request.content, parse_float=Decimal)["Payments"]
            bill = self.bills[item["Invoice"]["InvoiceID"]]
            if Decimal(str(item["Amount"])) > bill["AmountDue"]:
                return self._json(400, {"Message": "Payment amount exceeds the amount outstanding"})
            pid = self.pay(
                bill["InvoiceID"],
                str(item["Amount"]),
                reference=item["Reference"],
                account=item["Account"]["AccountID"],
                day=date.fromisoformat(item["Date"]),
            )
            answer = {"Payments": [dict(self.payments[pid])]}
            self.replays[key] = answer
            if self.lose_next_put:
                self.lose_next_put = False
                return httpx.Response(504)
            return self._json(200, answer)
        if request.method == "POST" and path.startswith("Payments/"):
            pid = path.split("/", 1)[1]
            assert json.loads(request.content) == {"Status": "DELETED"}
            self.delete_payment(pid)
            return self._json(200, {"Payments": [self.payments[pid]]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    @staticmethod
    def _page(q, rows: list) -> list:
        page, size = int(q.get("page", "1")), int(q.get("pageSize", "100"))
        return rows[(page - 1) * size : page * size]

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path)]


@pytest.fixture
def xero():
    double = _Xero()

    def client(self, *, timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(double.handle), timeout=timeout)

    with (
        patch.object(XeroAdapter, "access_token", AsyncMock(return_value="tok-xero")),
        patch.object(XeroAdapter, "oauth_client", client),
    ):
        yield double


def _adapter() -> XeroAdapter:
    return XeroAdapter(CONFIG)


def _payload(**kw) -> PaymentPayload:
    base = dict(
        payment_id=PAYMENT_ID,
        bill_id=BILL,
        amount=Decimal("250.00"),
        currency="ZAR",
        paid_on=date(2026, 10, 3),
        pay_from_account_erp_id=BANK,
    )
    base.update(kw)
    return PaymentPayload(**base)


def _report(xero_double, bill_id=BILL):
    return asyncio.run(_adapter().bill_report(bill_id))


# ---------------------------------------------------------------------------
# the capability
# ---------------------------------------------------------------------------


def test_xero_reports_its_bills_back():
    adapter = get_erp_adapter(CONFIG)
    assert adapter.supports_status_sync is True
    assert "xero" in status_sync_adapter_keys()
    assert "quickbooks_online" in status_sync_adapter_keys()
    assert "netsuite" not in status_sync_adapter_keys()


def test_the_connection_asks_for_the_payments_scope():
    assert "accounting.payments" in XeroAdapter.oauth_provider.scopes


# ---------------------------------------------------------------------------
# bill_report: paid / part-paid / credited / voided (§303)
# ---------------------------------------------------------------------------


def test_an_open_bill_reads_open_and_is_matched_by_its_id(xero):
    xero.bill()
    report = _report(xero)
    assert report.state is ErpBillState.open
    assert not report.reduced_outside
    # Xero bills carry no marker of ours: the GUID is the match (§320).
    assert report.correlation_id is None and report.match_by_reference is True
    assert report.bill_id == BILL


def test_a_bill_a_person_paid_from_the_bank_is_cash_paid_outside_us(xero):
    xero.bill()
    pid = xero.pay(BILL, "250.00", day=date(2026, 9, 28))
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and not report.settled_by_credit
    assert report.paid_on == date(2026, 9, 28)
    # A bank account states no rail: "not stated", so 1099-reportable.
    assert report.payment_method is None
    assert report.external_payment_ids == (pid,)
    assert not report.paid_only_by_us


def test_a_bill_paid_from_a_company_card_reads_as_card(xero):
    xero.bill()
    xero.pay(BILL, "250.00", account=CARD)
    report = _report(xero)
    assert report.state is ErpBillState.paid and not report.settled_by_credit
    assert report.payment_method == "card"


def test_a_connection_without_the_payments_scope_reads_the_rail_as_not_stated(xero):
    xero.bill()
    xero.pay(BILL, "250.00", account=CARD)
    xero.answers[("GET", "Payments")] = httpx.Response(403)
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.payment_method is None


def test_a_part_paid_bill_reads_partially_paid_outside_us(xero):
    xero.bill()
    xero.pay(BILL, "100.00")
    report = _report(xero)
    assert report.state is ErpBillState.partially_paid
    assert report.reduced_outside and not report.settled_by_credit


def test_a_bill_settled_by_a_credit_note_is_not_a_payment(xero):
    """Xero marks it PAID, but no money moved: AmountPaid is 0 and the credit
    is in AmountCredited. Never booked as cash (§303)."""
    xero.bill()
    xero.credit(BILL, "250.00")
    assert xero.bills[BILL]["Status"] == "PAID"
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_on is None and report.external_payment_ids == ()


def test_a_bill_settled_part_by_cash_part_by_credit_is_settled_by_credit(xero):
    xero.bill()
    xero.pay(BILL, "200.00")
    xero.credit(BILL, "50.00")
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.settled_by_credit


@pytest.mark.parametrize("status", ["VOIDED", "DELETED"])
def test_a_voided_or_deleted_bill_reads_deleted(xero, status):
    xero.bill(status=status)
    assert _report(xero).state is ErpBillState.deleted


def test_a_bill_paid_only_by_our_write_back_is_not_news(xero):
    xero.bill()
    xero.pay(BILL, "250.00", reference=f"FeohLedger payment {PAYMENT_ID}")
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.paid_only_by_us and not report.reduced_outside
    assert report.external_payment_ids == ()


def test_our_write_back_plus_a_persons_payment_is_outside_us(xero):
    xero.bill()
    xero.pay(BILL, "150.00", reference=f"FeohLedger payment {PAYMENT_ID}")
    xero.pay(BILL, "100.00")
    report = _report(xero)
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and not report.settled_by_credit
    assert not report.paid_only_by_us


def test_payments_that_do_not_add_up_read_unknown():
    """The listed payments must account for AmountPaid exactly, or nothing we
    read about who paid can be trusted (unknown: the dispatch check holds)."""
    bill = {
        "InvoiceID": BILL,
        "Type": "ACCPAY",
        "Status": "PAID",
        "Total": Decimal("250"),
        "AmountDue": Decimal("0"),
        "AmountPaid": Decimal("250"),
        "AmountCredited": Decimal("0"),
        "Payments": [],
    }
    assert report_from_bill(bill, BILL).state is ErpBillState.unknown
    assert report_from_bill({**bill, "Type": "ACCREC"}, BILL).state is ErpBillState.unknown


@pytest.mark.parametrize("status_code", [404, 500, 429])
def test_an_unreadable_bill_raises_and_never_reads_gone(xero, status_code):
    xero.answers[("GET", "Invoices")] = httpx.Response(status_code)
    with pytest.raises(XeroRequestError):
        _report(xero)


# ---------------------------------------------------------------------------
# changed_bills (If-Modified-Since)
# ---------------------------------------------------------------------------


def test_changed_bills_reads_bills_and_bill_payments_since_the_mark(xero):
    xero.bill()
    xero.bill("other-bill")
    xero.pay("other-bill", "10.00")
    since = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    changed = asyncio.run(_adapter().changed_bills(since))
    assert changed == [BILL, "other-bill"]  # de-duplicated, bills first
    for request in xero.sent("GET", "/Invoices") + xero.sent("GET", "/Payments"):
        assert request.headers["If-Modified-Since"] == "2026-10-20T06:00:00+00:00"
        assert request.url.params["pageSize"] == "1000"
    [invoices] = xero.sent("GET", "/Invoices")
    assert invoices.url.params["where"] == 'Type=="ACCPAY"'
    [payments] = xero.sent("GET", "/Payments")
    assert payments.url.params["where"] == 'PaymentType=="ACCPAYPAYMENT"'


def test_changed_bills_pages_past_a_hundred_rows(xero):
    """Xero answering 100 rows a page (ignoring pageSize) must not read as the end."""
    xero.extra_changed = [{"InvoiceID": f"b-{i}"} for i in range(150)]

    def small_pages(q, rows):
        page = int(q.get("page", "1"))
        return rows[(page - 1) * 100 : page * 100]

    xero._page = small_pages
    changed = asyncio.run(_adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))
    assert len(changed) == 150


def test_changed_bills_past_the_listing_cap_raises_incomplete(xero):
    xero._page = lambda q, rows: [{"InvoiceID": f"b-{q.get('page')}-{i}"} for i in range(100)]
    with pytest.raises(ErpListingIncomplete):
        asyncio.run(_adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))


def test_a_connection_without_the_payments_scope_falls_back_to_a_full_read(xero):
    xero.answers[("GET", "Payments")] = httpx.Response(403)
    with pytest.raises(ErpListingIncomplete):
        asyncio.run(_adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))


def test_a_failed_change_listing_raises_so_the_mark_stays(xero):
    xero.answers[("GET", "Invoices")] = httpx.Response(503)
    with pytest.raises(XeroRequestError):
        asyncio.run(_adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))


# ---------------------------------------------------------------------------
# post_payment: the write-back (idempotent, discount, refusals)
# ---------------------------------------------------------------------------


def test_post_payment_applies_the_payment_to_the_bill(xero):
    xero.bill()
    result = asyncio.run(_adapter().post_payment(_payload()))
    assert result.success, result.message
    assert result.bill_balance_after == Decimal("0")
    [put] = xero.sent("PUT", "/Payments")
    [item] = json.loads(put.content, parse_float=Decimal)["Payments"]
    assert item == {
        "Invoice": {"InvoiceID": BILL},
        "Account": {"AccountID": BANK},
        "Date": "2026-10-03",
        "Amount": Decimal("250.00"),
        "Reference": f"FeohLedger payment {PAYMENT_ID}",
    }
    assert put.headers["Idempotency-Key"]
    # Our own write-back is not news to the status sync.
    report = _report(xero)
    assert report.state is ErpBillState.paid and report.paid_only_by_us


def test_post_payment_on_retry_finds_its_own_payment_and_writes_nothing(xero):
    xero.bill()
    first = asyncio.run(_adapter().post_payment(_payload()))
    second = asyncio.run(_adapter().post_payment(_payload()))
    assert first.success and second.success
    assert second.erp_document_id == first.erp_document_id
    assert len(xero.sent("PUT", "/Payments")) == 1
    assert xero.bills[BILL]["AmountPaid"] == Decimal("250.00")


def test_a_payment_whose_answer_was_lost_is_found_on_retry_not_posted_twice(xero):
    """Xero created the payment but the answer never arrived (a 504): the retry
    finds it by its marker and reports it, writing nothing."""
    xero.bill()
    xero.lose_next_put = True
    lost = asyncio.run(_adapter().post_payment(_payload()))
    assert not lost.success and not lost.not_posted  # the outcome is unknown
    retry = asyncio.run(_adapter().post_payment(_payload()))
    assert retry.success
    assert len(xero.sent("PUT", "/Payments")) == 1
    assert xero.bills[BILL]["AmountPaid"] == Decimal("250.00")


def test_every_attempt_for_one_payment_sends_the_same_idempotency_key(xero):
    """The second layer, for the race the lookup cannot see: Xero answers a
    repeated key with its original response instead of a second payment."""
    xero.bill(total="500.00")
    xero.answers[("PUT", "Payments")] = httpx.Response(503)
    asyncio.run(_adapter().post_payment(_payload()))
    del xero.answers[("PUT", "Payments")]
    asyncio.run(_adapter().post_payment(_payload()))
    asyncio.run(_adapter().post_payment(_payload(payment_id=PAYMENT_ID.replace("0d", "1d"))))
    keys = [r.headers["Idempotency-Key"] for r in xero.sent("PUT", "/Payments")]
    assert keys[0] == keys[1] != keys[2]
    assert len(keys[0]) <= 128


def test_post_payment_refuses_to_repost_a_payment_someone_deleted_in_xero(xero):
    xero.bill()
    first = asyncio.run(_adapter().post_payment(_payload()))
    xero.delete_payment(first.erp_document_id)
    again = asyncio.run(_adapter().post_payment(_payload()))
    assert not again.success and again.refusal_code == BILL_PAYMENT_VOIDED
    assert len(xero.sent("PUT", "/Payments")) == 1


def test_post_payment_with_a_discount_leaves_the_rest_due_and_says_so(xero):
    """A Xero payment has no discount field: we post what reached the supplier
    and report what Xero still shows due, which the write-back flags."""
    xero.bill()
    result = asyncio.run(_adapter().post_payment(_payload(amount=Decimal("245.00"))))
    assert result.success
    assert result.bill_balance_after == Decimal("5.00")


def test_post_payment_never_overpays_the_bill(xero):
    xero.bill()
    xero.pay(BILL, "100.00")
    result = asyncio.run(_adapter().post_payment(_payload()))
    assert not result.success and result.refusal_code == PAYMENT_EXCEEDS_BALANCE
    assert result.not_posted
    assert xero.sent("PUT", "/Payments") == []


@pytest.mark.parametrize(
    ("setup", "payload", "code"),
    [
        (lambda x: x.bill(), {"pay_from_account_erp_id": None}, PAYMENT_ACCOUNT_NOT_MAPPED),
        (lambda x: x.bill(status="VOIDED"), {}, BILL_NOT_IN_ERP),
        (lambda x: x.bill(currency="GBP"), {}, CURRENCY_MISMATCH),
    ],
)
def test_post_payment_refuses_before_writing(xero, setup, payload, code):
    setup(xero)
    result = asyncio.run(_adapter().post_payment(_payload(**payload)))
    assert not result.success and result.refusal_code == code and result.not_posted
    assert xero.sent("PUT", "/Payments") == []


def test_a_payment_xero_refuses_is_a_failure_that_names_no_body(xero):
    xero.bill()
    xero.answers[("PUT", "Payments")] = httpx.Response(400, json={"Message": "secret detail"})
    result = asyncio.run(_adapter().post_payment(_payload()))
    assert not result.success and result.not_posted
    assert "secret detail" not in result.message


# ---------------------------------------------------------------------------
# void_payment
# ---------------------------------------------------------------------------


def test_void_payment_deletes_ours_once(xero):
    xero.bill()
    xero.pay(BILL, "50.00")  # a person's payment: never touched
    posted = asyncio.run(_adapter().post_payment(_payload(amount=Decimal("200.00"))))
    voided = asyncio.run(_adapter().void_payment(_payload(amount=Decimal("200.00"))))
    assert voided.success and voided.erp_document_id == posted.erp_document_id
    assert xero.payments[posted.erp_document_id]["Status"] == "DELETED"
    assert xero.bills[BILL]["AmountDue"] == Decimal("200.00")
    again = asyncio.run(_adapter().void_payment(_payload()))
    assert again.success and again.erp_document_id is None
    assert len(xero.sent("POST", posted.erp_document_id)) == 1
