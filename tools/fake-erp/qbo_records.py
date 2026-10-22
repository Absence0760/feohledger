"""The QuickBooks Online records the fake builds: Bill, BillPayment, a voided
BillPayment, a deleted bill in change-data-capture, and the CDC envelope.

Why a module of its own: these are the shapes the backend's status sync reads
(``bill_report``, ``bills_for_payment``, ``changed_bills``), and the fake once
emitted one of them wrong. Bills named their payments ``LinkedTxn.TxnType:
"BillPayment"``, real QuickBooks says ``BillPaymentCheck`` /
``BillPaymentCreditCard``, and the adapter's tests were written from the same
belief, so CI was green over a reader that matched nothing in a real company
(docs/decisions.md §317).

Kept stdlib-only (unlike ``app.py``, which needs FastAPI and defusedxml) so the
backend suite can import it beside the recorded real responses in
``fixtures/qbo_recorded/`` and compare the two:
``backend/tests/test_qbo_recorded_replay.py`` builds records here and fails when
their structure stops matching what QuickBooks returned. The recordings are made
by ``backend/scripts/record_qbo_contract_fixtures.py`` against a real sandbox
company (backend/docs/quickbooks-online-adapter.md § Recorded payment shapes).

Money is stored as ``float`` because the fake serialises with the stdlib JSON
encoder, as Intuit's wire format is a JSON number; arithmetic goes through
``Decimal``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

#: The ``LinkedTxn.TxnType`` a Bill gives a BillPayment applied to it, by the
#: payment's ``PayType``. QuickBooks types the link by how the bill was paid,
#: never by the entity name.
BILL_PAYMENT_LINK_TYPES = {"Check": "BillPaymentCheck", "CreditCard": "BillPaymentCreditCard"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def touch(record: dict) -> None:
    """Stamp ``MetaData.LastUpdatedTime``, which change-data-capture filters on."""
    record["MetaData"] = {"LastUpdatedTime": now()}


def new_bill(body: dict, bill_id: str) -> dict:
    """A created Bill: the request body plus what QuickBooks adds."""
    total = float(sum(Decimal(str(line.get("Amount") or 0)) for line in body.get("Line") or []))
    bill = {**body, "Id": bill_id, "SyncToken": "0", "TotalAmt": total, "Balance": total}
    bill["LinkedTxn"] = []
    touch(bill)
    return bill


def bill_payment_link_type(pay_type: str) -> str:
    return BILL_PAYMENT_LINK_TYPES.get(pay_type, BILL_PAYMENT_LINK_TYPES["Check"])


def apply_payment(
    bill: dict, amount: Any, *, payment_id: str, note: str, pay_type: str, txn_date: str
) -> dict:
    """A BillPayment paying ``amount`` of ``bill``. Mutates the bill (balance
    down, the payment linked, a new ``SyncToken``) and returns the payment."""
    amount = Decimal(str(amount))
    record = {
        "Id": payment_id,
        "SyncToken": "0",
        "PayType": pay_type,
        "TxnDate": txn_date,
        "TotalAmt": float(amount),
        "PrivateNote": note,
        "VendorRef": dict(bill.get("VendorRef") or {}),
        "Line": [
            {"Amount": float(amount), "LinkedTxn": [{"TxnId": bill["Id"], "TxnType": "Bill"}]}
        ],
    }
    touch(record)
    bill["Balance"] = float(Decimal(str(bill["Balance"])) - amount)
    link = {"TxnId": payment_id, "TxnType": bill_payment_link_type(pay_type)}
    bill.setdefault("LinkedTxn", []).append(link)
    bill["SyncToken"] = str(int(bill["SyncToken"]) + 1)
    touch(bill)
    return record


def void_payment(record: dict, bill: dict | None) -> None:
    """Void a BillPayment in place, as QuickBooks keeps it: a zero total and a
    note prefixed "Voided". The bill's balance comes back."""
    if bill is not None:
        bill["Balance"] = float(Decimal(str(bill["Balance"])) + Decimal(str(record["TotalAmt"])))
        bill["SyncToken"] = str(int(bill["SyncToken"]) + 1)
        touch(bill)
    record["TotalAmt"] = 0
    record["PrivateNote"] = f"Voided - {record['PrivateNote']}"
    record["SyncToken"] = str(int(record["SyncToken"]) + 1)
    touch(record)


def deleted_bill(bill_id: str, at: str) -> dict:
    """A deleted bill as change-data-capture reports it."""
    return {"Id": bill_id, "status": "Deleted", "MetaData": {"LastUpdatedTime": at}}


def cdc_response(blocks: list[dict]) -> dict:
    """``GET cdc``'s envelope around one ``{entity: [rows]}`` block per entity."""
    return {"CDCResponse": [{"QueryResponse": blocks}]}
