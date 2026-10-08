"""Sage Business Cloud Accounting adapter (`erp_adapters/sage_accounting.py`).

HTTP is mocked with `patch("httpx.AsyncClient")` as in
`test_erp_adapter_idempotency.py`; responses are real `httpx.Response` objects
so the adapter's Decimal-preserving JSON parse runs for real. The bearer token
comes from `OAuthErpAdapter.access_token`, which is monkeypatched.
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
from app.services.erp_adapters.base import ErpInvoiceStatus, InvoicePayload, LineItemPayload
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.sage_accounting import SageAccountingAdapter

BUSINESS = "sage-business-0001"
VENDOR = "sage-contact-1"
LEDGER = "sage-ledger-5000"


def _run(coro):
    return asyncio.run(coro)


def _resp(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers or {})
    return httpx.Response(status, json=body, headers=headers or {})


def _page(items: list[dict], next_url: str | None = None) -> dict:
    return {"$total": len(items), "$page": 1, "$next": next_url, "$items": items}


def _adapter(**config) -> SageAccountingAdapter:
    return SageAccountingAdapter(
        {
            "type": "sage_accounting",
            "integration_method": "direct",
            "oauth": {"external_tenant_id": BUSINESS},
            **config,
        }
    )


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="corr-sage-1",
        invoice_number="BILL-77",
        vendor_name="Acme Supplies",
        amount=Decimal("1150.00"),
        currency="GBP",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        tax_amount=Decimal("150.00"),
        vendor_erp_id=VENDOR,
        gl_account_erp_id=LEDGER,
    )
    base.update(overrides)
    return InvoicePayload(**base)


@pytest.fixture
def token():
    with patch.object(
        SageAccountingAdapter, "access_token", AsyncMock(return_value="tok-sage")
    ) as m:
        yield m


def _client(cm):
    return cm.return_value.__aenter__.return_value


def _router(*, lookup: httpx.Response, ledger: httpx.Response | None = None):
    async def get(url, params=None, headers=None):
        if url.endswith("/purchase_invoices"):
            return lookup
        if "/ledger_accounts/" in url:
            assert ledger is not None, "unexpected ledger lookup"
            return ledger
        raise AssertionError(f"unexpected GET {url}")

    return AsyncMock(side_effect=get)


def _post_body(client) -> dict:
    raw = client.post.await_args.kwargs["content"]
    return json.loads(raw, parse_float=Decimal)["purchase_invoice"]


def _ledger(rate: str | None = "GB_STANDARD") -> dict:
    body: dict = {"id": LEDGER, "displayed_as": "Purchases (5000)"}
    if rate:
        body["tax_rate"] = {"id": rate, "displayed_as": "Standard 20.00%"}
    return body


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registered_as_direct_adapter_and_oauth_provider():
    adapter = get_erp_adapter({"type": "sage_accounting", "integration_method": "direct"})
    assert isinstance(adapter, SageAccountingAdapter)
    spec = erp_oauth.OAUTH_PROVIDERS["sage_accounting"]
    assert spec is SageAccountingAdapter.oauth_provider
    assert spec.extra_authorize_params == {"filter": "apiv3.1"}
    assert spec.authorize_url == "https://www.sageone.com/oauth2/auth/central"
    assert getattr(settings, spec.client_id_setting) == ""
    assert getattr(settings, spec.client_secret_setting) == ""


# ---------------------------------------------------------------------------
# post_invoice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"vendor_erp_id": None}, "vendor_not_linked"),
        ({"invoice_date": None}, "missing_dates"),
        ({"gl_account_erp_id": None}, "account_not_linked"),
        (
            {"line_items": [LineItemPayload(line_number=1, total=Decimal("700.00"))]},
            "amount_mismatch",
        ),
        # Header-only tax on several tax-inclusive lines: Xero can split it,
        # v3.1 cannot (it never calculates tax), so Sage refuses.
        (
            {
                "line_items": [
                    LineItemPayload(line_number=1, total=Decimal("575.00")),
                    LineItemPayload(line_number=2, total=Decimal("575.00")),
                ]
            },
            "tax_not_itemised",
        ),
    ],
)
def test_refusals_send_nothing(token, overrides, reason):
    with patch("httpx.AsyncClient", side_effect=AssertionError("no HTTP on a refusal")):
        result = _run(_adapter().post_invoice(_payload(**overrides)))
    assert result.success is False
    assert result.message == f"Sage Accounting post refused: {reason}"


def test_posts_purchase_invoice_with_exact_explicit_amounts_and_business_header(token):
    payload = _payload(
        amount=Decimal("1150.10"),
        tax_amount=Decimal("150.01"),
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                quantity=Decimal("4"),
                unit_price=Decimal("250.0225"),
                total=Decimal("1000.09"),
                tax=Decimal("150.01"),
            )
        ],
    )
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-1", "displayed_as": "BILL-77"}))
        result = _run(_adapter().post_invoice(payload))

    assert result.success, result.message
    assert result.erp_document_id == "pi-1"

    headers = client.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-sage"
    assert headers["X-Business"] == BUSINESS
    for call in client.get.await_args_list:
        assert call.kwargs["headers"]["X-Business"] == BUSINESS

    raw = client.post.await_args.kwargs["content"]
    assert '"total_amount":1150.10' in raw and '"unit_price":250.0225' in raw

    body = _post_body(client)
    assert body["contact_id"] == VENDOR
    assert body["vendor_reference"] == "BILL-77"
    assert body["notes"] == "FeohLedger corr-sage-1"
    assert body["date"] == "2026-09-01" and body["due_date"] == "2026-10-01"
    assert body["currency_id"] == "GBP"
    assert body["net_amount"] + body["tax_amount"] == body["total_amount"] == payload.amount
    (line,) = body["invoice_lines"]
    assert line["ledger_account_id"] == LEDGER
    assert line["tax_rate_id"] == "GB_STANDARD"
    assert line["tax_amount"] == Decimal("150.01")
    assert line["net_amount"] == Decimal("1000.09")
    assert line["quantity"] * line["unit_price"] == line["net_amount"]
    assert line["unit_price_includes_tax"] is False


def test_tax_inclusive_single_line_is_split_from_the_invoice_not_a_rate(token):
    payload = _payload(currency="ZAR")  # 1150.00 gross, 150.00 tax, no line items
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger("ZA_STD")))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-2"}))
        result = _run(_adapter().post_invoice(payload))
    assert result.success
    (line,) = _post_body(client)["invoice_lines"]
    assert (line["net_amount"], line["tax_amount"], line["total_amount"]) == (
        Decimal("1000.00"),
        Decimal("150.00"),
        Decimal("1150.00"),
    )
    assert line["quantity"] == 1 and line["unit_price"] == Decimal("1000.00")


def test_untaxed_invoice_sends_no_tax_rate_and_skips_the_ledger_lookup(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-3"}))
        result = _run(_adapter().post_invoice(_payload(amount=Decimal("80.00"), tax_amount=None)))
    assert result.success
    (line,) = _post_body(client)["invoice_lines"]
    assert "tax_rate_id" not in line and "tax_amount" not in line
    assert line["total_amount"] == Decimal("80.00")


def test_ledger_without_a_tax_rate_is_refused_unless_a_default_is_configured(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger(None)))
        client.post = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Sage Accounting post refused: tax_rate_unresolved"

    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger(None)))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-4"}))
        result = _run(_adapter(default_tax_rate_id="GB_REDUCED").post_invoice(_payload()))
    assert result.success
    assert _post_body(client)["invoice_lines"][0]["tax_rate_id"] == "GB_REDUCED"


def test_existing_invoice_carrying_our_marker_short_circuits(token):
    existing = {
        "id": "pi-old",
        "vendor_reference": "BILL-77",
        "notes": "FeohLedger corr-sage-1",
        "status": {"id": "UNPAID"},
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([existing])))
        client.post = AsyncMock(side_effect=AssertionError("must not create a second invoice"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.success
    assert result.erp_document_id == "pi-old"
    assert "idempotent" in result.message
    params = client.get.await_args.kwargs["params"]
    assert params["contact_id"] == VENDOR
    assert params["from_date"] == params["to_date"] == "2026-09-01"


def test_existing_invoice_without_our_marker_is_refused_not_adopted(token):
    existing = {"id": "pi-manual", "vendor_reference": "BILL-77", "status": {"id": "UNPAID"}}
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([existing])))
        client.post = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Sage Accounting post refused: duplicate_document_number"


def test_a_voided_namesake_does_not_block_the_post(token):
    voided = {"id": "pi-void", "vendor_reference": "BILL-77", "status": {"id": "VOID"}}
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([voided])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-new"}))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.success and result.erp_document_id == "pi-new"


def test_lookup_that_never_ends_fails_closed(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([], next_url="https://next")))
        client.post = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Sage Accounting post refused: idempotency_lookup_incomplete"


def test_rate_limited_lookup_fails_closed_without_posting(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(429, {}, {"Retry-After": "60"}))
        client.post = AsyncMock(side_effect=AssertionError("must not post"))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Sage Accounting post failed: HTTP 429 (rate_limited)"
    assert result.raw_response == {"retry_after": "60"}


def test_validation_failure_message_carries_no_response_body(token):
    echo = [
        {
            "$severity": "error",
            "$dataCode": "RecordInvalid",
            "$message": "Tax number GB123456789 at 1 High Street, London is invalid",
            "$source": "contact",
        }
    ]
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=httpx.Response(422, json=echo))
        result = _run(
            _adapter().post_invoice(
                _payload(vendor_tax_id="GB123456789", vendor_address="1 High Street, London")
            )
        )
    assert result.message == "Sage Accounting post failed: HTTP 422 (validation_failed)"
    assert "GB123456789" not in result.message and "High Street" not in result.message


def test_not_connected_propagates_the_fixed_message(token):
    adapter = SageAccountingAdapter({"type": "sage_accounting", "integration_method": "direct"})
    with pytest.raises(erp_oauth.ErpNotConnectedError) as exc:
        _run(adapter.post_invoice(_payload()))
    assert str(exc.value).startswith("sage_accounting: not connected")
    assert _run(adapter.test_connection()) is False


# ---------------------------------------------------------------------------
# Status + void
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_id, expected",
    [
        ("DRAFT", ErpInvoiceStatus.draft),
        ("UNPAID", ErpInvoiceStatus.open),
        ("DISPUTED", ErpInvoiceStatus.open),
        ("PART_PAID", ErpInvoiceStatus.partially_paid),
        ("PAID", ErpInvoiceStatus.paid),
        ("VOID", ErpInvoiceStatus.cancelled),
        ("SOMETHING_NEW", ErpInvoiceStatus.unknown),
    ],
)
def test_status_mapping(token, status_id, expected):
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(
            return_value=_resp(200, {"id": "pi-1", "status": {"id": status_id}})
        )
        assert _run(_adapter().get_invoice_status("pi-1")) is expected


def test_status_unknown_on_error(token):
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(404, {}))
        assert _run(_adapter().get_invoice_status("pi-1")) is ErpInvoiceStatus.unknown


def test_void_sends_a_reason_for_an_unpaid_posted_invoice(token):
    invoice = {"status": {"id": "UNPAID"}, "total_amount": 1150.0, "outstanding_amount": 1150.0}
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, invoice))
        client.delete = AsyncMock(return_value=_resp(204))
        assert _run(_adapter(void_reason="Duplicate").void_invoice("pi-1")) is True
    kwargs = client.delete.await_args.kwargs
    assert client.delete.await_args.args[0].endswith("/purchase_invoices/pi-1")
    assert kwargs["params"] == {"void_reason": "Duplicate"}
    assert kwargs["headers"]["X-Business"] == BUSINESS


def test_void_deletes_a_draft_without_a_reason(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"status": {"id": "DRAFT"}}))
        client.delete = AsyncMock(return_value=_resp(204))
        assert _run(_adapter().void_invoice("pi-1")) is True
    assert client.delete.await_args.kwargs["params"] == {}


@pytest.mark.parametrize(
    "invoice",
    [
        {"status": {"id": "PAID"}, "total_amount": 10, "outstanding_amount": 0},
        {"status": {"id": "PART_PAID"}, "total_amount": 10, "outstanding_amount": 4},
        {"status": {"id": "UNPAID"}, "total_amount": 10, "outstanding_amount": 9},
    ],
)
def test_void_refuses_an_invoice_with_money_allocated(token, invoice):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, invoice))
        client.delete = AsyncMock(side_effect=AssertionError("must not void"))
        assert _run(_adapter().void_invoice("pi-1")) is False


def test_void_of_an_already_void_invoice_is_true_without_a_write(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"status": {"id": "VOID"}}))
        client.delete = AsyncMock(side_effect=AssertionError("no write needed"))
        assert _run(_adapter().void_invoice("pi-1")) is True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_test_connection_reads_business_settings(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {"siret": None}))
        assert _run(_adapter().test_connection()) is True
    assert client.get.await_args.args[0].endswith("/business_settings")
    assert client.get.await_args.kwargs["headers"]["X-Business"] == BUSINESS


def test_list_vendors_filters_to_suppliers_and_maps_fields(token):
    contact = {
        "id": VENDOR,
        "name": "Acme Supplies",
        "reference": "ACME001",
        "email": "ap@acme.example",
        "tax_number": "GB123456789",
        "credit_days": 30,
        "main_address": {"address_line_1": "1 High Street", "city": "London"},
    }
    system = {"id": "sys-1", "name": "HMRC", "system": True}
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, _page([contact, system])))
        vendors = _run(_adapter().list_vendors())
    params = client.get.await_args.kwargs["params"]
    assert params["contact_type_id"] == "VENDOR"
    (v,) = vendors
    assert (v.erp_vendor_id, v.name, v.code) == (VENDOR, "Acme Supplies", "ACME001")
    assert v.address == "1 High Street, London"
    assert v.payment_terms == "Net 30"


def test_list_vendors_follows_next_and_degrades_on_rate_limit(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(
            side_effect=[
                _resp(200, _page([{"id": "c1", "name": "One"}], next_url="https://next")),
                _resp(429, {}),
            ]
        )
        vendors = _run(_adapter().list_vendors())
    assert [v.erp_vendor_id for v in vendors] == ["c1"]
    assert [c.kwargs["params"]["page"] for c in client.get.await_args_list] == [1, 2]


def test_list_gl_accounts_maps_type_and_skips_out_of_chart(token):
    rows = [
        {
            "id": "l1",
            "name": "Purchases",
            "nominal_code": 5000,
            "ledger_account_type": {"id": "DIRECT_EXPENSES"},
            "included_in_chart": True,
        },
        {
            "id": "l2",
            "name": "Trade Creditors",
            "nominal_code": 2100,
            "ledger_account_type": {"id": "CURRENT_LIABILITY"},
        },
        {
            "id": "l3",
            "name": "Hidden",
            "nominal_code": 9999,
            "ledger_account_type": {"id": "OVERHEADS"},
            "included_in_chart": False,
        },
    ]
    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(return_value=_resp(200, _page(rows)))
        accounts = _run(_adapter().list_gl_accounts())
    assert [(a.code, a.account_type, a.erp_account_id) for a in accounts] == [
        ("5000", "expense", "l1"),
        ("2100", "liability", "l2"),
    ]


def test_api_base_defaults_to_live_sage_and_honours_the_override(token, monkeypatch):
    from app.services.erp_adapters import sage_accounting

    monkeypatch.setattr(settings, "erp_sage_accounting_api_base", "")
    assert sage_accounting._api_base() == "https://api.accounting.sage.com/v3.1"

    monkeypatch.setattr(
        settings, "erp_sage_accounting_api_base", "http://localhost:12112/sage/v3.1/"
    )
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, {}))
        assert _run(_adapter().test_connection()) is True
    assert client.get.await_args.args[0] == "http://localhost:12112/sage/v3.1/business_settings"
