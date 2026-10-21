"""QuickBooks Online status sync-back and BillPayment write-back, adapter half
(decisions §295): `bill_report`, `bills_for_payment`, `changed_bills`,
`post_payment`, `void_payment`.

HTTP is mocked with `patch("httpx.AsyncClient")` like
`test_erp_quickbooks_online_adapter.py`, against a small stateful QuickBooks
holding bills and bill payments.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import erp_oauth
from app.services.erp_adapters.base import (
    BILL_NOT_IN_ERP,
    BILL_PAYMENT_VOIDED,
    PAYMENT_ACCOUNT_NOT_MAPPED,
    PAYMENT_EXCEEDS_BALANCE,
    ErpBillState,
    ErpListingIncomplete,
    PaymentPayload,
)
from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
from app.utils.json_money import dumps_exact_json

REALM = "9130"
CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": REALM},
}
CORR = "6b0f6d4c-8d2e-4a43-9b8e-3d1c2a7e9f10"
PAYMENT_ID = "0d4c1f7a-2b1e-4c55-8f0a-9e3b6a2d7c41"


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = dumps_exact_json(body).encode() if body is not None else b""
    return r


def _gone() -> MagicMock:
    return _resp(400, {"Fault": {"Error": [{"Message": "Object Not Found", "code": "610"}]}})


class _Qbo:
    def __init__(self):
        self.calls: list[dict] = []
        self.bills: dict[str, dict] = {}
        self.payments: dict[str, dict] = {}
        self.replays: dict[str, dict] = {}
        self.cdc: dict = {"CDCResponse": [{"QueryResponse": [{"Bill": []}, {"BillPayment": []}]}]}
        self.post_answer: MagicMock | None = None
        self.next_id = 900

    def bill(self, bill_id="501", total="100.00", balance=None, note=f"FeohLedger {CORR}", **kw):
        self.bills[bill_id] = {
            "Id": bill_id,
            "TotalAmt": Decimal(total),
            "Balance": Decimal(balance if balance is not None else total),
            "PrivateNote": note,
            "VendorRef": {"value": "56"},
            "SyncToken": "0",
            "LinkedTxn": [],
            **kw,
        }
        return self.bills[bill_id]

    def pay(self, bill_id, amount, *, note=None, pay_type="Check", txn_date="2026-09-30"):
        pid = str(self.next_id)
        self.next_id += 1
        self.payments[pid] = {
            "Id": pid,
            "TotalAmt": Decimal(amount),
            "PayType": pay_type,
            "TxnDate": txn_date,
            "PrivateNote": note or "",
            "SyncToken": "0",
            "Line": [
                {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]}
            ],
        }
        bill = self.bills[bill_id]
        bill["Balance"] -= Decimal(amount)
        bill["LinkedTxn"].append({"TxnId": pid, "TxnType": "BillPayment"})
        return pid

    async def request(self, method, url, params=None, content=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "content": content})
        path = url.split(f"/v3/company/{REALM}/", 1)[1]
        if path == "cdc":
            return _resp(200, self.cdc)
        if path.startswith("bill/"):
            bill = self.bills.get(path.split("/", 1)[1])
            return _resp(200, {"Bill": bill}) if bill else _gone()
        if path.startswith("billpayment/"):
            record = self.payments.get(path.split("/", 1)[1])
            return _resp(200, {"BillPayment": record}) if record else _gone()
        if method == "POST" and path == "billpayment":
            body = json.loads(content, parse_float=Decimal)
            if (params or {}).get("include") == "void":
                record = self.payments[body["Id"]]
                bill_id = record["Line"][0]["LinkedTxn"][0]["TxnId"]
                self.bills[bill_id]["Balance"] += record["TotalAmt"]
                record["TotalAmt"] = Decimal("0")
                return _resp(200, {"BillPayment": record})
            if self.post_answer is not None:
                return self.post_answer
            rid = (params or {}).get("requestid")
            if rid in self.replays:
                return _resp(200, self.replays[rid])
            bill_id = body["Line"][0]["LinkedTxn"][0]["TxnId"]
            pid = self.pay(bill_id, body["TotalAmt"], note=body["PrivateNote"])
            self.replays[rid] = {"BillPayment": self.payments[pid]}
            return _resp(200, {"BillPayment": self.payments[pid]})
        raise AssertionError(f"unexpected {method} {url}")

    def posts(self):
        return [
            c
            for c in self.calls
            if c["method"] == "POST" and (c["params"] or {}).get("include") != "void"
        ]


@pytest.fixture(autouse=True)
def token():
    with patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value="tok-1")):
        yield


def _run(qbo: _Qbo, factory):
    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.request = qbo.request
        return asyncio.run(factory())


def _adapter() -> QuickBooksOnlineAdapter:
    return QuickBooksOnlineAdapter(CONFIG)


def _payload(**overrides) -> PaymentPayload:
    base = dict(
        payment_id=PAYMENT_ID,
        bill_id="501",
        amount=Decimal("100.00"),
        currency="USD",
        paid_on=date(2026, 10, 2),
        method="ach",
        pay_from_account_erp_id="35",
    )
    base.update(overrides)
    return PaymentPayload(**base)


def test_supports_status_sync():
    assert _adapter().supports_status_sync is True


# ---------------------------------------------------------------------------
# bill_report
# ---------------------------------------------------------------------------


def test_bill_paid_by_a_person_in_quickbooks():
    qbo = _Qbo()
    qbo.bill()
    qbo.pay("501", "100.00", pay_type="CreditCard", txn_date="2026-09-30")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.correlation_id == CORR
    assert report.paid_on == date(2026, 9, 30)
    # A company card is a card rail: 1099-K, not the payer's 1099.
    assert report.payment_method == "card"
    assert report.external_payment_ids and not report.paid_only_by_us


def test_bill_paid_only_by_our_write_back_is_not_news():
    qbo = _Qbo()
    qbo.bill()
    qbo.pay("501", "100.00", note=f"FeohLedger payment {PAYMENT_ID}")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.paid_only_by_us is True
    assert report.external_payment_ids == ()


def _apply_credit(qbo: _Qbo, bill_id: str, amount: str) -> str:
    """What QuickBooks records when a vendor credit is applied to a bill: a $0
    BillPayment with one line on the bill and one on the VendorCredit."""
    pid = str(qbo.next_id)
    qbo.next_id += 1
    qbo.payments[pid] = {
        "Id": pid,
        "TotalAmt": Decimal("0"),
        "PayType": "Check",
        "TxnDate": "2026-09-30",
        "PrivateNote": "",
        "SyncToken": "0",
        "Line": [
            {"Amount": Decimal(amount), "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]},
            {
                "Amount": Decimal(amount),
                "LinkedTxn": [{"TxnId": "VC-1", "TxnType": "VendorCredit"}],
            },
        ],
    }
    bill = qbo.bills[bill_id]
    bill["Balance"] -= Decimal(amount)
    bill["LinkedTxn"].append({"TxnId": pid, "TxnType": "BillPayment"})
    return pid


def test_a_bill_settled_by_a_vendor_credit_is_not_our_write_back_nor_cash():
    """A $0 BillPayment is a credit, not a void: the bill was settled outside
    FeohLedger, without money leaving (money-path review, §295)."""
    qbo = _Qbo()
    qbo.bill()
    _apply_credit(qbo, "501", "100.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_only_by_us is False
    assert report.paid_on is None and report.payment_method is None


def test_a_partial_credit_beside_our_write_back_is_still_outside_us():
    qbo = _Qbo()
    qbo.bill(total="100.00")
    _apply_credit(qbo, "501", "20.00")
    _run(qbo, lambda: _adapter().post_payment(_payload(amount=Decimal("80.00"))))
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.paid
    assert report.reduced_outside and report.settled_by_credit
    assert report.paid_only_by_us is False


def test_a_person_voided_in_quickbooks_leaves_nothing_outside_us():
    qbo = _Qbo()
    qbo.bill()
    pid = qbo.pay("501", "100.00")
    qbo.payments[pid].update(TotalAmt=Decimal("0"), PrivateNote="Voided")
    qbo.bills["501"]["Balance"] = Decimal("100.00")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.state is ErpBillState.open and report.reduced_outside is False


def test_partial_open_deleted_and_unknown():
    qbo = _Qbo()
    qbo.bill("501", total="100.00")
    qbo.pay("501", "40.00")
    qbo.bill("502", total="50.00")
    qbo.bill("503", total="0.00")

    async def read_all():
        return [await _adapter().bill_report(b) for b in ("501", "502", "503", "999")]

    reports = _run(qbo, read_all)
    assert [r.state for r in reports] == [
        ErpBillState.partially_paid,
        ErpBillState.open,
        ErpBillState.unknown,
        ErpBillState.deleted,  # Fault 610 — QuickBooks' positive "gone"
    ]


def test_a_bill_we_never_marked_names_no_invoice():
    qbo = _Qbo()
    qbo.bill(note="typed in QuickBooks by hand")
    report = _run(qbo, lambda: _adapter().bill_report("501"))
    assert report.correlation_id is None


def test_an_unreadable_bill_raises_rather_than_reads_as_deleted():
    qbo = _Qbo()

    async def broken(method, url, **kw):
        return _resp(503)

    qbo.request = broken
    with pytest.raises(Exception):  # noqa: B017 — any failure, never a report
        _run(qbo, lambda: _adapter().bill_report("501"))


# ---------------------------------------------------------------------------
# bills_for_payment / changed_bills
# ---------------------------------------------------------------------------


def test_bills_for_payment_reads_the_linked_bills():
    qbo = _Qbo()
    qbo.bill()
    pid = qbo.pay("501", "10.00")
    assert _run(qbo, lambda: _adapter().bills_for_payment(pid)) == ["501"]
    assert _run(qbo, lambda: _adapter().bills_for_payment("404")) == []


def test_changed_bills_collects_bills_and_payment_links():
    qbo = _Qbo()
    qbo.cdc = {
        "CDCResponse": [
            {
                "QueryResponse": [
                    {"Bill": [{"Id": "501"}, {"Id": "502", "status": "Deleted"}]},
                    {
                        "BillPayment": [
                            {
                                "Id": "9",
                                "Line": [{"LinkedTxn": [{"TxnId": "503", "TxnType": "Bill"}]}],
                            },
                            {"Id": "10", "status": "Deleted"},
                        ]
                    },
                ]
            }
        ]
    }
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert _run(qbo, lambda: _adapter().changed_bills(since)) == ["501", "502", "503"]
    (call,) = qbo.calls
    assert call["params"]["entities"] == "Bill,BillPayment"
    assert call["params"]["changedSince"] == "2026-10-01T00:00:00+00:00"


def test_a_full_cdc_page_raises_instead_of_returning_part():
    qbo = _Qbo()
    qbo.cdc = {
        "CDCResponse": [{"QueryResponse": [{"Bill": [{"Id": str(i)} for i in range(1000)]}]}]
    }
    with pytest.raises(ErpListingIncomplete):
        _run(qbo, lambda: _adapter().changed_bills(datetime(2026, 10, 1, tzinfo=UTC)))


# ---------------------------------------------------------------------------
# post_payment
# ---------------------------------------------------------------------------


def test_post_payment_writes_one_bill_payment_against_the_bill():
    qbo = _Qbo()
    qbo.bill()
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert result.success, result.message
    (post,) = qbo.posts()
    assert post["params"]["requestid"] == PAYMENT_ID
    body = json.loads(post["content"], parse_float=Decimal)
    assert body["PayType"] == "Check"
    assert body["CheckPayment"] == {"BankAccountRef": {"value": "35"}}
    assert body["Line"][0]["LinkedTxn"] == [{"TxnId": "501", "TxnType": "Bill"}]
    assert body["TotalAmt"] == Decimal("100.00")
    # Money on the wire is exact: a JSON number, never a float rendering.
    assert '"TotalAmt": 100.00' in post["content"] or '"TotalAmt":100.00' in post["content"]
    assert body["PrivateNote"] == f"FeohLedger payment {PAYMENT_ID}"
    assert result.bill_balance_after == Decimal("0")


def test_post_payment_is_idempotent_on_the_payment_id():
    qbo = _Qbo()
    qbo.bill()
    first = _run(qbo, lambda: _adapter().post_payment(_payload()))
    again = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert first.success and again.success
    assert again.erp_document_id == first.erp_document_id
    assert len(qbo.posts()) == 1  # the retry found our own earlier write


def test_a_card_payment_books_from_the_card_account():
    qbo = _Qbo()
    qbo.bill()
    result = _run(qbo, lambda: _adapter().post_payment(_payload(card=True, method="virtual_card")))
    assert result.success
    body = json.loads(qbo.posts()[0]["content"])
    assert body["PayType"] == "CreditCard"
    assert body["CreditCardPayment"] == {"CCAccountRef": {"value": "35"}}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"pay_from_account_erp_id": None}, PAYMENT_ACCOUNT_NOT_MAPPED),
        ({"bill_id": "999"}, BILL_NOT_IN_ERP),
        ({"amount": Decimal("100.01")}, PAYMENT_EXCEEDS_BALANCE),
        ({"currency": "EUR"}, "currency_mismatch"),
    ],
)
def test_post_payment_refuses_before_writing(overrides, code):
    qbo = _Qbo()
    qbo.bill(CurrencyRef={"value": "USD"})
    result = _run(qbo, lambda: _adapter().post_payment(_payload(**overrides)))
    assert not result.success and not result.retryable
    assert result.refusal_code == code
    assert result.not_posted is True
    assert qbo.posts() == []


def test_a_replay_of_a_payment_since_voided_is_refused_not_reported_written():
    qbo = _Qbo()
    qbo.bill()
    first = _run(qbo, lambda: _adapter().post_payment(_payload()))
    _run(qbo, lambda: _adapter().void_payment(_payload()))  # voided "in QuickBooks"
    again = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert first.success
    assert not again.success and again.refusal_code == BILL_PAYMENT_VOIDED
    assert len(qbo.posts()) == 1


def test_a_refused_bill_payment_is_coded_from_the_fault_number_only():
    qbo = _Qbo()
    qbo.bill()
    qbo.post_answer = _resp(
        400,
        {
            "Fault": {
                "Error": [
                    {
                        "Message": "Account Period Closed",
                        "Detail": "Vendor Acme Supplies paid 100.00 from 1234-5678",
                        "code": "6210",
                    }
                ]
            }
        },
    )
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert result.refusal_code == "period_closed"
    assert result.refusal_params == {"intuit_code": "6210"}
    assert "Acme" not in (result.message or "") and "1234" not in (result.message or "")


def test_a_throttled_write_back_stays_retryable():
    qbo = _Qbo()
    qbo.bill()
    qbo.post_answer = _resp(429)
    result = _run(qbo, lambda: _adapter().post_payment(_payload()))
    assert not result.success and result.retryable


# ---------------------------------------------------------------------------
# void_payment
# ---------------------------------------------------------------------------


def test_void_payment_voids_only_our_own_bill_payment():
    qbo = _Qbo()
    qbo.bill(total="150.00")
    qbo.pay("501", "50.00", note="paid by hand")
    _run(qbo, lambda: _adapter().post_payment(_payload()))
    result = _run(qbo, lambda: _adapter().void_payment(_payload()))
    assert result.success
    voids = [c for c in qbo.calls if (c["params"] or {}).get("include") == "void"]
    assert len(voids) == 1
    body = json.loads(voids[0]["content"])
    assert body["sparse"] is True and body["SyncToken"] == "0"
    assert qbo.bills["501"]["Balance"] == Decimal("100.00")
    # Nothing of ours left: a second void is a no-op success.
    assert _run(qbo, lambda: _adapter().void_payment(_payload())).success
    assert len([c for c in qbo.calls if (c["params"] or {}).get("include") == "void"]) == 1
