"""Xero ERP adapter (`erp_adapters/xero.py`) and the shared bill-line split.

HTTP is mocked with `patch("httpx.AsyncClient")` as in
`test_erp_adapter_idempotency.py`; responses are real `httpx.Response` objects
so the adapter's Decimal-preserving JSON parse runs for real. The bearer token
comes from `OAuthErpAdapter.access_token`, which is monkeypatched: the adapter
must never read or refresh a token itself.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.services import erp_oauth
from app.services.erp_adapters import bill_allocation
from app.services.erp_adapters.base import ErpInvoiceStatus, InvoicePayload, LineItemPayload
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.xero import XeroAdapter

TENANT = "xero-tenant-0001"
VENDOR = "c0ffee00-0000-0000-0000-000000000001"
ACCOUNT = "acc00000-0000-0000-0000-000000006100"


def _run(coro):
    return asyncio.run(coro)


def _resp(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers or {})
    return httpx.Response(status, json=body, headers=headers or {})


def _adapter(**config) -> XeroAdapter:
    return XeroAdapter(
        {
            "type": "xero",
            "integration_method": "direct",
            "oauth": {"external_tenant_id": TENANT},
            **config,
        }
    )


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="corr-xero-1",
        invoice_number="SUP-1001",
        vendor_name="Acme Supplies",
        amount=Decimal("1150.00"),
        currency="ZAR",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        tax_amount=Decimal("150.00"),
        vendor_erp_id=VENDOR,
        gl_account_erp_id=ACCOUNT,
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _accounts(tax_type: str | None = "INPUT") -> dict:
    row = {"AccountID": ACCOUNT, "Code": "6100", "Name": "Supplies", "Class": "EXPENSE"}
    if tax_type:
        row["TaxType"] = tax_type
    return {"Accounts": [row]}


@pytest.fixture
def token():
    with patch.object(XeroAdapter, "access_token", AsyncMock(return_value="tok-xero")) as m:
        yield m


def _client(cm):
    return cm.return_value.__aenter__.return_value


def _get_router(*, lookup: httpx.Response, accounts: httpx.Response | None = None):
    async def get(url, params=None, headers=None):
        if url.endswith("/Invoices"):
            return lookup
        if url.endswith("/Accounts"):
            assert accounts is not None, "unexpected Accounts lookup"
            return accounts
        raise AssertionError(f"unexpected GET {url}")

    return AsyncMock(side_effect=get)


def _put_body(client) -> dict:
    raw = client.put.await_args.kwargs["content"]
    return json.loads(raw, parse_float=Decimal)["Invoices"][0]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registered_as_direct_adapter_and_oauth_provider():
    adapter = get_erp_adapter({"type": "xero", "integration_method": "direct"})
    assert isinstance(adapter, XeroAdapter)
    spec = erp_oauth.OAUTH_PROVIDERS["xero"]
    assert spec is XeroAdapter.oauth_provider
    # The named platform-credential settings exist and default to empty: no fallback.
    assert getattr(settings, spec.client_id_setting) == ""
    assert getattr(settings, spec.client_secret_setting) == ""
    assert "offline_access" in spec.scopes


# ---------------------------------------------------------------------------
# post_invoice: refusals never reach Xero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"vendor_erp_id": None}, "vendor_not_linked"),
        ({"due_date": None}, "missing_dates"),
        ({"gl_account_erp_id": None}, "account_not_linked"),
        (
            {"line_items": [LineItemPayload(line_number=1, total=Decimal("700.00"))]},
            "amount_mismatch",
        ),
    ],
)
def test_refusals_send_nothing(token, overrides, reason):
    with patch("httpx.AsyncClient", side_effect=AssertionError("no HTTP on a refusal")):
        result = _run(_adapter().post_invoice(_payload(**overrides)))
    assert result.success is False
    assert result.message == f"Xero post refused: {reason}"


# ---------------------------------------------------------------------------
# post_invoice: body shape, headers, idempotency
# ---------------------------------------------------------------------------


def test_posts_accpay_bill_with_exact_amounts_and_tenant_headers(token):
    payload = _payload(
        amount=Decimal("1150.10"),
        tax_amount=Decimal("150.01"),
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                quantity=Decimal("4"),
                unit_price=Decimal("250.03"),
                total=Decimal("1000.09"),
                tax=Decimal("150.01"),
            )
        ],
    )
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts("INPUT"))
        )
        client.put = AsyncMock(
            return_value=_resp(
                200, {"Invoices": [{"InvoiceID": "inv-1", "InvoiceNumber": "SUP-1001"}]}
            )
        )
        result = _run(_adapter().post_invoice(payload))

    assert result.success, result.message
    assert result.erp_document_id == "inv-1"

    headers = client.put.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-xero"
    assert headers["Xero-Tenant-Id"] == TENANT
    assert headers["Idempotency-Key"] == "corr-xero-1"
    for call in client.get.await_args_list:
        assert call.kwargs["headers"]["Xero-Tenant-Id"] == TENANT

    raw = client.put.await_args.kwargs["content"]
    assert '"LineAmount":1000.09' in raw and '"TaxAmount":150.01' in raw

    body = _put_body(client)
    assert body["Type"] == "ACCPAY"
    assert body["Contact"] == {"ContactID": VENDOR}
    assert body["InvoiceNumber"] == "SUP-1001"
    assert body["Date"] == "2026-09-01" and body["DueDate"] == "2026-10-01"
    assert body["CurrencyCode"] == "ZAR"
    assert body["Status"] == "AUTHORISED"
    assert body["LineAmountTypes"] == "Exclusive"
    (line,) = body["LineItems"]
    assert line["AccountID"] == ACCOUNT
    assert line["TaxType"] == "INPUT"
    assert line["LineAmount"] + line["TaxAmount"] == payload.amount
    # 4 x 250.03 != 1000.09, so Xero must not be handed a quantity to re-derive from.
    assert "Quantity" not in line


def test_no_tax_posts_notax_and_skips_the_account_lookup(token):
    payload = _payload(amount=Decimal("500.00"), tax_amount=None)
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(lookup=_resp(200, {"Invoices": []}))
        client.put = AsyncMock(return_value=_resp(200, {"Invoices": [{"InvoiceID": "inv-2"}]}))
        result = _run(_adapter(bill_status="draft").post_invoice(payload))

    assert result.success
    body = _put_body(client)
    assert body["LineAmountTypes"] == "NoTax"
    assert body["Status"] == "DRAFT"
    assert body["LineItems"][0]["LineAmount"] == Decimal("500.00")
    assert "TaxType" not in body["LineItems"][0]


def test_header_only_tax_on_inclusive_lines_lets_xero_split_but_keeps_the_total(token):
    payload = _payload(
        line_items=[
            LineItemPayload(line_number=1, description="A", total=Decimal("575.00")),
            LineItemPayload(line_number=2, description="B", total=Decimal("575.00")),
        ]
    )
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts())
        )
        client.put = AsyncMock(return_value=_resp(200, {"Invoices": [{"InvoiceID": "inv-3"}]}))
        result = _run(_adapter().post_invoice(payload))

    assert result.success
    body = _put_body(client)
    assert body["LineAmountTypes"] == "Inclusive"
    assert sum(li["LineAmount"] for li in body["LineItems"]) == payload.amount
    assert all("TaxAmount" not in li for li in body["LineItems"])


def test_account_without_tax_type_is_refused_unless_a_default_is_configured(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts(None))
        )
        client.put = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Xero post refused: tax_rate_unresolved"

    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts(None))
        )
        client.put = AsyncMock(return_value=_resp(200, {"Invoices": [{"InvoiceID": "inv-4"}]}))
        result = _run(_adapter(default_tax_type="TAX001").post_invoice(_payload()))
    assert result.success
    assert _put_body(client)["LineItems"][0]["TaxType"] == "TAX001"


def test_existing_bill_with_same_total_short_circuits(token):
    existing = {
        "InvoiceID": "inv-old",
        "InvoiceNumber": "SUP-1001",
        "Type": "ACCPAY",
        "Total": 1150.0,
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(lookup=_resp(200, {"Invoices": [existing]}))
        client.put = AsyncMock(side_effect=AssertionError("must not create a second bill"))
        result = _run(_adapter().post_invoice(_payload()))

    assert result.success
    assert result.erp_document_id == "inv-old"
    assert "idempotent" in result.message
    params = client.get.await_args.kwargs["params"]
    assert params["InvoiceNumbers"] == "SUP-1001"
    assert params["ContactIDs"] == VENDOR


def test_existing_bill_with_a_different_total_is_refused_not_adopted(token):
    existing = {
        "InvoiceID": "inv-old",
        "InvoiceNumber": "SUP-1001",
        "Type": "ACCPAY",
        "Total": 99.0,
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(lookup=_resp(200, {"Invoices": [existing]}))
        client.put = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.success is False
    assert result.message == "Xero post refused: duplicate_document_number"


# ---------------------------------------------------------------------------
# Failures: rate limits and PII
# ---------------------------------------------------------------------------


def test_rate_limited_lookup_fails_closed_without_posting(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(lookup=_resp(429, {}, {"Retry-After": "37"}))
        client.put = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.success is False
    assert result.message == "Xero post failed: HTTP 429 (rate_limited)"
    assert result.raw_response == {"retry_after": "37"}


def test_rate_limited_create_reports_rate_limited(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts())
        )
        client.put = AsyncMock(return_value=_resp(429, {}, {"Retry-After": "5"}))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Xero post failed: HTTP 429 (rate_limited)"


def test_validation_failure_message_carries_no_response_body(token):
    echo = {
        "Type": "ValidationException",
        "Elements": [
            {
                "TaxNumber": "4123456789",
                "Addresses": [{"AddressLine1": "12 Long Street, Cape Town"}],
                "ValidationErrors": [{"Message": "Account 4123456789 is invalid"}],
            }
        ],
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _get_router(
            lookup=_resp(200, {"Invoices": []}), accounts=_resp(200, _accounts())
        )
        client.put = AsyncMock(return_value=_resp(400, echo))
        result = _run(
            _adapter().post_invoice(
                _payload(vendor_tax_id="4123456789", vendor_address="12 Long Street, Cape Town")
            )
        )
    assert result.message == "Xero post failed: HTTP 400 (invalid_request)"
    assert "4123456789" not in result.message and "Long Street" not in result.message


def test_not_connected_propagates_the_fixed_message(token):
    adapter = XeroAdapter({"type": "xero", "integration_method": "direct"})
    with pytest.raises(erp_oauth.ErpNotConnectedError) as exc:
        _run(adapter.post_invoice(_payload()))
    assert str(exc.value).startswith("xero: not connected")
    assert _run(adapter.test_connection()) is False


# ---------------------------------------------------------------------------
# Status + void
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bill, expected",
    [
        ({"Status": "DRAFT"}, ErpInvoiceStatus.draft),
        ({"Status": "SUBMITTED"}, ErpInvoiceStatus.draft),
        ({"Status": "AUTHORISED", "AmountPaid": 0}, ErpInvoiceStatus.open),
        ({"Status": "AUTHORISED", "AmountPaid": 10.5}, ErpInvoiceStatus.partially_paid),
        ({"Status": "PAID"}, ErpInvoiceStatus.paid),
        ({"Status": "VOIDED"}, ErpInvoiceStatus.cancelled),
        ({"Status": "DELETED"}, ErpInvoiceStatus.cancelled),
        ({"Status": "SOMETHING_NEW"}, ErpInvoiceStatus.unknown),
    ],
)
def test_status_mapping(token, bill, expected):
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(200, {"Invoices": [bill]}))
        assert _run(_adapter().get_invoice_status("inv-1")) is expected


def test_status_unknown_on_error(token):
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(404, {}))
        assert _run(_adapter().get_invoice_status("inv-1")) is ErpInvoiceStatus.unknown


@pytest.mark.parametrize(
    "bill, target",
    [
        ({"Status": "DRAFT"}, "DELETED"),
        ({"Status": "SUBMITTED"}, "DELETED"),
        ({"Status": "AUTHORISED", "AmountPaid": 0, "AmountCredited": 0}, "VOIDED"),
    ],
)
def test_void_picks_deleted_or_voided_by_state(token, bill, target):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Invoices": [bill]}))
        client.post = AsyncMock(return_value=_resp(200, {"Invoices": [{"Status": target}]}))
        assert _run(_adapter().void_invoice("inv-1")) is True
    sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent == {"Invoices": [{"InvoiceID": "inv-1", "Status": target}]}
    assert client.post.await_args.kwargs["headers"]["Xero-Tenant-Id"] == TENANT


@pytest.mark.parametrize(
    "bill",
    [
        {"Status": "PAID"},
        {"Status": "AUTHORISED", "AmountPaid": 1, "AmountCredited": 0},
        {"Status": "AUTHORISED", "AmountPaid": 0, "AmountCredited": 2},
    ],
)
def test_void_refuses_a_bill_with_money_applied(token, bill):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Invoices": [bill]}))
        client.post = AsyncMock(side_effect=AssertionError("must not void"))
        assert _run(_adapter().void_invoice("inv-1")) is False


def test_void_of_an_already_voided_bill_is_true_without_a_write(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Invoices": [{"Status": "VOIDED"}]}))
        client.post = AsyncMock(side_effect=AssertionError("no write needed"))
        assert _run(_adapter().void_invoice("inv-1")) is True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_test_connection_reads_organisation(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Organisations": [{"Name": "Acme"}]}))
        assert _run(_adapter().test_connection()) is True
    assert client.get.await_args.args[0].endswith("/Organisation")
    assert client.get.await_args.kwargs["headers"]["Xero-Tenant-Id"] == TENANT


def test_list_vendors_filters_suppliers_and_maps_fields(token):
    contact = {
        "ContactID": VENDOR,
        "Name": "Acme Supplies",
        "AccountNumber": "ACM01",
        "EmailAddress": "ap@acme.example",
        "TaxNumber": "4123456789",
        "Phones": [{"PhoneType": "DEFAULT", "PhoneCountryCode": "27", "PhoneNumber": "215550100"}],
        "PaymentTerms": {"Bills": {"Day": 30, "Type": "DAYSAFTERBILLDATE"}},
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Contacts": [contact]}))
        vendors = _run(_adapter().list_vendors())
    assert client.get.await_args.kwargs["params"]["where"] == "IsSupplier==true"
    (v,) = vendors
    assert v.erp_vendor_id == VENDOR and v.name == "Acme Supplies" and v.code == "ACM01"
    assert v.phone == "27 215550100"
    assert v.payment_terms == "DAYSAFTERBILLDATE 30"


def test_list_vendors_degrades_to_empty_on_rate_limit(token):
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(429, {}))
        assert _run(_adapter().list_vendors()) == []


def test_list_gl_accounts_maps_class_and_skips_archived(token):
    accounts = {
        "Accounts": [
            {"AccountID": "a1", "Code": "6100", "Name": "Supplies", "Class": "EXPENSE"},
            {"AccountID": "a2", "Code": "800", "Name": "AP", "Class": "LIABILITY"},
            {
                "AccountID": "a3",
                "Code": "999",
                "Name": "Old",
                "Class": "EXPENSE",
                "Status": "ARCHIVED",
            },
        ]
    }
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(200, accounts))
        rows = _run(_adapter().list_gl_accounts())
    assert [(r.code, r.account_type, r.erp_account_id) for r in rows] == [
        ("6100", "expense", "a1"),
        ("800", "liability", "a2"),
    ]


def test_list_pos_maps_status_total_and_currency_exactly(token):
    po = {
        "PurchaseOrderNumber": "PO-0001",
        "Contact": {"Name": "Acme Supplies"},
        "Total": 1250.10,
        "Status": "BILLED",
        "CurrencyCode": "ZAR",
        "DeliveryDateString": "2026-11-01T00:00:00",
        "LineItems": [
            {"Description": "Paper", "Quantity": 2, "UnitAmount": 625.05, "LineAmount": 1250.10}
        ],
    }
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(200, {"PurchaseOrders": [po]}))
        (row,) = _run(_adapter().list_pos())
    assert row.total == Decimal("1250.10")
    assert row.status == "closed"
    assert row.currency == "ZAR"
    assert row.expected_delivery_date == date(2026, 11, 1)
    assert row.line_items[0].unit_price == Decimal("625.05")


# ---------------------------------------------------------------------------
# bill_allocation: the shared split
# ---------------------------------------------------------------------------


def test_split_refuses_header_only_tax_on_several_exclusive_lines():
    payload = _payload(
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("500.00")),
            LineItemPayload(line_number=2, total=Decimal("500.00")),
        ]
    )
    with pytest.raises(bill_allocation.BillRefusal) as exc:
        bill_allocation.allocate_bill_lines(payload)
    assert exc.value.reason == "tax_not_itemised"


def test_split_line_level_account_wins_over_the_header():
    payload = _payload(
        amount=Decimal("100.00"),
        tax_amount=None,
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("60.00"), gl_account_erp_id="line-acc"),
            LineItemPayload(line_number=2, total=Decimal("40.00")),
        ],
    )
    allocation = bill_allocation.allocate_bill_lines(payload)
    assert [line.account_erp_id for line in allocation.lines] == ["line-acc", ACCOUNT]
    assert sum(line.gross for line in allocation.lines) == payload.amount


def test_split_derives_line_amount_from_quantity_and_unit_price():
    payload = _payload(
        amount=Decimal("30.00"),
        tax_amount=None,
        line_items=[
            LineItemPayload(line_number=1, quantity=Decimal("3"), unit_price=Decimal("10.00"))
        ],
    )
    (line,) = bill_allocation.allocate_bill_lines(payload).lines
    assert line.gross == Decimal("30.00") and line.quantity == Decimal("3")


def test_split_refuses_a_line_without_any_amount():
    payload = _payload(line_items=[LineItemPayload(line_number=1, description="?")])
    with pytest.raises(bill_allocation.BillRefusal) as exc:
        bill_allocation.allocate_bill_lines(payload)
    assert exc.value.reason == "line_amount_missing"


# ---------------------------------------------------------------------------
# FEOH_ERP_XERO_API_BASE
# ---------------------------------------------------------------------------


def test_api_base_defaults_to_live_xero_and_honours_the_override(token, monkeypatch):
    from app.services.erp_adapters import xero

    monkeypatch.setattr(settings, "erp_xero_api_base", "")
    assert xero._api_base() == "https://api.xero.com/api.xro/2.0"

    monkeypatch.setattr(settings, "erp_xero_api_base", "http://localhost:12112/xero/api.xro/2.0/")
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"Organisations": [{"Name": "x"}]}))
        assert _run(_adapter().test_connection()) is True
    assert client.get.await_args.args[0] == "http://localhost:12112/xero/api.xro/2.0/Organisation"
