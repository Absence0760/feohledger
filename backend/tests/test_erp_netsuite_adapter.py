"""The NetSuite adapter against fake-erp's own NetSuite routes (issue #655 N1–N5).

fake-erp's FastAPI app (``tools/fake-erp/app.py``) is loaded in process and
mounted as the adapter's transport, so every request here — the ``eid:``
lookup, the currency and refusal SuiteQL reads, the create, the read-back and
the PO line query — is answered by the same handlers the e2e suite runs
against (``frontend/tests-e2e/erp/netsuite.spec.ts``). The fake encodes our
reading of SuiteTalk REST; a NetSuite sandbox is still the only proof against
Oracle itself (#654).

Decisions §398.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.services.erp_adapters import netsuite
from app.services.erp_adapters.base import (
    ERP_REFUSAL_CODES,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
)
from app.services.erp_adapters.netsuite import NetSuiteAdapter

_FAKE_ERP = Path(__file__).resolve().parents[2] / "tools" / "fake-erp"
API_BASE = "http://fake-erp.test/netsuite/services/rest/record/v1"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _load_fake_erp():
    """fake-erp's app module, loaded once. It imports its sibling modules by
    bare name, so its directory goes on the path; ``defusedxml`` (used only by
    its Intacct / SYSPRO routes, never reached here) is not a backend
    dependency, so a stand-in is registered when it is absent."""
    if "fake_erp_app" in sys.modules:
        return sys.modules["fake_erp_app"]
    if str(_FAKE_ERP) not in sys.path:
        sys.path.insert(0, str(_FAKE_ERP))
    try:
        import defusedxml  # noqa: F401
    except ImportError:
        stub = types.ModuleType("defusedxml")
        stub.DefusedXmlException = type("DefusedXmlException", (ValueError,), {})
        stub.ElementTree = types.ModuleType("defusedxml.ElementTree")
        sys.modules["defusedxml"] = stub
        sys.modules["defusedxml.ElementTree"] = stub.ElementTree
    spec = importlib.util.spec_from_file_location("fake_erp_app", _FAKE_ERP / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake(monkeypatch):
    """fake-erp, reset, as the adapter's only transport."""
    module = _load_fake_erp()
    module.STATE = module._fresh_state()
    monkeypatch.setattr(settings, "erp_netsuite_api_base", API_BASE)
    transport = httpx.ASGITransport(app=module.app)

    def client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(netsuite.httpx, "AsyncClient", client)
    return module


def _adapter() -> NetSuiteAdapter:
    # TBA: the fake checks the header's shape, not its signature.
    return NetSuiteAdapter(
        {
            "account_id": "1234567",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "ti",
            "token_secret": "ts",
        }
    )


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="5f0d3c1e-8a7b-4c2d-9e10-6b5a4f3e2d1c",
        invoice_number="INV-NS-398",
        vendor_name="Fake NetSuite Vendor A",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 11, 2),
        vendor_erp_id="25",
        gl_account="6100",
        gl_account_erp_id="120",
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                total=Decimal("100.00"),
                gl_account="6100",
                gl_account_erp_id="120",
            )
        ],
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _run(coro):
    return asyncio.run(coro)


def _set_status(fake, doc_id: str, status: str) -> None:
    async def go():
        async with _REAL_ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=fake.app), base_url="http://fake-erp.test"
        ) as client:
            resp = await client.post(
                "/__set-status", json={"provider": "netsuite", "id": doc_id, "status": status}
            )
            assert resp.status_code == 200

    _run(go())


# ---------------------------------------------------------------------------
# A clean push, and the retry that finds it (N2: the `eid:` lookup)
# ---------------------------------------------------------------------------


def test_a_clean_push_posts_by_currency_id_and_reads_back_posted(fake):
    result = _run(_adapter().post_invoice(_payload()))

    assert result.success, result.message
    assert result.message == "Posted to NetSuite"
    (bill,) = fake.STATE["netsuite_bills"].values()
    assert result.erp_document_id == bill["id"]
    # By the currency's internal id, never its `refName` display text.
    assert bill["currency"] == {"id": "1", "refName": "USD"}
    assert bill["externalId"] == _payload().correlation_id


def test_a_retry_finds_the_bill_by_external_id_and_creates_no_second_one(fake):
    first = _run(_adapter().post_invoice(_payload()))
    again = _run(_adapter().post_invoice(_payload()))

    assert again.success, again.message
    assert "idempotent" in again.message
    assert again.erp_document_id == first.erp_document_id
    assert len(fake.STATE["netsuite_bills"]) == 1


# ---------------------------------------------------------------------------
# N4: a bill counts as posted only when NetSuite shows it posted
# ---------------------------------------------------------------------------


def test_a_bill_pending_approval_is_not_reported_posted_and_is_left_there(fake):
    """The routed vendor's bill starts Pending Approval. At the approved total
    it is neither success (the invoice would close at `done`, with nothing
    left to pay it if NetSuite's approver rejects it) nor deleted (the
    approval is NetSuite's to give)."""
    result = _run(_adapter().post_invoice(_payload(vendor_erp_id="28")))

    assert result.success is False
    assert result.retryable is False
    assert result.not_posted is False  # the bill exists
    assert result.refusal_code == "bill_not_posted"
    assert result.refusal_provider == "NetSuite"
    assert result.message == (
        "NetSuite post refused: bill_not_posted "
        "(the bill is in NetSuite and was left there: NetSuite does not show it approved)"
    )
    (bill,) = fake.STATE["netsuite_bills"].values()
    assert result.erp_document_id == bill["id"]


def test_once_approved_in_netsuite_the_retry_reports_the_same_bill_posted(fake):
    pending = _run(_adapter().post_invoice(_payload(vendor_erp_id="28")))
    _set_status(fake, pending.erp_document_id, "open")

    result = _run(_adapter().post_invoice(_payload(vendor_erp_id="28")))

    assert result.success, result.message
    assert result.erp_document_id == pending.erp_document_id
    assert len(fake.STATE["netsuite_bills"]) == 1


def test_a_bill_rejected_in_netsuite_is_refused_and_left(fake):
    pending = _run(_adapter().post_invoice(_payload(vendor_erp_id="28")))
    _set_status(fake, pending.erp_document_id, "rejected")

    result = _run(_adapter().post_invoice(_payload(vendor_erp_id="28")))

    assert result.success is False
    # Its own code: BC's `existing_invoice_not_open` sentence reads as "live
    # in the ERP", which would invite closing an invoice nobody will pay.
    assert result.refusal_code == "bill_rejected"
    assert pending.erp_document_id in fake.STATE["netsuite_bills"]


# ---------------------------------------------------------------------------
# N3 (currency) and the coded create refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("currency", ["CAD", "JPY", "usd!"])
def test_a_currency_netsuite_has_no_active_record_for_is_refused_before_the_create(fake, currency):
    """CAD is inactive in the fake, JPY absent, and `usd!` no ISO code."""
    result = _run(_adapter().post_invoice(_payload(currency=currency)))

    assert result.success is False
    assert result.retryable is False
    assert result.refusal_code == "currency_not_set_up"
    assert result.not_posted is True
    assert fake.STATE["netsuite_bills"] == {}


def test_a_bill_dated_in_a_closed_period_is_coded_period_closed(fake):
    result = _run(_adapter().post_invoice(_payload(invoice_date=date(2020, 1, 15))))

    assert result.success is False
    assert result.retryable is False
    assert result.not_posted is True
    assert result.refusal_code == "period_closed"
    assert result.message == "NetSuite post refused: period_closed (HTTP 400, invalid_request)"
    assert fake.STATE["netsuite_bills"] == {}


@pytest.mark.parametrize("vendor_id", ["27", "99"])
def test_an_inactive_or_gone_vendor_is_coded_vendor_inactive(fake, vendor_id):
    """27 is inactive in NetSuite; 99 does not exist there any more."""
    result = _run(_adapter().post_invoice(_payload(vendor_erp_id=vendor_id)))

    assert result.refusal_code == "vendor_inactive"
    assert result.not_posted is True


def test_a_gone_account_is_coded_account_inactive_naming_our_code(fake):
    line = LineItemPayload(
        line_number=1,
        description="Paper",
        total=Decimal("100.00"),
        gl_account="6999",
        gl_account_erp_id="777",
    )
    result = _run(_adapter().post_invoice(_payload(line_items=[line])))

    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["6999"]}
    assert result.not_posted is True


def test_every_code_the_adapter_returns_is_registered():
    for code in (
        netsuite.PERIOD_CLOSED,
        netsuite.VENDOR_INACTIVE,
        netsuite.ACCOUNT_INACTIVE,
        netsuite.BILL_REFUSED,
        netsuite.BILL_NOT_POSTED,
        netsuite.BILL_REJECTED,
        netsuite.CURRENCY_NOT_SET_UP,
    ):
        assert code in ERP_REFUSAL_CODES


# ---------------------------------------------------------------------------
# The PO sync: positive totals (N3) and lines (§327)
# ---------------------------------------------------------------------------


def test_po_sync_reads_the_ui_total_and_the_lines_of_live_orders(fake):
    listing = _run(_adapter().list_pos_report())
    by_number = {po.po_number: po for po in listing.pos}

    open_po = by_number["PO-FAKE-NS-501"]
    # SuiteQL states the header in posting sign (-2100.50): stored positive.
    assert open_po.total == Decimal("2100.50")
    assert [
        (
            line.description,
            line.quantity,
            line.unit_price,
            line.total,
            line.gl_account,
            line.item_code,
        )
        for line in open_po.line_items
    ] == [
        ("Widgets", Decimal(10), Decimal("150.05"), Decimal("1500.50"), None, "FAKE-NS-WIDGET"),
        ("Installation", None, None, Decimal("600.00"), "6100", None),
    ]
    assert sum(line.total for line in open_po.line_items) == open_po.total

    closed_po = by_number["PO-FAKE-NS-502"]
    assert closed_po.total == Decimal("640.00")
    # A closed order is sent without lines, which leaves the stored ones alone.
    assert closed_po.line_items == []


# ---------------------------------------------------------------------------
# Mocked: what the fake does not exercise
# ---------------------------------------------------------------------------


def _resp(status: int, body: object = None, *, text: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    raw = text if text is not None else json.dumps(body if body is not None else {})
    resp.content = raw.encode()
    resp.json = MagicMock(side_effect=lambda: json.loads(raw))
    resp.headers = {"content-type": "application/json"}
    return resp


def _rows(*rows: dict) -> MagicMock:
    return _resp(200, {"items": list(rows), "hasMore": False})


def test_po_lines_are_read_in_chunks_of_live_order_ids():
    headers = [
        {"id": str(i), "tranid": f"PO-{i}", "status": "B", "foreigntotal": -1} for i in range(150)
    ]
    headers.append({"id": "999", "tranid": "PO-CLOSED", "status": "H", "foreigntotal": -1})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _resp(200, {"items": headers, "hasMore": False}),
                _rows({"poid": "3", "lineid": "1", "foreignamount": 1}),
                _rows({"poid": "120", "lineid": "1", "foreignamount": 1}),
            ]
        )
        listing = _run(_adapter().list_pos_report())

    queries = [json.loads(c.kwargs["content"])["q"] for c in client.post.await_args_list]
    assert len(queries) == 3
    assert "FROM transactionline" in queries[1]
    assert "tl.mainline = 'F' AND tl.taxline = 'F'" in queries[1]
    assert queries[1].count(",") >= 99 and "IN (0, 1, 2," in queries[1]
    assert "IN (100, 101," in queries[2] and "999" not in queries[2]
    by_number = {po.po_number: po for po in listing.pos}
    assert len(by_number["PO-3"].line_items) == 1
    assert len(by_number["PO-120"].line_items) == 1
    assert by_number["PO-CLOSED"].line_items == []


def test_a_failed_line_read_fails_the_whole_listing():
    headers = [{"id": "501", "tranid": "PO-501", "status": "B", "foreigntotal": -10}]
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[_resp(200, {"items": headers, "hasMore": False}), _resp(400, {})]
        )
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(_adapter().list_pos_report())
    assert exc.value.collection == "purchase order lines"


def test_a_lookup_404_that_is_not_nonexistent_id_is_not_a_miss():
    """Only NetSuite's own "no such record" is a miss; any other 404 (a wrong
    path, a record type the role cannot see) fails the push before any
    create."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            return_value=_resp(
                404, {"o:errorDetails": [{"o:errorCode": "INVALID_RCRD_TYPE", "detail": "x"}]}
            )
        )
        client.post = AsyncMock()
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success is False
    assert result.message == "NetSuite post failed: HTTP 404 (not_found)"
    client.post.assert_not_awaited()
    assert "/vendorBill/eid:" in client.get.await_args.args[0]


def test_an_unreadable_currency_fails_retryably_before_any_create():
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            return_value=_resp(404, {"o:errorDetails": [{"o:errorCode": "NONEXISTENT_ID"}]})
        )
        client.post = AsyncMock(return_value=_resp(500, {}))
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "NetSuite post failed: the currency could not be read"
    assert client.post.await_count == 1  # the currency query only


def test_a_refusal_no_read_explains_is_the_shared_bill_refused():
    """The create is refused and every read made to explain it fails: "don't
    know" is never "inactive", so the code is the shared `bill_refused`."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            return_value=_resp(404, {"o:errorDetails": [{"o:errorCode": "NONEXISTENT_ID"}]})
        )
        client.post = AsyncMock(
            side_effect=[
                _rows({"id": "1", "symbol": "USD", "isinactive": "F"}),
                _resp(422, {"o:errorDetails": [{"o:errorCode": "USER_ERROR", "detail": "pii"}]}),
                _resp(500, {}),  # the period
                _resp(500, {}),  # the vendor
                _resp(500, {}),  # the accounts
            ]
        )
        result = _run(_adapter().post_invoice(_payload()))

    assert result.refusal_code == "bill_refused"
    assert result.not_posted is True
    assert result.retryable is False
    assert "pii" not in result.message


def test_a_retryable_create_answer_stays_an_uncoded_retryable_failure():
    """A 429 or 5xx may even have created the bill: never `not_posted`."""
    for status in (429, 503):
        with patch("httpx.AsyncClient") as cm:
            client = cm.return_value.__aenter__.return_value
            client.get = AsyncMock(
                return_value=_resp(404, {"o:errorDetails": [{"o:errorCode": "NONEXISTENT_ID"}]})
            )
            client.post = AsyncMock(
                side_effect=[_rows({"id": "1", "symbol": "USD", "isinactive": "F"}), _resp(status)]
            )
            result = _run(_adapter().post_invoice(_payload()))
        assert result.retryable is True
        assert result.refusal_code is None
        assert result.not_posted is False


def test_a_found_bill_in_another_currency_is_a_mismatch_not_success():
    """The lookup's bill at the approved figure but booked in GBP is not the
    approved USD bill (BC's rule, §391)."""
    found = {
        "id": "9001",
        "total": 100.00,
        "currency": {"id": "2", "refName": "GBP"},
        "status": {"id": "open"},
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _resp(200, found),
                _resp(200, found),  # void_invoice's status read: approved, left
            ]
        )
        client.post = AsyncMock(return_value=_rows({"id": "1", "symbol": "USD", "isinactive": "F"}))
        client.delete = AsyncMock()
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success is False
    assert result.refusal_code == "posted_total_mismatch"
    client.delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("record", "state"),
    [
        ({"status": {"id": "open"}}, "posted"),
        ({"status": {"id": "paidInFull"}, "approvalStatus": {"id": "2"}}, "posted"),
        ({"status": {"id": "A", "refName": "Open"}}, "posted"),
        ({"status": {"id": "VendBill:B"}}, "posted"),
        ({"status": {"refName": "Pending Approval"}}, "pending"),
        ({"status": {"id": "D"}}, "pending"),
        ({"status": {"id": "open"}, "approvalStatus": {"id": "1"}}, "pending"),
        ({"status": {"id": "E"}}, "closed"),
        ({"status": {"id": "open"}, "approvalStatus": {"id": "3"}}, "closed"),
        ({"status": {"id": "cancelled"}}, "closed"),
        ({"status": {"id": "somethingNew"}}, "pending"),
        ({}, "pending"),
    ],
)
def test_posting_state_reads_every_status_shape(record, state):
    assert netsuite._netsuite_posting_state(record) == state


# ---------------------------------------------------------------------------
# get_invoice_status: Paid In Full is paid only when vendor payments paid it
# ---------------------------------------------------------------------------


def _status_read(links: MagicMock, *, total: str = "100.00", status: str = "paidInFull"):
    bill = _resp(200, text=f'{{"id": "1001", "total": {total}, "status": {{"id": "{status}"}}}}')
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=bill)
        client.post = AsyncMock(return_value=links)
        return _run(_adapter().get_invoice_status("1001")), client


def test_paid_in_full_by_vendor_payments_is_paid():
    links = _rows(
        {"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": -60.00},
        {"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": -40.00},
    )
    status, client = _status_read(links)
    assert status is ErpInvoiceStatus.paid
    query = json.loads(client.post.await_args.kwargs["content"])["q"]
    assert "FROM NextTransactionLineLink WHERE previousdoc = 1001" in query


@pytest.mark.parametrize(
    "rows",
    [
        # A vendor credit applied beside the payment.
        [
            {"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": 60},
            {"previousdoc": "1001", "nexttype": "VendCred", "foreignamount": 40},
        ],
        # Credit only.
        [{"previousdoc": "1001", "nexttype": "VendCred", "foreignamount": 100}],
        # A payment and its reversal: the magnitudes add up, the cash does not.
        [
            {"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": -50},
            {"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": 50},
        ],
        # Payments that do not cover the bill (a write-off took the rest).
        [{"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": 0.01}],
        # A payment whose amount cannot be read.
        [{"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": None}],
        # Nothing applied at all.
        [],
    ],
)
def test_paid_in_full_without_cash_covering_it_is_settled_no_payment(rows):
    status, _ = _status_read(_rows(*rows))
    assert status is ErpInvoiceStatus.settled_no_payment


def test_paid_in_full_whose_links_cannot_be_read_is_unknown():
    status, _ = _status_read(_resp(500, {}))
    assert status is ErpInvoiceStatus.unknown


def test_a_status_letter_reads_like_its_word():
    status, client = _status_read(
        _rows({"previousdoc": "1001", "nexttype": "VendPymt", "foreignamount": 100}),
        status="B",
    )
    assert status is ErpInvoiceStatus.paid
    status, client = _status_read(_rows(), status="D")
    assert status is ErpInvoiceStatus.draft
    client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Money-path review follow-ups (decisions §398)
# ---------------------------------------------------------------------------


def _miss() -> MagicMock:
    return _resp(404, {"o:errorDetails": [{"o:errorCode": "NONEXISTENT_ID"}]})


def test_a_refused_create_whose_bill_exists_after_all_is_never_not_posted():
    """A second create racing the first (an SQS redelivery after a lost
    response) gets NetSuite's duplicate-externalId 400. The adapter looks
    again before it claims `not_posted`, finds our bill, and checks it."""
    bill = {
        "id": "1001",
        "externalId": _payload().correlation_id,
        "total": 100.00,
        "currency": {"id": "1"},
        "status": {"id": "open"},
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[_miss(), _resp(200, bill)])
        client.post = AsyncMock(
            side_effect=[
                _rows({"id": "1", "symbol": "USD", "isinactive": "F"}),
                _resp(400, {"o:errorDetails": [{"o:errorCode": "DUP_RCRD"}]}),
            ]
        )
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success, result.message
    assert result.erp_document_id == "1001"
    assert result.not_posted is False


def test_a_refused_create_whose_second_lookup_fails_is_coded_but_not_not_posted():
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[_miss(), _resp(503, {})])
        client.post = AsyncMock(
            side_effect=[
                _rows({"id": "1", "symbol": "USD", "isinactive": "F"}),
                _resp(400, {}),
                _resp(500, {}),
                _resp(500, {}),
                _resp(500, {}),
            ]
        )
        result = _run(_adapter().post_invoice(_payload()))

    assert result.refusal_code == "bill_refused"
    assert result.retryable is False
    assert result.not_posted is False  # fail closed: we could not prove it


@pytest.mark.parametrize(
    "found",
    [
        {"total": 100.00, "currency": {"id": "1"}, "status": {"id": "open"}},  # no id
        {
            "id": "1001",
            "externalId": "someone-else",
            "total": 100.00,
            "currency": {"id": "1"},
            "status": {"id": "open"},
        },
    ],
)
def test_a_lookup_hit_that_is_not_plainly_our_bill_fails_the_push(found):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, found))
        client.post = AsyncMock()
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is True  # 502: the next attempt looks again
    client.post.assert_not_awaited()


def test_a_line_chunk_past_the_bound_is_split_until_it_fits():
    """A hundred long orders must not fail every sync: a truncated chunk is
    read again in halves, and only one PO alone past the bound raises."""
    from app.services.erp_adapters.base import ListingGap

    adapter = _adapter()
    ids = [str(i) for i in range(4)]
    calls: list[list[str]] = []

    async def fake_suiteql(query, *, collection):
        chunk = query.split("IN (")[1].split(")")[0].split(", ")
        calls.append(chunk)
        if len(chunk) > 1:
            raise ErpListingIncomplete("NetSuite", collection, ListingGap.truncated)
        return [{"poid": chunk[0], "lineid": "1", "foreignamount": 1}]

    lines: dict = {}
    with patch.object(adapter, "_suiteql", side_effect=fake_suiteql):
        _run(adapter._read_po_lines(ids, lines))
    assert sorted(lines) == ids
    assert calls[0] == ids and ["0"] in calls and ["3"] in calls

    async def always_truncated(query, *, collection):
        raise ErpListingIncomplete("NetSuite", collection, ListingGap.truncated)

    with patch.object(adapter, "_suiteql", side_effect=always_truncated):
        with pytest.raises(ErpListingIncomplete):
            _run(adapter._read_po_lines(["7"], {}))

    async def failed(query, *, collection):
        raise ErpListingIncomplete("NetSuite", collection, ListingGap.fetch_failed)

    with patch.object(adapter, "_suiteql", side_effect=failed) as spy:
        with pytest.raises(ErpListingIncomplete):
            _run(adapter._read_po_lines(ids, {}))
    assert spy.call_count == 1  # never split on anything but truncation


def test_lines_stated_in_credit_sign_are_read_in_the_uis_sign():
    from app.services.erp_adapters.base import PoLinePayload

    credit = [
        PoLinePayload(quantity=Decimal(-10), unit_price=Decimal(150), total=Decimal(-1500)),
        PoLinePayload(total=Decimal(-600)),
    ]
    flipped = netsuite._credit_sign_flipped(credit)
    assert [(line.quantity, line.total) for line in flipped] == [
        (Decimal(10), Decimal(1500)),
        (None, Decimal(600)),
    ]
    assert flipped[0].unit_price == Decimal(150)

    # A positive set, a discount line included, is kept as read.
    debit = [PoLinePayload(total=Decimal(100)), PoLinePayload(total=Decimal(-10))]
    assert [line.total for line in netsuite._credit_sign_flipped(debit)] == [
        Decimal(100),
        Decimal(-10),
    ]
