"""Live contract check: the QuickBooks Online behaviour the status sync relies on,
against a real company.

Opt-in and local only (``pnpm test:erp:qbo-live``, which sets
``FEOH_QBO_LIVE=1``). Every other run skips this module. It reads the
QuickBooks connection a local tenant already holds (default ``acme``; override
with ``FEOH_QBO_LIVE_TENANT``), so connect that tenant to a company first
(Organization → ERP Integration → Connect to QuickBooks, with a real Intuit
development app's keys in ``backend/.env`` and the fake-erp overrides blanked).
A sandbox company is the intended target.

Why it exists: on 2026-10-22 production found the status sync reading a bill's
payments under ``LinkedTxn.TxnType: "BillPayment"``, which QuickBooks never
sends, while CI was green, because ``tools/fake-erp`` and the adapter tests held
the same wrong belief (docs/decisions.md §317). The recorded replay
(``test_qbo_recorded_replay.py``) pins the shapes of one recording; this module
re-asks the company whenever it is run, so a QuickBooks change or a belief the
recording never covered surfaces as a named failure.

Read-only: every request to the company is a GET (a token refresh may POST to
Intuit's token endpoint, as any sync does). Nothing in the company is created,
changed or deleted. Pay a few bills by hand in QuickBooks ("Pay Bills", from a
bank account and from a credit card) before the first run, or the cash checks
skip for want of a paid bill.

Not run in CI: it needs an interactive OAuth consent, Intuit rotates refresh
tokens, and this repository is public.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.live_erp import live_erp_adapter

LIVE = os.environ.get("FEOH_QBO_LIVE") == "1"
TENANT = os.environ.get("FEOH_QBO_LIVE_TENANT", "acme")

pytestmark = pytest.mark.skipif(
    not LIVE, reason="live QuickBooks contract check — opt in with `pnpm test:erp:qbo-live`"
)

#: What a BillPayment line may apply, as far as the status sync knows. Anything
#: else is a shape nobody has reasoned about.
_KNOWN_LINE_LINKS = frozenset({"Bill", "VendorCredit", "JournalEntry"})
#: How many records each check reads (one page; the newest first).
_SAMPLE = 100


@pytest.fixture
async def qbo():
    async with live_erp_adapter(
        TENANT, "quickbooks_online", env_var="FEOH_QBO_LIVE_TENANT"
    ) as adapter:
        yield adapter


async def _get(adapter, path: str, params: dict | None = None) -> dict:
    """One GET against the tenant's company, asserting it succeeded."""
    from app.services.erp_adapters.quickbooks_online import _json

    resp = await adapter._request("GET", path, params=params)
    assert resp.status_code == 200, f"GET {path} → {resp.status_code}"
    return _json(resp)


async def _rows(adapter, entity: str) -> list[dict]:
    statement = f"select * from {entity} ORDERBY MetaData.LastUpdatedTime DESC MAXRESULTS {_SAMPLE}"
    body = await _get(adapter, "query", {"query": statement})
    return (body.get("QueryResponse") or {}).get(entity) or []


def _payment_links(bill: dict) -> list[dict]:
    return [t for t in bill.get("LinkedTxn") or [] if "Payment" in str(t.get("TxnType") or "")]


async def _paid_bills(adapter) -> list[dict]:
    from app.services.erp_adapters.quickbooks_online import _dec

    return [
        b
        for b in await _rows(adapter, "Bill")
        if _dec(b.get("Balance")) == 0 and (_dec(b.get("TotalAmt")) or 0) > 0 and _payment_links(b)
    ]


def test_the_adapter_is_pointed_at_intuit():
    from app.config import settings
    from tests.live_erp import load_local_settings

    load_local_settings()
    assert not settings.erp_qbo_api_base, (
        "FEOH_ERP_QBO_API_BASE points the adapter at fake-erp — blank it in backend/.env "
        "(FEOH_ERP_QBO_API_BASE=) to check real QuickBooks"
    )


async def test_the_connection_is_live(qbo):
    assert await qbo.test_connection() is True, (
        f"QuickBooks refused tenant '{TENANT}'s stored connection — reconnect it under "
        "Organization → ERP Integration"
    )


async def test_a_bill_names_its_payments_only_as_the_reader_expects(qbo):
    """The §317 defect: every link a bill gives a payment must be a type the
    reader matches, or that payment is invisible to the status sync."""
    from app.services.erp_adapters.quickbooks_online import _BILL_PAYMENT_TXN_TYPES

    bills = await _rows(qbo, "Bill")
    if not bills:
        pytest.skip("the company has no bills yet")
    types = {str(t.get("TxnType")) for b in bills for t in _payment_links(b)}
    if not types:
        pytest.skip("no bill in the company has a payment yet — pay one with Pay Bills")
    assert types <= _BILL_PAYMENT_TXN_TYPES, (
        f"QuickBooks links payments as {sorted(types)}; the reader matches "
        f"{sorted(_BILL_PAYMENT_TXN_TYPES)}"
    )


async def test_every_bill_payment_has_a_known_pay_type_and_applies_known_records(qbo):
    from app.services.erp_adapters.quickbooks_online import _PAY_TYPE_METHODS

    payments = await _rows(qbo, "BillPayment")
    if not payments:
        pytest.skip("the company has no bill payments yet")
    assert {p.get("PayType") for p in payments} <= set(_PAY_TYPE_METHODS)
    applied = {
        str(t.get("TxnType"))
        for p in payments
        for line in p.get("Line") or []
        for t in line.get("LinkedTxn") or []
    }
    assert applied <= _KNOWN_LINE_LINKS, (
        f"a BillPayment applies {sorted(applied - _KNOWN_LINE_LINKS)}, which the status sync "
        "has never reasoned about — review quickbooks_online._applied_to and the cash test"
    )


async def test_a_paid_bills_payments_read_back_and_point_at_the_bill(qbo):
    bills = (await _paid_bills(qbo))[:5]
    if not bills:
        pytest.skip("the company has no paid bill yet — pay one with Pay Bills")
    for bill in bills:
        for link in _payment_links(bill):
            payment = (await _get(qbo, f"billpayment/{link['TxnId']}"))["BillPayment"]
            assert str(payment["Id"]) == str(link["TxnId"])
            assert str(bill["Id"]) in await qbo.bills_for_payment(str(payment["Id"]))


async def test_a_bill_paid_in_cash_reads_as_cash_paid_outside_feohledger(qbo):
    """The production case: a person paid a bill in full with Pay Bills."""
    from app.services.erp_adapters.base import ErpBillState
    from app.services.erp_adapters.quickbooks_online import _payment_marker

    for bill in await _paid_bills(qbo):
        payments = [
            (await _get(qbo, f"billpayment/{link['TxnId']}"))["BillPayment"]
            for link in _payment_links(bill)
        ]
        cash = all(
            Decimal(str(p.get("TotalAmt") or 0)) > 0
            and _payment_marker(p) is None
            and all(
                t.get("TxnType") == "Bill"
                for line in p.get("Line") or []
                for t in line.get("LinkedTxn") or []
            )
            for p in payments
        )
        if not cash:
            continue
        report = await qbo.bill_report(str(bill["Id"]))
        assert report.state is ErpBillState.paid
        assert report.reduced_outside is True
        assert report.settled_by_credit is False, (
            f"bill {bill['Id']}, paid in cash, read as settled by a vendor credit"
        )
        assert report.payment_method in ("check", "card")
        assert report.paid_on is not None
        assert set(report.external_payment_ids) == {str(p["Id"]) for p in payments}
        return
    pytest.skip("no bill in the company was paid wholly in cash by a person — pay one")


async def test_change_data_capture_answers_in_the_shape_the_sweep_reads(qbo):
    since = datetime.now(UTC) - timedelta(days=7)
    body = await _get(
        qbo,
        "cdc",
        {"entities": "Bill,BillPayment", "changedSince": since.isoformat(timespec="seconds")},
    )
    blocks = body.get("CDCResponse")
    assert isinstance(blocks, list) and blocks
    for block in blocks:
        assert isinstance(block.get("QueryResponse"), list)
    # And the adapter's own reading of it does not raise.
    assert isinstance(await qbo.changed_bills(since), list)


async def test_an_unknown_bill_is_answered_as_not_found(qbo):
    """``bill_report`` reads Fault 610 as "deleted"; this is what it relies on."""
    from app.services.erp_adapters.base import ErpBillState
    from app.services.erp_adapters.quickbooks_online import _object_not_found

    resp = await qbo._request("GET", "bill/999999999")
    assert _object_not_found(resp), f"GET an unknown bill → {resp.status_code}, not Fault 610"
    assert (await qbo.bill_report("999999999")).state is ErpBillState.deleted
