"""Sage Intacct direct adapter (REST API, OAuth 2.0 client credentials).

HTTP is mocked by patching ``httpx.AsyncClient`` (the house style of
``test_erp_adapter_idempotency.py``) with a factory that returns a real client
over ``httpx.MockTransport``, so the request each test inspects is exactly what
httpx would put on the wire — URL, form body, JSON body text.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import settings
from app.services.erp_adapters.base import (
    ErpInvoiceStatus,
    InvoicePayload,
    LineItemPayload,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.sage_intacct import (
    DEFAULT_API_BASE,
    SageIntacctAdapter,
    map_bill_status,
)

_RealAsyncClient = httpx.AsyncClient

CONFIG = {
    "type": "sage_intacct",
    "integration_method": "direct",
    "client_id": "cid-123",
    "client_secret": "s3cr3t-client-value",
    "company_id": "ACMECO",
    "user_id": "ws_user",
}


class FakeIntacct:
    """Routes requests to canned responses and records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.query_rows: dict[str, list[list[dict]]] = {}  # object -> pages
        self.query_status = 200
        self.create_status = 201
        self.create_body: dict = {"ia::result": {"key": "777", "id": "777"}}
        self.bill: dict | None = None
        self.delete_status = 204

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth2/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "tok-abc", "expires_in": 21600})
        if path.endswith("/services/core/query"):
            if self.query_status != 200:
                return httpx.Response(self.query_status, json={})
            body = json.loads(request.content)
            pages = self.query_rows.get(body["object"], [[]])
            idx = (body["start"] - 1) // body["size"]
            page = pages[idx] if idx < len(pages) else []
            nxt = body["start"] + body["size"] if idx + 1 < len(pages) else None
            return httpx.Response(200, json={"ia::result": page, "ia::meta": {"next": nxt}})
        if path.endswith("/objects/accounts-payable/bill") and request.method == "POST":
            return httpx.Response(self.create_status, json=self.create_body)
        if "/objects/accounts-payable/bill/" in path and request.method == "GET":
            if self.bill is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"ia::result": self.bill})
        if "/objects/accounts-payable/bill/" in path and request.method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(599)

    def of(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _run(fake: FakeIntacct, coro_fn):
    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(fake.handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        return asyncio.run(coro_fn())


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="9b2f3c1e-0000-4000-8000-000000000001",
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        vendor_erp_id="V-ACME",
        gl_account_erp_id="6100",
        description="Office supplies",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _create_json(fake: FakeIntacct) -> tuple[dict, str]:
    [req] = fake.of("POST", "/objects/accounts-payable/bill")
    text = req.content.decode()
    return json.loads(text), text


# ---------------------------------------------------------------------------
# Registry + config
# ---------------------------------------------------------------------------


def test_dispatcher_resolves_sage_intacct():
    assert isinstance(get_erp_adapter(CONFIG), SageIntacctAdapter)


def test_default_base_is_the_real_intacct_host(monkeypatch):
    monkeypatch.setattr(settings, "erp_intacct_api_base", "")
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert str(fake.requests[0].url) == f"{DEFAULT_API_BASE}/oauth2/token"


def test_operator_override_base_is_used(monkeypatch):
    monkeypatch.setattr(
        settings, "erp_intacct_api_base", "http://localhost:12112/intacct/ia/api/v1/"
    )
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert all(
        str(r.url).startswith("http://localhost:12112/intacct/ia/api/v1/") for r in fake.requests
    )


def test_missing_secret_fails_closed_naming_the_key_only():
    config = {k: v for k, v in CONFIG.items() if k != "client_secret"}
    with pytest.raises(ValueError, match="client_secret"):
        _run(FakeIntacct(), lambda: SageIntacctAdapter(config).post_invoice(_payload()))


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_token_exchange_is_client_credentials_with_secret_in_the_body_only():
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    [token_req] = fake.of("POST", "/oauth2/token")
    form = {k: v[0] for k, v in parse_qs(token_req.content.decode()).items()}
    assert form == {
        "grant_type": "client_credentials",
        "client_id": "cid-123",
        "client_secret": "s3cr3t-client-value",
        "username": "ws_user@ACMECO",
    }
    assert not any("s3cr3t" in str(r.url) for r in fake.requests)
    assert all(
        r.headers.get("authorization") == "Bearer tok-abc"
        for r in fake.requests
        if not r.url.path.endswith("/oauth2/token")
    )


def test_bill_body_posts_against_erp_ids_with_exact_decimal_strings():
    fake = FakeIntacct()
    amount = Decimal("99999999999999.99")
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(amount=amount)))
    assert result.success and result.erp_document_id == "777"
    body, text = _create_json(fake)
    assert body["vendor"] == {"id": "V-ACME"}
    assert body["billNumber"] == "INV-1"
    assert body["referenceNumber"] == "9b2f3c1e-0000-4000-8000-000000000001"
    assert body["createdDate"] == "2026-01-01" and body["dueDate"] == "2026-01-31"
    assert body["currency"] == {"txnCurrency": "USD"}
    assert body["lines"] == [
        {"glAccount": {"id": "6100"}, "txnAmount": "99999999999999.99", "memo": "Office supplies"}
    ]
    assert '"99999999999999.99"' in text and "e+" not in text.lower()


def test_tiny_amount_is_never_exponent_notation():
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(amount=Decimal("0.00001"))))
    _, text = _create_json(fake)
    assert '"txnAmount": "0.00001"' in text


def test_hostile_strings_survive_json_encoding_intact():
    hostile = '"}, "vendor": {"id": "EVIL"}, "x": "</bill>&<script>'
    fake = FakeIntacct()
    _run(
        fake,
        lambda: SageIntacctAdapter(CONFIG).post_invoice(
            _payload(description=hostile, invoice_number=hostile)
        ),
    )
    body, _ = _create_json(fake)
    assert body["vendor"] == {"id": "V-ACME"}
    assert body["billNumber"] == hostile
    assert body["lines"][0]["memo"] == hostile


def test_lines_post_per_line_when_they_sum_to_the_header_amount():
    lines = [
        LineItemPayload(
            line_number=1, total=Decimal("60.10"), gl_account_erp_id="6200", description="Licence"
        ),
        LineItemPayload(line_number=2, total=Decimal("39.90"), description="Support"),
    ]
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(line_items=lines)))
    body, _ = _create_json(fake)
    assert body["lines"] == [
        {"glAccount": {"id": "6200"}, "txnAmount": "60.10", "memo": "Licence"},
        # No account of its own → the header's.
        {"glAccount": {"id": "6100"}, "txnAmount": "39.90", "memo": "Support"},
    ]


def test_lines_that_do_not_sum_to_the_header_fall_back_to_one_line():
    """Never post a bill whose total differs from the approved amount."""
    lines = [LineItemPayload(line_number=1, total=Decimal("90.00"), gl_account_erp_id="6200")]
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(line_items=lines)))
    body, _ = _create_json(fake)
    assert body["lines"] == [
        {"glAccount": {"id": "6100"}, "txnAmount": "100.00", "memo": "Office supplies"}
    ]


def test_location_id_scopes_every_call_to_the_entity():
    fake = FakeIntacct()
    config = {**CONFIG, "location_id": "EAST"}
    _run(fake, lambda: SageIntacctAdapter(config).post_invoice(_payload()))
    api_calls = [r for r in fake.requests if not r.url.path.endswith("/oauth2/token")]
    assert api_calls and all(r.headers["x-ia-api-param-entity"] == "EAST" for r in api_calls)


# ---------------------------------------------------------------------------
# Refusals, idempotency, failures
# ---------------------------------------------------------------------------


def test_refuses_without_vendor_erp_id_before_any_http():
    fake = FakeIntacct()
    result = _run(
        fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(vendor_erp_id=None))
    )
    assert not result.success
    assert result.message == "Sage Intacct post refused: vendor_not_linked"
    assert fake.requests == []


def test_refuses_without_any_gl_account_erp_id():
    fake = FakeIntacct()
    result = _run(
        fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(gl_account_erp_id=None))
    )
    assert not result.success
    assert result.message == "Sage Intacct post refused: account_not_linked"
    assert fake.requests == []


def test_idempotency_lookup_filters_on_reference_number():
    fake = FakeIntacct()
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    [query] = fake.of("POST", "/services/core/query")
    body = json.loads(query.content)
    assert body["object"] == "accounts-payable/bill"
    assert body["filters"] == [{"$eq": {"referenceNumber": "9b2f3c1e-0000-4000-8000-000000000001"}}]


def test_existing_bill_short_circuits_without_a_second_create():
    fake = FakeIntacct()
    fake.query_rows["accounts-payable/bill"] = [[{"key": "555", "id": "555"}]]
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.success and result.erp_document_id == "555"
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


def test_failed_lookup_is_not_read_as_a_miss():
    """A 5xx on the idempotency query must not lead to a create — the first
    attempt may already have posted."""
    fake = FakeIntacct()
    fake.query_status = 503
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


def test_create_failure_message_is_pii_free():
    fake = FakeIntacct()
    fake.create_status = 422
    fake.create_body = {
        "ia::result": {
            "ia::error": {
                "message": "Validation failed for vendor 12-3456789 at 17 Bank Street",
                "submitted": {"remitTo": "PO Box 9001", "iban": "GB29NWBK60161331926819"},
            }
        }
    }
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert result.message == erp_failure_message("Sage Intacct", 422)
    for token in ("12-3456789", "Bank Street", "PO Box", "GB29", "Validation failed"):
        assert token not in result.message


def test_token_failure_raises_with_status_only():
    fake = FakeIntacct()
    fake.token_status = 401
    with pytest.raises(RuntimeError) as exc:
        _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert "HTTP 401" in str(exc.value)
    assert "s3cr3t" not in str(exc.value) and "cid-123" not in str(exc.value)


# ---------------------------------------------------------------------------
# Status + void
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"state": "draft"}, ErpInvoiceStatus.draft),
        ({"state": "submitted"}, ErpInvoiceStatus.draft),
        (
            {"state": "posted", "totalTxnAmount": "100.00", "totalTxnAmountDue": "100.00"},
            ErpInvoiceStatus.open,
        ),
        (
            {"state": "posted", "totalTxnAmount": "100.00", "totalTxnAmountDue": "0.00"},
            ErpInvoiceStatus.paid,
        ),
        ({"state": "partiallyPaid"}, ErpInvoiceStatus.partially_paid),
        ({"state": "paid"}, ErpInvoiceStatus.paid),
        ({"state": "reversed"}, ErpInvoiceStatus.cancelled),
        ({"state": "declined"}, ErpInvoiceStatus.cancelled),
        ({"state": "somethingNew"}, ErpInvoiceStatus.unknown),
        ({}, ErpInvoiceStatus.unknown),
    ],
)
def test_bill_status_mapping(record, expected):
    assert map_bill_status(record) is expected


def test_get_invoice_status_reads_the_bill_by_key():
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": "paid"}
    status = _run(fake, lambda: SageIntacctAdapter(CONFIG).get_invoice_status("777"))
    assert status is ErpInvoiceStatus.paid
    assert fake.of("GET", "/objects/accounts-payable/bill/777")


def test_get_invoice_status_unknown_when_the_bill_is_missing():
    fake = FakeIntacct()
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).get_invoice_status("1")) is (
        ErpInvoiceStatus.unknown
    )


def test_void_deletes_an_unpaid_bill():
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": "posted"}
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is True
    assert fake.of("DELETE", "/objects/accounts-payable/bill/777")


@pytest.mark.parametrize("state", ["paid", "partiallyPaid", "reversed", "selected"])
def test_void_never_deletes_a_bill_with_money_applied(state):
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": state}
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is False
    assert fake.of("DELETE", "/objects/accounts-payable/bill/777") == []


# ---------------------------------------------------------------------------
# List syncs + connection test
# ---------------------------------------------------------------------------


def test_list_vendors_follows_pagination():
    fake = FakeIntacct()
    fake.query_rows["accounts-payable/vendor"] = [
        [{"key": "1", "id": "V-ACME", "name": "Acme"}],
        [{"key": "2", "id": "V-BETA", "name": "Beta"}],
    ]
    vendors = _run(fake, lambda: SageIntacctAdapter(CONFIG).list_vendors())
    assert [(v.erp_vendor_id, v.name) for v in vendors] == [("V-ACME", "Acme"), ("V-BETA", "Beta")]
    assert len(fake.of("POST", "/services/core/query")) == 2


def test_list_gl_accounts_classifies_only_what_intacct_states():
    fake = FakeIntacct()
    fake.query_rows["general-ledger/account"] = [
        [
            {
                "id": "6100",
                "name": "Supplies",
                "accountType": "incomeStatement",
                "normalBalance": "debit",
            },
            {
                "id": "4000",
                "name": "Sales",
                "accountType": "incomeStatement",
                "normalBalance": "credit",
            },
            {"id": "1000", "name": "Cash", "accountType": "balanceSheet", "normalBalance": "debit"},
            {
                "id": "3000",
                "name": "Equity",
                "accountType": "balanceSheet",
                "normalBalance": "credit",
            },
        ]
    ]
    accounts = _run(fake, lambda: SageIntacctAdapter(CONFIG).list_gl_accounts())
    assert [(a.erp_account_id, a.account_type) for a in accounts] == [
        ("6100", "expense"),
        ("4000", "revenue"),
        ("1000", "asset"),
        ("3000", None),
    ]


def test_list_pos_maps_total_exactly_and_state():
    fake = FakeIntacct()
    fake.query_rows["purchasing/document::Purchase Order"] = [
        [
            {
                "documentNumber": "PO-1",
                "vendor.name": "Acme",
                "state": "pending",
                "txnTotal": "1250.10",
                "currency.txnCurrency": "USD",
            },
            {
                "documentNumber": "PO-2",
                "vendor": {"name": "Beta"},
                "state": "closed",
                "txnTotal": "5.00",
                "currency": {"txnCurrency": "ZAR"},
            },
        ]
    ]
    pos = _run(fake, lambda: SageIntacctAdapter(CONFIG).list_pos())
    assert [(p.po_number, p.vendor_name, p.total, p.status, p.currency) for p in pos] == [
        ("PO-1", "Acme", Decimal("1250.10"), "open", "USD"),
        ("PO-2", "Beta", Decimal("5.00"), "closed", "ZAR"),
    ]


def test_list_syncs_degrade_to_empty_on_failure():
    fake = FakeIntacct()
    fake.query_status = 500
    adapter = SageIntacctAdapter(CONFIG)
    assert _run(fake, adapter.list_vendors) == []
    assert _run(fake, adapter.list_gl_accounts) == []
    assert _run(fake, adapter.list_pos) == []


def test_test_connection():
    fake = FakeIntacct()
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is True
    fake.token_status = 401
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is False
