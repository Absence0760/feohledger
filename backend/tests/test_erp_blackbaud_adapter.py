"""Blackbaud Financial Edge NXT direct adapter (SKY API, OAuth 2.0 auth code).

HTTP is mocked by patching ``httpx.AsyncClient`` with a factory that returns a
real client over ``httpx.MockTransport`` (the house style of
``test_erp_sage_intacct_adapter.py``), so each request inspected is exactly
what httpx would put on the wire. Bearer tokens come from
``OAuthErpAdapter.access_token``, monkeypatched here: ``services/erp_oauth``
owns token exchange and refresh.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services import erp_oauth
from app.services.erp_adapters import blackbaud_fe_nxt as bb
from app.services.erp_adapters.base import ErpInvoiceStatus, InvoicePayload, LineItemPayload
from app.services.erp_adapters.blackbaud_fe_nxt import (
    DEFAULT_API_BASE,
    BlackbaudFeNxtAdapter,
    map_invoice_status,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter

_RealAsyncClient = httpx.AsyncClient

CORRELATION = "9b2f3c1e-0000-4000-8000-000000000001"
MARKER = f"[feoh:{CORRELATION}]"

CONFIG = {
    "type": "blackbaud_fe_nxt",
    "integration_method": "direct",
    "ap_account_number": "01-2000-00",
    "currency": "USD",
    "oauth": {"external_tenant_id": "p-env-123"},
}


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_subscription_key", "platform-sub-key")
    monkeypatch.setattr(settings, "erp_blackbaud_api_base", "")
    monkeypatch.setattr(bb, "PROCESS_POLL_INTERVAL_SECONDS", 0)

    async def fake_token(self):
        return "tok-abc"

    monkeypatch.setattr(BlackbaudFeNxtAdapter, "access_token", fake_token)


class FakeSky:
    """Routes SKY requests to canned responses and records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.invoices: list[dict] = []  # rows the list endpoint returns
        self.list_status = 200
        self.list_headers: dict[str, str] = {}
        self.process_status = 200
        self.job_statuses: list[int] = [5]  # successive status reads
        self.status_http = 200
        self.result_http = 200
        self.record_id = 4975
        self.invoice: dict | None = None
        self.collections: dict[str, list[dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/accountspayable/v1/invoices" and request.method == "GET":
            if self.list_status != 200:
                return httpx.Response(self.list_status, headers=self.list_headers, json={})
            return httpx.Response(200, json={"count": len(self.invoices), "value": self.invoices})
        if path == "/accountspayable/v1/invoices/process":
            if self.process_status != 200:
                return httpx.Response(
                    self.process_status,
                    json={"Error": "Failed to save", "Details": ["Acme Ltd 12-3456789 rejected"]},
                )
            return httpx.Response(200, json={"process_id": 843})
        if path.endswith("/backgroundProcess/843/status"):
            if self.status_http != 200:
                return httpx.Response(self.status_http, json={})
            status = (
                self.job_statuses.pop(0) if len(self.job_statuses) > 1 else self.job_statuses[0]
            )
            return httpx.Response(200, json={"status": status, "process_id": 843})
        if path.endswith("/backgroundProcess/843/result"):
            if self.result_http != 200:
                return httpx.Response(self.result_http, json={})
            return httpx.Response(200, json={"record_id": self.record_id})
        if path.startswith("/accountspayable/v1/invoices/"):
            if self.invoice is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, content=json.dumps(self.invoice))
        rows = self.collections.get(path)
        if rows is not None:
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "100"))
            return httpx.Response(
                200, json={"count": len(rows), "value": rows[offset : offset + limit]}
            )
        return httpx.Response(599)

    def of(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _run(fake: FakeSky, coro_fn):
    def factory(*args, **kwargs):
        return _RealAsyncClient(*args, transport=httpx.MockTransport(fake.handler), **kwargs)

    with patch("httpx.AsyncClient", side_effect=factory):
        return asyncio.run(coro_fn())


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id=CORRELATION,
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 31),
        vendor_erp_id="136",
        gl_account_erp_id="01-5000-00",
        description="Office supplies",
        vendor_tax_id="12-3456789",
        vendor_address="1 Secret Lane",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _post(fake: FakeSky, config: dict | None = None, **overrides):
    adapter = BlackbaudFeNxtAdapter(config or CONFIG)
    return _run(fake, lambda: adapter.post_invoice(_payload(**overrides)))


def _create_json(fake: FakeSky) -> tuple[dict, str]:
    [req] = fake.of("POST", "/invoices/process")
    text = req.content.decode()
    return json.loads(text, parse_float=Decimal), text


# ---------------------------------------------------------------------------
# Registry, OAuth spec, config
# ---------------------------------------------------------------------------


def test_dispatcher_resolves_blackbaud():
    assert isinstance(get_erp_adapter(CONFIG), BlackbaudFeNxtAdapter)


def test_oauth_provider_is_registered_with_the_sky_endpoints():
    spec = erp_oauth.OAUTH_PROVIDERS["blackbaud_fe_nxt"]
    assert spec is BlackbaudFeNxtAdapter.oauth_provider
    assert spec.authorize_url == "https://app.blackbaud.com/oauth/authorize"
    assert spec.token_url == "https://oauth2.sky.blackbaud.com/token"
    assert spec.client_id_setting == "erp_blackbaud_client_id"
    assert spec.client_secret_setting == "erp_blackbaud_client_secret"
    assert hasattr(settings, spec.client_id_setting)
    assert hasattr(settings, spec.client_secret_setting)


def test_platform_credentials_default_empty():
    from app.config import Settings

    fields = Settings.model_fields
    for name in (
        "erp_blackbaud_client_id",
        "erp_blackbaud_client_secret",
        "erp_blackbaud_subscription_key",
        "erp_blackbaud_api_base",
        "erp_blackbaud_token_url",
    ):
        assert fields[name].default == ""


def test_every_call_carries_bearer_and_subscription_key():
    fake = FakeSky()
    result = _post(fake)
    assert result.success
    assert fake.requests
    for req in fake.requests:
        assert req.headers["authorization"] == "Bearer tok-abc"
        assert req.headers["bb-api-subscription-key"] == "platform-sub-key"
        assert str(req.url).startswith(DEFAULT_API_BASE + "/")


def test_tenant_subscription_key_overrides_the_platform_key():
    fake = FakeSky()
    _post(fake, {**CONFIG, "subscription_key": "tenant-key"})
    assert {r.headers["bb-api-subscription-key"] for r in fake.requests} == {"tenant-key"}


def test_missing_subscription_key_fails_closed_before_any_call(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_subscription_key", "")
    fake = FakeSky()
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message == "Blackbaud FE NXT post refused: subscription_key_missing"
    assert fake.requests == []
    assert _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).test_connection()) is False
    assert _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_vendors()) == []
    with pytest.raises(bb.BlackbaudConfigError, match="subscription key"):
        _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).get_invoice_status("1"))
    assert fake.requests == []


def test_operator_override_base_is_used(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_api_base", "http://localhost:12112/blackbaud/")
    fake = FakeSky()
    _post(fake)
    assert all(str(r.url).startswith("http://localhost:12112/blackbaud/") for r in fake.requests)


# ---------------------------------------------------------------------------
# Body shape
# ---------------------------------------------------------------------------


def test_invoice_body_is_balanced_distributions_with_exact_money():
    fake = FakeSky()
    amount = Decimal("99999999999999.99")
    result = _post(fake, amount=amount)
    assert result.success and result.erp_document_id == "4975"
    body, text = _create_json(fake)
    assert body["vendor_id"] == 136
    assert body["invoice_number"] == "INV-1"
    assert body["invoice_date"] == "2026-01-01"
    assert body["due_date"] == "2026-01-31"
    assert body["post_date"] == "2026-01-01"
    assert body["payment_details"] == {}
    assert body["description"].startswith(MARKER)
    assert "approval_status" not in body
    split = {"percent": Decimal("100"), "transaction_code_values": []}
    assert body["distributions"] == [
        {
            "account_number": "01-5000-00",
            "amount": amount,
            "description": "Office supplies",
            "type_code": "Debit",
            "distribution_splits": [split],
        },
        {
            "account_number": "01-2000-00",
            "amount": amount,
            "description": "INV-1",
            "type_code": "Credit",
            "distribution_splits": [split],
        },
    ]
    assert text.count('"amount":99999999999999.99') == 3
    # PII the payload carries never reaches the wire.
    assert "12-3456789" not in text and "Secret Lane" not in text


def test_tiny_amount_is_never_exponent_notation():
    fake = FakeSky()
    _post(fake, amount=Decimal("0.00001"))
    _, text = _create_json(fake)
    assert text.count('"amount":0.00001') == 3
    assert "1e-05" not in text.lower()


def test_per_line_debits_and_a_negative_line_flips_side():
    fake = FakeSky()
    lines = [
        LineItemPayload(line_number=1, total=Decimal("120.00"), gl_account_erp_id="01-5000-00"),
        LineItemPayload(
            line_number=2,
            total=Decimal("-20.00"),
            gl_account_erp_id="01-5100-00",
            description="Discount",
        ),
    ]
    _post(fake, line_items=lines)
    body, _ = _create_json(fake)
    sides = [(d["account_number"], d["amount"], d["type_code"]) for d in body["distributions"]]
    assert sides == [
        ("01-5000-00", 120, "Debit"),
        ("01-5100-00", 20, "Credit"),
        ("01-2000-00", 100, "Credit"),
    ]


def test_configured_project_codes_and_approval_are_sent():
    fake = FakeSky()
    tcv = [{"id": 1, "value": "None"}, {"id": 2, "value": "Spendable"}]
    _post(
        fake,
        {
            **CONFIG,
            "project_id": "UNREST",
            "transaction_code_values": tcv,
            "approval_status": "Approved",
        },
    )
    body, _ = _create_json(fake)
    assert body["approval_status"] == "Approved"
    for dist in body["distributions"]:
        assert dist["distribution_splits"] == [
            {"percent": 100, "transaction_code_values": tcv, "ui_project_id": "UNREST"}
        ]


# ---------------------------------------------------------------------------
# Pre-flight refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config_overrides", "payload_overrides", "reason"),
    [
        ({}, {"vendor_erp_id": None}, "vendor_not_linked"),
        ({}, {"vendor_erp_id": "V-ACME"}, "vendor_not_linked"),
        ({}, {"gl_account_erp_id": None}, "account_not_linked"),
        ({"ap_account_number": ""}, {}, "ap_account_not_configured"),
        ({"currency": ""}, {}, "currency_not_configured"),
        ({}, {"currency": "EUR"}, "currency_mismatch"),
        ({}, {"invoice_date": None}, "invoice_date_missing"),
        ({}, {"due_date": None}, "due_date_missing"),
        ({}, {"amount": Decimal("0")}, "amount_not_positive"),
    ],
)
def test_preflight_refusals_make_no_call(config_overrides, payload_overrides, reason):
    fake = FakeSky()
    result = _post(fake, {**CONFIG, **config_overrides}, **payload_overrides)
    assert not result.success and not result.retryable
    assert result.message == f"Blackbaud FE NXT post refused: {reason}"
    assert fake.requests == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_lookup_searches_by_invoice_number_before_creating():
    fake = FakeSky()
    _post(fake)
    first = fake.requests[0]
    assert first.method == "GET" and first.url.path == "/accountspayable/v1/invoices"
    assert first.url.params["search_text"] == "INV-1"


def test_earlier_attempt_with_our_marker_is_returned_without_posting():
    fake = FakeSky()
    fake.invoices = [
        {
            "invoice_id": 777,
            "vendor_id": 136,
            "invoice_number": "INV-1",
            "description": f"{MARKER} Office supplies",
            "status": "Pending",
        }
    ]
    result = _post(fake)
    assert result.success and result.erp_document_id == "777"
    assert fake.of("POST", "/invoices/process") == []


def test_same_vendor_and_number_without_marker_is_refused_as_duplicate():
    fake = FakeSky()
    fake.invoices = [
        {
            "invoice_id": 12,
            "vendor_id": 136,
            "invoice_number": "INV-1",
            "description": "keyed by hand",
            "status": "Approved",
        }
    ]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.message == "Blackbaud FE NXT post refused: duplicate_invoice_number"
    assert fake.of("POST", "/invoices/process") == []


def test_deleted_or_other_vendor_rows_do_not_count():
    fake = FakeSky()
    fake.invoices = [
        {"invoice_id": 1, "vendor_id": 136, "invoice_number": "INV-1", "status": "Deleted"},
        {"invoice_id": 2, "vendor_id": 999, "invoice_number": "INV-1", "status": "Approved"},
    ]
    assert _post(fake).success
    assert len(fake.of("POST", "/invoices/process")) == 1


def test_failed_lookup_is_a_failure_not_a_miss():
    fake = FakeSky()
    fake.list_status = 503
    result = _post(fake)
    assert not result.success and result.retryable
    assert result.message == (
        "Blackbaud FE NXT idempotency lookup failed: HTTP 503 (provider_error)"
    )
    assert fake.of("POST", "/invoices/process") == []


# ---------------------------------------------------------------------------
# Background job + rate limits
# ---------------------------------------------------------------------------


def test_job_is_polled_until_completed():
    fake = FakeSky()
    fake.job_statuses = [0, 3, 5]
    result = _post(fake)
    assert result.success and result.erp_document_id == "4975"
    assert len(fake.of("GET", "/status")) == 3
    assert len(fake.of("GET", "/result")) == 1


def test_job_still_running_is_unconfirmed_and_not_retried():
    fake = FakeSky()
    fake.job_statuses = [3]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message
    assert len(fake.of("GET", "/status")) == bb.PROCESS_POLL_ATTEMPTS


def test_job_failed_is_retryable():
    fake = FakeSky()
    fake.job_statuses = [7]
    result = _post(fake)
    assert not result.success and result.retryable
    assert result.message == "Blackbaud FE NXT post failed: background job status 7"


def test_rate_limited_create_maps_to_rate_limited_and_is_retryable():
    fake = FakeSky()
    fake.process_status = 429
    result = _post(fake)
    assert not result.success and result.retryable
    assert result.message == "Blackbaud FE NXT post failed: HTTP 429 (rate_limited)"


def test_quota_403_with_retry_after_is_rate_limited():
    fake = FakeSky()
    fake.list_status = 403
    fake.list_headers = {"Retry-After": "3600"}
    result = _post(fake)
    assert result.message.endswith("HTTP 403 (rate_limited)")
    fake = FakeSky()
    fake.list_status = 403
    assert _post(fake).message.endswith("HTTP 403 (forbidden)")


def test_rate_limited_status_poll_stops_at_once_without_sleeping(monkeypatch):
    slept: list[float] = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(bb.asyncio, "sleep", no_sleep)
    fake = FakeSky()
    fake.status_http = 429
    result = _post(fake)
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message and "rate_limited" in result.message
    assert len(fake.of("GET", "/status")) == 1
    assert slept == []


def test_failure_message_never_echoes_the_response_body():
    fake = FakeSky()
    fake.process_status = 400
    result = _post(fake)
    assert result.message == "Blackbaud FE NXT post failed: HTTP 400 (invalid_request)"
    assert "Acme" not in result.message and "12-3456789" not in result.message
    assert result.raw_response is None


# ---------------------------------------------------------------------------
# Status, void, syncs, connection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": "Pending", "amount": 10, "balance": 10}, ErpInvoiceStatus.draft),
        ({"status": "Approved", "amount": 10, "balance": 10}, ErpInvoiceStatus.open),
        ({"status": "Approved", "amount": 10, "balance": 0}, ErpInvoiceStatus.paid),
        ({"status": "PartiallyPaid", "amount": 10, "balance": 4}, ErpInvoiceStatus.partially_paid),
        ({"status": "Paid", "amount": 10, "balance": 0}, ErpInvoiceStatus.paid),
        ({"status": "Deleted"}, ErpInvoiceStatus.cancelled),
        ({"status": "Weird"}, ErpInvoiceStatus.unknown),
    ],
)
def test_status_mapping(record, expected):
    assert map_invoice_status(record) is expected


def test_get_invoice_status_reads_the_invoice():
    fake = FakeSky()
    fake.invoice = {"invoice_id": 4975, "status": "PartiallyPaid", "amount": 10, "balance": 4}
    adapter = BlackbaudFeNxtAdapter(CONFIG)
    assert _run(fake, lambda: adapter.get_invoice_status("4975")) is ErpInvoiceStatus.partially_paid
    assert fake.requests[0].url.path == "/accountspayable/v1/invoices/4975"
    fake.invoice = None
    assert _run(fake, lambda: adapter.get_invoice_status("4975")) is ErpInvoiceStatus.unknown


def test_void_is_not_automated():
    fake = FakeSky()
    assert _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).void_invoice("4975")) is False
    assert fake.requests == []


def test_list_vendors_pages_and_maps():
    fake = FakeSky()
    fake.collections["/accountspayable/v1/vendors"] = [
        {
            "vendor_id": i,
            "vendor_name": f"Vendor {i}",
            "ui_defined_id": f"V{i}",
            "payment_defaults": {"payment_terms": "Net 30"},
        }
        for i in range(1, 151)
    ]
    vendors = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_vendors())
    assert len(vendors) == 150
    assert vendors[0].erp_vendor_id == "1" and vendors[0].code == "V1"
    assert vendors[0].payment_terms == "Net 30"
    assert [r.url.params["offset"] for r in fake.requests] == ["0", "100"]


def test_list_gl_accounts_keys_by_number_and_skips_locked_accounts():
    fake = FakeSky()
    fake.collections["/generalledger/v1/accounts"] = [
        {"account_id": 1, "account_number": "01-5000-00", "description": "Supplies"},
        {"account_id": 2, "account_number": "01-1000-00", "prevent_data_entry": True},
    ]
    accounts = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_gl_accounts())
    assert [(a.code, a.erp_account_id, a.name, a.account_type) for a in accounts] == [
        ("01-5000-00", "01-5000-00", "Supplies", None)
    ]


def test_list_pos_maps_status_and_never_labels_currency():
    fake = FakeSky()
    fake.collections["/accountspayable/v1/purchaseorders"] = [
        {
            "order_number": 11,
            "vendor_name": "A",
            "order_total": 1250.5,
            "order_status": "OpenPurchaseOrder",
        },
        {"order_number": 12, "order_total": 10, "order_status": "ClosedOrder"},
        {"order_number": 13, "order_total": 10, "order_status": "CanceledOrder"},
        {"order_number": 14, "order_total": 10, "order_status": "DeletedOrder"},
        {"order_number": 15, "order_total": 10, "type": "Template"},
    ]
    pos = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos())
    assert [(p.po_number, p.status) for p in pos] == [
        ("11", "open"),
        ("12", "closed"),
        ("13", "cancelled"),
    ]
    assert pos[0].total == Decimal("1250.5") and all(p.currency is None for p in pos)


def test_test_connection_needs_a_captured_environment():
    fake = FakeSky()
    fake.collections["/accountspayable/v1/vendors"] = []
    assert _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).test_connection()) is True
    no_env = {k: v for k, v in CONFIG.items() if k != "oauth"}
    assert _run(fake, lambda: BlackbaudFeNxtAdapter(no_env).test_connection()) is False


def test_not_connected_propagates_from_access_token(monkeypatch):
    async def not_connected(self):
        raise erp_oauth.ErpNotConnectedError("blackbaud_fe_nxt")

    monkeypatch.setattr(BlackbaudFeNxtAdapter, "access_token", not_connected)
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        _post(FakeSky())


def test_oauth_spec_reads_the_environment_and_a_call_time_token_override():
    from app.services.erp_adapters.blackbaud_fe_nxt import BLACKBAUD_OAUTH

    assert BLACKBAUD_OAUTH.external_tenant_id_token_field == "environment_id"
    assert BLACKBAUD_OAUTH.token_url_setting == "erp_blackbaud_token_url"
    tenant = asyncio.run(
        BlackbaudFeNxtAdapter.resolve_external_tenant_id(
            access_token="t", token_response={"environment_id": "p-env-1"}, callback_params={}
        )
    )
    assert tenant == "p-env-1"
