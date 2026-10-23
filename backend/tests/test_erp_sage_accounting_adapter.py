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
from app.services.erp_adapters.base import (
    SYNC_LISTING_MAX_ROWS,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
    ListingGap,
)
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
    # Refused before any request: the attempt left nothing in the ERP.
    assert result.not_posted is True


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
        client.post = AsyncMock(
            return_value=_resp(
                201, {"id": "pi-1", "displayed_as": "BILL-77", "total_amount": 1150.10}
            )
        )
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
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-2", "total_amount": 1150.00}))
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
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-3", "total_amount": 80.00}))
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
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-4", "total_amount": 1150.00}))
        result = _run(_adapter(default_tax_rate_id="GB_REDUCED").post_invoice(_payload()))
    assert result.success
    assert _post_body(client)["invoice_lines"][0]["tax_rate_id"] == "GB_REDUCED"


def test_existing_invoice_carrying_our_marker_short_circuits(token):
    existing = {
        "id": "pi-old",
        "vendor_reference": "BILL-77",
        "notes": "FeohLedger corr-sage-1",
        "status": {"id": "UNPAID"},
        "total_amount": 1150.00,
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


def test_a_total_sage_changed_voids_the_invoice_and_is_not_retried(token):
    """Sage recalculates total_amount; a different one is never reported as
    posted, and the invoice just created is voided."""
    created = {"id": "pi-9", "total_amount": 1170.00}

    async def get(url, params=None, headers=None):
        if url.endswith("/purchase_invoices"):
            return _resp(200, _page([]))
        if "/ledger_accounts/" in url:
            return _resp(200, _ledger())
        if url.endswith("/purchase_invoices/pi-9"):
            return _resp(
                200,
                {"status": {"id": "UNPAID"}, "total_amount": 1170.0, "outstanding_amount": 1170.0},
            )
        raise AssertionError(f"unexpected GET {url}")

    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(side_effect=get)
        client.post = AsyncMock(return_value=_resp(201, created))
        client.delete = AsyncMock(return_value=httpx.Response(204))
        result = _run(_adapter().post_invoice(_payload()))

    assert not result.success and result.retryable is False
    assert result.message == (
        "Sage Accounting post failed: posted_total_mismatch (the bill was voided)"
    )
    assert client.delete.await_args.args[0].endswith("/purchase_invoices/pi-9")


def test_a_created_invoice_with_no_total_is_unconfirmed_not_success(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-9"}))
        result = _run(_adapter().post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.message.startswith("Sage Accounting post unconfirmed: posted_total_unconfirmed")


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
        client.post = AsyncMock(return_value=_resp(201, {"id": "pi-new", "total_amount": 1150.00}))
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


#: Sage's 422 error array, its $message echoing what we sent.
_ECHO_422 = [
    {
        "$severity": "error",
        "$dataCode": "RecordInvalid",
        "$message": "Tax number GB123456789 at 1 High Street, London is invalid",
        "$source": "contact",
    }
]


def _refusal_router(
    *,
    financial: httpx.Response | None = None,
    contact: httpx.Response | None = None,
    ledger_chart: httpx.Response | None = None,
):
    """GETs for a post whose create Sage refuses: the empty idempotency lookup,
    the ledger's tax rate, and the read-backs that explain the refusal (each
    defaulting to "nothing wrong here")."""
    financial = financial or _resp(
        200,
        {
            "year_end_lockdown_date": "2025-12-31",
            "base_currency": {"id": "GBP"},
            "multi_currency_enabled": False,
        },
    )
    contact = contact or _resp(200, {"id": VENDOR, "deleted_at": None})
    ledger_chart = ledger_chart or _resp(200, {"id": LEDGER, "included_in_chart": True})

    async def get(url, params=None, headers=None):
        if url.endswith("/purchase_invoices"):
            return _resp(200, _page([]))
        if url.endswith("/financial_settings"):
            return financial
        if url.endswith(f"/contacts/{VENDOR}"):
            return contact
        if url.endswith(f"/ledger_accounts/{LEDGER}"):
            if (params or {}).get("attributes") == "tax_rate":
                return _resp(200, _ledger())
            return ledger_chart
        raise AssertionError(f"unexpected GET {url}")

    return AsyncMock(side_effect=get)


def _post_refused(status: int, router=None, **payload_overrides):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = router or _refusal_router()
        client.post = AsyncMock(return_value=httpx.Response(status, json=_ECHO_422))
        return _run(_adapter().post_invoice(_payload(**payload_overrides)))


def test_a_refused_create_is_coded_and_carries_no_response_body(token):
    """A 422 Sage cannot be read further is ``bill_refused``: non-retryable,
    recorded as having created nothing (QuickBooks lesson 3, decisions §285),
    and the message names nothing Sage echoed back."""
    result = _post_refused(422, vendor_tax_id="GB123456789", vendor_address="1 High Street, London")
    assert result.message == "Sage Accounting post refused: bill_refused (HTTP 422)"
    assert result.refusal_code == "bill_refused"
    assert result.retryable is False
    assert result.not_posted is True
    assert "GB123456789" not in result.message and "High Street" not in result.message


def test_a_create_inside_the_year_end_lockdown_is_period_closed(token):
    router = _refusal_router(
        financial=_resp(200, {"year_end_lockdown_date": "2026-09-01"})  # == invoice date
    )
    result = _post_refused(422, router)
    assert (result.refusal_code, result.not_posted, result.retryable) == (
        "period_closed",
        True,
        False,
    )


def test_a_foreign_currency_without_multi_currency_is_currency_not_enabled(token):
    result = _post_refused(422, currency="EUR")
    assert result.refusal_code == "currency_not_enabled"
    assert result.not_posted is True


@pytest.mark.parametrize(
    "contact",
    [
        _resp(404, {}),
        _resp(200, {"id": VENDOR, "deleted_at": "2026-09-02T00:00:00Z"}),
        _resp(200, {"id": VENDOR, "gdpr_obfuscated": True}),
    ],
)
def test_a_create_naming_a_gone_contact_is_vendor_inactive(token, contact):
    result = _post_refused(422, _refusal_router(contact=contact))
    assert result.refusal_code == "vendor_inactive"
    assert result.not_posted is True


def test_a_create_on_an_out_of_chart_ledger_names_our_account_code(token):
    router = _refusal_router(ledger_chart=_resp(200, {"id": LEDGER, "included_in_chart": False}))
    result = _post_refused(422, router, gl_account="5000")
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["5000"]}


def test_an_unreadable_explanation_is_never_read_as_inactive(token):
    router = _refusal_router(
        financial=_resp(500, {}), contact=_resp(503, {}), ledger_chart=_resp(500, {})
    )
    result = _post_refused(422, router)
    assert result.refusal_code == "bill_refused"


def test_a_403_create_is_purchases_not_permitted_without_further_reads(token):
    """Accounting Start has no purchase-invoice write (the Swagger's Endpoint
    Availability lists Plus and Standard only), and nothing in the v3.1 API
    reports the plan; neither does a missing Purchases role. Sage answers 403."""
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=httpx.Response(403, json={}))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.message == "Sage Accounting post refused: purchases_not_permitted (HTTP 403)"
    assert (result.not_posted, result.retryable) == (True, False)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_create_failure_a_retry_could_change_stays_retryable_and_unknown(token, status):
    """A 5xx or 429 may have created the invoice: never coded, never not_posted."""
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = _router(lookup=_resp(200, _page([])), ledger=_resp(200, _ledger()))
        client.post = AsyncMock(return_value=httpx.Response(status, json={}))
        result = _run(_adapter().post_invoice(_payload()))
    assert result.refusal_code is None
    assert result.retryable is True
    assert result.not_posted is False


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


_PAYMENT = {"type": "PURCHASE_PAYMENT", "amount": 1150, "contact_payment": {"id": "cp-1"}}
_CREDIT = {"type": "PURCHASE_ALLOCATION", "amount": 1150, "contact_allocation": {"id": "ca-1"}}
_REFUND = {**_PAYMENT, "negative_payment": True}


@pytest.mark.parametrize(
    "status_id, allocations, expected",
    [
        ("PAID", [_PAYMENT], ErpInvoiceStatus.paid),
        ("PAID", [_CREDIT, _PAYMENT], ErpInvoiceStatus.paid),
        # A credit note allocated to the bill also sets PAID: no money moved.
        ("PAID", [_CREDIT], ErpInvoiceStatus.settled_no_payment),
        ("PAID", [], ErpInvoiceStatus.settled_no_payment),
        ("PAID", [_REFUND], ErpInvoiceStatus.settled_no_payment),
        ("PART_PAID", [_PAYMENT], ErpInvoiceStatus.partially_paid),
        ("PART_PAID", [_CREDIT], ErpInvoiceStatus.open),
    ],
)
def test_paid_needs_a_payment_sage_records(token, status_id, allocations, expected):
    """decisions §303: ``paid`` is a claim that money left. Sage's ``total_paid``
    counts allocations and discounts too, so only a ``contact_payment`` among
    ``payments_allocations`` says so."""
    invoice = {
        "id": "pi-1",
        "status": {"id": status_id},
        "total_paid": 1150,
        "payments_allocations": allocations,
    }
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, invoice))
        assert _run(_adapter().get_invoice_status("pi-1")) is expected
    params = client.get.await_args.kwargs["params"]
    assert params["show_payments_allocations"] == "true"
    assert "payments_allocations" in params["attributes"]


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


def test_list_vendors_follows_next_and_raises_on_rate_limit(token):
    """A 429 on page 2 used to end the walk with page 1, which the vendor sync
    took for every vendor and unlinked the rest (§284). It raises now."""
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(
            side_effect=[
                _resp(200, _page([{"id": "c1", "name": "One"}], next_url="https://next")),
                _resp(429, {}),
            ]
        )
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(_adapter().list_vendors())
    assert exc.value.gap is ListingGap.fetch_failed
    assert [c.kwargs["params"]["page"] for c in client.get.await_args_list] == [1, 2]


def test_list_gl_accounts_past_the_sync_bound_raises(token):
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(
            return_value=_resp(200, _page([{"id": "l1", "nominal_code": 1}], next_url="https://n"))
        )
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(_adapter().list_gl_accounts())
    assert exc.value.gap is ListingGap.truncated
    assert client.get.await_count == -(-SYNC_LISTING_MAX_ROWS // 200)


def test_list_vendors_reads_a_gdpr_obfuscated_contact_as_inactive(token):
    rows = [
        {"id": "c1", "name": "Live Ltd"},
        {"id": "c2", "name": "Obfuscated", "gdpr_obfuscated": True},
    ]
    with patch("httpx.AsyncClient") as cm:
        client = _client(cm)
        client.get = AsyncMock(return_value=_resp(200, _page(rows)))
        vendors = _run(_adapter().list_vendors())
    assert [(v.erp_vendor_id, v.active) for v in vendors] == [("c1", True), ("c2", False)]
    assert "gdpr_obfuscated" in client.get.await_args.kwargs["params"]["attributes"]


def test_list_gl_accounts_emits_out_of_chart_and_control_ledgers_inactive(token):
    """Dropping an out-of-chart ledger read as a dead link and unlinked a row
    whose ledger still exists (§284). Both chart values are asked for, since
    Sage leaves ``show_included_in_chart``'s default undocumented."""
    in_chart = [
        {"id": "l1", "nominal_code": 5000, "included_in_chart": True},
        {"id": "l2", "nominal_code": 2100, "included_in_chart": True, "is_control_account": True},
    ]
    out_of_chart = [{"id": "l3", "nominal_code": 9999, "included_in_chart": False}]

    async def get(url, params=None, headers=None):
        rows = in_chart if params["show_included_in_chart"] == "true" else out_of_chart
        return _resp(200, _page(rows))

    with patch("httpx.AsyncClient") as cm:
        _client(cm).get = AsyncMock(side_effect=get)
        accounts = _run(_adapter().list_gl_accounts())
    assert [(a.code, a.active) for a in accounts] == [
        ("5000", True),
        ("2100", False),
        ("9999", False),
    ]


def test_list_gl_accounts_maps_type_and_keeps_out_of_chart_inactive(token):
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
    assert [(a.code, a.account_type, a.erp_account_id, a.active) for a in accounts] == [
        ("5000", "expense", "l1", True),
        ("2100", "liability", "l2", True),
        ("9999", "expense", "l3", False),
    ]


def test_oauth_endpoints_default_to_sage_and_honour_the_overrides(monkeypatch):
    from app.services.erp_adapters.sage_accounting import SAGE_ACCOUNTING_OAUTH as spec

    monkeypatch.setattr(settings, "erp_sage_accounting_authorize_url", "")
    monkeypatch.setattr(settings, "erp_sage_accounting_token_url", "")
    assert erp_oauth.authorize_endpoint(spec) == "https://www.sageone.com/oauth2/auth/central"
    assert erp_oauth.token_endpoint(spec) == "https://oauth.accounting.sage.com/token"

    # The operator override is what lets the connect flow run against
    # fake-erp locally (guard rail 7); each one moves only its own endpoint.
    fake = "http://localhost:12112/sage/oauth2"
    monkeypatch.setattr(settings, "erp_sage_accounting_authorize_url", f"{fake}/auth/central")
    assert erp_oauth.authorize_endpoint(spec) == f"{fake}/auth/central"
    assert erp_oauth.token_endpoint(spec) == "https://oauth.accounting.sage.com/token"
    monkeypatch.setattr(settings, "erp_sage_accounting_token_url", f"{fake}/token")
    assert erp_oauth.token_endpoint(spec) == f"{fake}/token"


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


def _transport_client(handler):
    """``httpx.AsyncClient`` stand-in answering every request via ``handler``."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _resolve_sage(body, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/businesses")
        return httpx.Response(status, json=body)

    with patch("httpx.AsyncClient", _transport_client(handler)):
        return asyncio.run(
            SageAccountingAdapter.resolve_external_tenant_id(
                access_token="tok", token_response={}, callback_params={}
            )
        )


def test_resolve_business_reads_the_one_business():
    assert _resolve_sage({"$items": [{"id": "biz-1", "name": "Acme"}]}) == "biz-1"


def test_resolve_business_never_picks_among_several():
    assert _resolve_sage({"$items": [{"id": "biz-1"}, {"id": "biz-2"}]}) is None


def test_resolve_business_failure_is_not_connected():
    assert _resolve_sage({"$message": "nope"}, status=401) is None


def test_sage_token_endpoint_takes_credentials_in_the_body():
    assert SageAccountingAdapter.oauth_provider.token_auth == "body"
