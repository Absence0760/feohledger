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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services import erp_call_budget, erp_oauth
from app.services.erp_adapters import blackbaud_fe_nxt as bb
from app.services.erp_adapters.base import (
    SYNC_LISTING_MAX_ROWS,
    ErpCallBudgetExhausted,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
    ListingGap,
    PoSkipReason,
    SkippedPo,
)
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
    monkeypatch.setattr(bb, "PROCESS_POLL_DELAYS_SECONDS", (0, 0, 0, 0))
    monkeypatch.setattr(bb, "_FISCAL_YEAR_CACHE", {})

    async def fake_token(self):
        return "tok-abc"

    monkeypatch.setattr(BlackbaudFeNxtAdapter, "access_token", fake_token)


@pytest.fixture(autouse=True)
def quota_notices(monkeypatch):
    """The call budget's admin notices, recorded instead of delivered (no DB)."""
    sent: list[tuple[str, str]] = []

    async def deliver(org_id, *, notice, **_kwargs):
        sent.append((str(org_id), notice.value))
        return True

    monkeypatch.setattr(erp_call_budget, "deliver_quota_notice", deliver)
    return sent


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
        # A collection page at this offset (or later) answers 503.
        self.collection_fail_offset: int | None = None
        # A transport error class raised instead of answering (timeout, reset).
        self.process_exc: type[httpx.TransportError] | None = None
        self.status_exc: type[httpx.TransportError] | None = None
        # GET /generalledger/v1/fiscalyears: the years, or an HTTP status.
        self.fiscal_years: list[dict] = []
        self.fiscal_http = 200
        self.fiscal_headers: dict[str, str] = {}
        # GET /accountspayable/v1/vendors/{id}: the VendorDetail, or 404.
        self.vendor: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/accountspayable/v1/invoices" and request.method == "GET":
            if self.list_status != 200:
                return httpx.Response(self.list_status, headers=self.list_headers, json={})
            # ListInvoices: limit (<= 500) / offset, and from_date / to_date
            # read as strictly after / before (the narrower reading).
            params = request.url.params
            rows = [
                r
                for r in self.invoices
                if (not params.get("from_date") or r.get("invoice_date", "") > params["from_date"])
                and (not params.get("to_date") or r.get("invoice_date", "") < params["to_date"])
            ]
            offset = int(params.get("offset", "0"))
            limit = min(int(params.get("limit", "100")), 500)
            return httpx.Response(
                200, json={"count": len(rows), "value": rows[offset : offset + limit]}
            )
        if path == "/generalledger/v1/fiscalyears":
            if self.fiscal_http != 200:
                return httpx.Response(self.fiscal_http, headers=self.fiscal_headers, json={})
            return httpx.Response(
                200, json={"count": len(self.fiscal_years), "value": self.fiscal_years}
            )
        if path.startswith("/accountspayable/v1/vendors/"):
            if self.vendor is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=self.vendor)
        if path == "/accountspayable/v1/invoices/process":
            if self.process_exc is not None:
                raise self.process_exc("transport failure", request=request)
            if self.process_status != 200:
                return httpx.Response(
                    self.process_status,
                    json={"Error": "Failed to save", "Details": ["Acme Ltd 12-3456789 rejected"]},
                )
            return httpx.Response(200, json={"process_id": 843})
        if path.endswith("/backgroundProcess/843/status"):
            if self.status_exc is not None:
                raise self.status_exc("transport failure", request=request)
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
            if self.collection_fail_offset is not None and offset >= self.collection_fail_offset:
                return httpx.Response(503, json={})
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
    # A listing that cannot ask is a failure, never "no vendors" (§284).
    with pytest.raises(bb.BlackbaudConfigError, match="subscription key"):
        _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_vendors())
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
    assert result.not_posted is True


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_lookup_searches_by_invoice_number_before_creating():
    fake = FakeSky()
    _post(fake)
    first = fake.requests[0]
    assert first.method == "GET" and first.url.path == "/accountspayable/v1/invoices"
    assert first.url.params["search_text"] == "INV-1"
    # ListInvoices' maximum page, so a busy number costs one call, not five.
    assert first.url.params["limit"] == "500"
    assert "from_date" not in first.url.params


def _busy(n: int, invoice_date: str = "2025-06-01") -> list[dict]:
    """``n`` other invoices ``search_text`` also matches (it matches vendor
    name, description and PO number too), dated away from ours."""
    return [
        {
            "invoice_id": 10_000 + i,
            "vendor_id": 137,
            "invoice_number": f"X-{i}",
            "description": "re INV-1",
            "invoice_date": invoice_date,
            "status": "Approved",
        }
        for i in range(n)
    ]


def test_a_busy_number_is_narrowed_to_the_invoice_date_not_failed():
    """The regression: the search walked 5 pages of 100 and, past them,
    reported "result set truncated" — retryably, so every push of a common
    number failed the same way until the backoff ran out, forever."""
    fake = FakeSky()
    ours = {
        "invoice_id": 777,
        "vendor_id": 136,
        "invoice_number": "INV-1",
        "description": f"{MARKER} Office supplies",
        "invoice_date": "2026-01-01",
        "status": "Pending",
    }
    fake.invoices = [*_busy(600), ours]
    result = _post(fake)
    assert result.success and result.erp_document_id == "777"
    lookups = fake.of("GET", "/accountspayable/v1/invoices")
    assert len(lookups) == 2
    assert lookups[1].url.params["from_date"] == "2025-12-31"
    assert lookups[1].url.params["to_date"] == "2026-01-02"
    assert lookups[1].url.params["search_text"] == "INV-1"
    assert fake.of("POST", "/invoices/process") == []

    # Nothing of ours on the date: absent, so it posts.
    fake = FakeSky()
    fake.invoices = _busy(600)
    assert _post(fake).success
    assert len(fake.of("POST", "/invoices/process")) == 1


def test_a_retry_after_the_date_was_edited_still_finds_the_earlier_bill():
    """Attempt 1 posted at 2026-01-01 and its outcome was lost; the invoice date
    was then edited to 2026-01-11. The number is busy, so only the narrowed
    search can see attempt 1's bill — keyed on the CURRENT date alone it looked
    at 01-10..01-12, missed the bill and posted a second one (decisions §322).
    ``services/erp`` hands back the date attempt 1 was sent with."""
    fake = FakeSky()
    ours = {
        "invoice_id": 777,
        "vendor_id": 136,
        "invoice_number": "INV-1",
        "description": f"{MARKER} Office supplies",
        "invoice_date": "2026-01-01",
        "status": "Pending",
    }
    fake.invoices = [*_busy(600), ours]
    result = _post(
        fake,
        invoice_date=date(2026, 1, 11),
        due_date=date(2026, 2, 10),
        prior_invoice_dates=[date(2026, 1, 1)],
    )
    assert result.success and result.erp_document_id == "777"
    assert fake.of("POST", "/invoices/process") == []
    windows = [
        (r.url.params["from_date"], r.url.params["to_date"])
        for r in fake.of("GET", "/accountspayable/v1/invoices")
        if "from_date" in r.url.params
    ]
    assert windows == [("2026-01-10", "2026-01-12"), ("2025-12-31", "2026-01-02")]


def test_a_lookup_still_truncated_after_narrowing_is_a_coded_non_retryable_refusal():
    fake = FakeSky()
    fake.invoices = _busy(600, "2025-06-01") + _busy(2100, "2026-01-01")
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "idempotency_lookup_incomplete"
    assert result.message == "Blackbaud FE NXT post refused: idempotency_lookup_incomplete"
    assert fake.of("POST", "/invoices/process") == []


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
    assert len(fake.of("GET", "/status")) == len(bb.PROCESS_POLL_DELAYS_SECONDS) + 1


def test_status_poll_backs_off_between_reads(monkeypatch):
    """One subscription's call quota is shared by every tenant on the
    platform key: the poll doubles its pause instead of reading every second."""
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(bb, "PROCESS_POLL_DELAYS_SECONDS", (1.0, 2.0, 4.0, 8.0))
    monkeypatch.setattr(bb.asyncio, "sleep", record)
    fake = FakeSky()
    fake.job_statuses = [3]
    _post(fake)
    assert slept == [1.0, 2.0, 4.0, 8.0]
    assert len(fake.of("GET", "/status")) == 5


def test_unconfirmed_job_carries_its_process_id_for_the_next_attempt():
    fake = FakeSky()
    fake.job_statuses = [3]
    result = _post(fake)
    assert result.pending_job_id == "843"
    assert result.raw_response == {"process_id": "843"}


def test_retry_while_the_earlier_job_still_runs_never_queues_a_second_invoice():
    """The regression: a manual retry after `job_unconfirmed` used to go
    straight to the lookup, miss the invoice the still-running job had not yet
    made, and queue a second one."""
    fake = FakeSky()
    fake.job_statuses = [3]
    result = _post(fake, pending_job_id="843")
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message
    assert result.pending_job_id == "843"
    assert fake.of("POST", "/invoices/process") == []
    assert fake.of("GET", "/accountspayable/v1/invoices") == []


def test_retry_after_the_earlier_job_completed_reports_its_invoice():
    fake = FakeSky()
    result = _post(fake, pending_job_id="843")
    assert result.success and result.erp_document_id == "4975"
    assert result.pending_job_id is None  # services/erp clears the stored job
    assert fake.of("POST", "/invoices/process") == []


def test_retry_after_the_earlier_job_failed_posts_again():
    fake = FakeSky()
    fake.job_statuses = [7, 5]
    result = _post(fake, pending_job_id="843")
    assert result.success, result.message
    assert len(fake.of("GET", "/accountspayable/v1/invoices")) == 1  # lookup still guards it
    assert len(fake.of("POST", "/invoices/process")) == 1


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.ConnectError])
def test_create_transport_error_is_unconfirmed_and_the_retry_looks_up_first(exc):
    """The regression: a timeout on `POST /invoices/process` used to escape as
    a generic error, so services/erp re-sent two seconds later — while the job
    FE NXT had already queued was still running, so the lookup missed and a
    second invoice was queued. No process id came back, so there is no job to
    carry; the result is non-retryable and the operator's retry runs the
    correlation-marker lookup before posting."""
    fake = FakeSky()
    fake.process_exc = exc
    result = _post(fake)
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message
    assert exc.__name__ in result.message
    assert "transport failure" not in result.message
    assert result.pending_job_id is None and result.raw_response is None
    assert len(fake.of("POST", "/invoices/process")) == 1

    # The next attempt: the job FE NXT queued has since made the invoice.
    fake.process_exc = None
    fake.invoices = [
        {
            "invoice_id": 4975,
            "vendor_id": 136,
            "invoice_number": "INV-1",
            "description": f"{MARKER} Office supplies",
            "status": "Pending",
        }
    ]
    retry = _post(fake)
    assert retry.success and retry.erp_document_id == "4975"
    assert len(fake.of("POST", "/invoices/process")) == 1  # never a second create


def test_garbled_create_body_is_unconfirmed_not_retryable():
    fake = FakeSky()
    original = fake.handler

    def handler(request):
        if request.url.path.endswith("/invoices/process"):
            fake.requests.append(request)
            return httpx.Response(200, content=b"<html>gateway</html>")
        return original(request)

    fake.handler = handler
    result = _post(fake)
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message and result.pending_job_id is None


def test_status_poll_transport_error_keeps_the_process_id():
    fake = FakeSky()
    fake.status_exc = httpx.ReadTimeout
    result = _post(fake)
    assert not result.success and not result.retryable
    assert "job_unconfirmed" in result.message and "ReadTimeout" in result.message
    assert result.pending_job_id == "843"
    assert len(fake.of("GET", "/status")) == 1  # stops at once, no further reads

    # The next attempt, handed the job by services/erp, reads it and never posts.
    fake.status_exc = None
    retry = _post(fake, pending_job_id="843")
    assert retry.success and retry.erp_document_id == "4975"
    assert len(fake.of("POST", "/invoices/process")) == 1


def test_pending_job_read_transport_error_is_unconfirmed_and_keeps_the_job():
    fake = FakeSky()
    fake.status_exc = httpx.ConnectError
    result = _post(fake, pending_job_id="843")
    assert not result.success and not result.retryable
    assert result.pending_job_id == "843"
    assert fake.of("POST", "/invoices/process") == []
    assert fake.of("GET", "/accountspayable/v1/invoices") == []


def test_job_failed_is_a_coded_non_retryable_refusal():
    """The regression: status 7 came back retryable and uncoded, so
    services/erp re-sent a job that failed the same way until its backoff was
    spent, and the user saw English with no code to act on."""
    fake = FakeSky()
    fake.job_statuses = [7]
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "job_failed"
    assert result.message == "Blackbaud FE NXT post refused: job_failed"
    assert result.pending_job_id is None  # the job is over: services/erp clears it
    assert len(fake.of("POST", "/invoices/process")) == 1


def test_job_canceled_stays_retryable():
    fake = FakeSky()
    fake.job_statuses = [6]
    result = _post(fake)
    assert not result.success and result.retryable and result.refusal_code is None
    assert result.message == "Blackbaud FE NXT post failed: background job status 6"


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


def test_403_without_retry_after_is_a_permission_refusal():
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


# ---------------------------------------------------------------------------
# Closed fiscal periods (pre-create) and inactive vendors (on a 400)
# ---------------------------------------------------------------------------


def _year(year: int, *, status: str = "Open", closed_months: tuple[int, ...] = ()) -> dict:
    return {
        "fiscal_year_id": year,
        "year": str(year),
        "start_date": f"{year}-01-01T00:00:00",
        "end_date": f"{year}-12-31T00:00:00",
        "status": status,
        "fiscal_periods": [
            {
                "fiscal_period_id": year * 100 + m,
                "sequence": m,
                "start_date": f"{year}-{m:02d}-01T00:00:00",
                "end_date": f"{year}-{m:02d}-28T00:00:00",
                "closed": m in closed_months,
            }
            for m in range(1, 13)
        ],
    }


@pytest.mark.parametrize(
    "years",
    [
        [_year(2026, closed_months=(1,))],  # the period is closed
        [_year(2026, status="Closed")],  # the whole year is
        [_year(2026, status="Purged")],
    ],
)
def test_a_post_date_in_a_closed_period_is_refused_before_the_create(years):
    """The regression: nothing checked, and FE NXT's refusal came back as an
    uncoded 400 (`invalid_request`) nobody could act on."""
    fake = FakeSky()
    fake.fiscal_years = years
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "period_closed"
    assert fake.of("POST", "/invoices/process") == []
    [read] = fake.of("GET", "/generalledger/v1/fiscalyears")
    # The lookup ran first: an invoice an earlier attempt posted is reported.
    assert fake.requests.index(read) > 0


def test_an_open_period_or_an_uncovered_date_posts():
    fake = FakeSky()
    fake.fiscal_years = [_year(2026, closed_months=(2, 3)), _year(2025, status="Closed")]
    assert _post(fake).success
    fake = FakeSky()
    fake.fiscal_years = [_year(2030, status="Closed")]  # no year covers the date
    assert _post(fake).success


def test_an_earlier_attempt_is_found_even_when_the_period_has_since_closed():
    fake = FakeSky()
    fake.fiscal_years = [_year(2026, closed_months=(1,))]
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


@pytest.mark.parametrize(("http", "headers"), [(403, {}), (403, {"Retry-After": "60"}), (503, {})])
def test_unreadable_fiscal_years_mean_cannot_check_not_open_or_closed(http, headers):
    """Some sandboxes refuse the GL fiscal-year read. That is "cannot check":
    the create goes ahead and FE NXT judges the date itself."""
    fake = FakeSky()
    fake.fiscal_http = http
    fake.fiscal_headers = headers
    assert _post(fake).success


def test_fiscal_years_are_read_once_per_environment_not_per_push():
    fake = FakeSky()
    fake.fiscal_years = [_year(2026)]
    _post(fake)
    _post(fake)
    assert len(fake.of("GET", "/generalledger/v1/fiscalyears")) == 1
    # A permission refusal is remembered too, so it is not re-asked every push.
    fake = FakeSky()
    fake.fiscal_http = 403
    other = {**CONFIG, "oauth": {"external_tenant_id": "p-env-other"}}
    _post(fake, other)
    _post(fake, other)
    assert len(fake.of("GET", "/generalledger/v1/fiscalyears")) == 1


def test_a_400_for_an_inactive_vendor_is_coded_vendor_inactive():
    """The regression: an Inactive FE NXT vendor's push died as an uncoded
    400. The body is never parsed; the vendor is read back instead."""
    fake = FakeSky()
    fake.process_status = 400
    fake.vendor = {"vendor_id": 136, "vendor_status": "Inactive"}
    result = _post(fake)
    assert not result.success and not result.retryable
    assert result.refusal_code == "vendor_inactive"
    [read] = fake.of("GET", "/accountspayable/v1/vendors/136")
    assert read.headers["bb-api-subscription-key"] == "platform-sub-key"


@pytest.mark.parametrize("vendor", [None, {"vendor_id": 136, "vendor_status": "Active"}])
def test_a_400_for_an_active_or_unreadable_vendor_stays_invalid_request(vendor):
    fake = FakeSky()
    fake.process_status = 400
    fake.vendor = vendor
    result = _post(fake)
    assert result.message == "Blackbaud FE NXT post failed: HTTP 400 (invalid_request)"
    assert not result.retryable and result.refusal_code is None


def test_a_rate_limited_create_does_not_read_the_vendor():
    fake = FakeSky()
    fake.process_status = 429
    _post(fake)
    assert fake.of("GET", "/accountspayable/v1/vendors/136") == []


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
        # A zero balance under "Approved" is not a payment until FE NXT says
        # "Paid": a credit zeroes it too (decisions §303).
        ({"status": "Approved", "amount": 10, "balance": 0}, ErpInvoiceStatus.open),
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


def test_inactive_and_on_hold_vendors_are_emitted_inactive():
    """The regression: every vendor synced active, so an Inactive FE NXT
    vendor got a payee (its push then died as an uncoded 400) and an OnHold
    one could be paid from FeohLedger around FE NXT's hold."""
    fake = FakeSky()
    fake.collections["/accountspayable/v1/vendors"] = [
        {"vendor_id": 1, "vendor_name": "A", "vendor_status": "Active"},
        {"vendor_id": 2, "vendor_name": "I", "vendor_status": "Inactive"},
        {"vendor_id": 3, "vendor_name": "H", "vendor_status": "OnHold"},
        {"vendor_id": 4, "vendor_name": "N"},  # no status: not refused on a guess
    ]
    vendors = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_vendors())
    assert [(v.erp_vendor_id, v.active) for v in vendors] == [
        ("1", True),
        ("2", False),
        ("3", False),
        ("4", True),
    ]


def test_list_gl_accounts_keys_by_number_and_emits_locked_accounts_inactive():
    """A locked account still exists: dropping it made a row linked to it look
    like a dead link, which a sync re-judging links unlinks (§284)."""
    fake = FakeSky()
    fake.collections["/generalledger/v1/accounts"] = [
        {"account_id": 1, "account_number": "01-5000-00", "description": "Supplies"},
        {"account_id": 2, "account_number": "01-1000-00", "prevent_data_entry": True},
    ]
    accounts = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_gl_accounts())
    assert [(a.code, a.erp_account_id, a.name, a.account_type, a.active) for a in accounts] == [
        ("01-5000-00", "01-5000-00", "Supplies", None, True),
        ("01-1000-00", "01-1000-00", "01-1000-00", None, False),
    ]


@pytest.mark.parametrize(
    "collection, listing",
    [
        ("/generalledger/v1/accounts", "list_gl_accounts"),
        ("/accountspayable/v1/vendors", "list_vendors"),
    ],
)
def test_chart_and_vendor_listings_raise_rather_than_return_part(collection, listing):
    """A failed later page used to end the walk with the rows already read,
    and the first page failing returned []. The sync took either for the
    whole collection and unlinked every link it did not contain (§284)."""
    fake = FakeSky()
    fake.collections[collection] = [
        {"account_number": f"01-{i:04d}", "vendor_id": i, "vendor_name": f"V{i}"}
        for i in range(150)
    ]
    fake.collection_fail_offset = 100
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, lambda: getattr(BlackbaudFeNxtAdapter(CONFIG), listing)())
    assert exc.value.gap is ListingGap.fetch_failed

    fake.collection_fail_offset = 0
    with pytest.raises(ErpListingIncomplete):
        _run(fake, lambda: getattr(BlackbaudFeNxtAdapter(CONFIG), listing)())

    # Past the sync bound: truncated, never the first N rows.
    fake.collection_fail_offset = None
    fake.collections[collection] = [
        {"account_number": f"01-{i:05d}", "vendor_id": i, "vendor_name": f"V{i}"}
        for i in range(SYNC_LISTING_MAX_ROWS + 1)
    ]
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, lambda: getattr(BlackbaudFeNxtAdapter(CONFIG), listing)())
    assert exc.value.gap is ListingGap.truncated


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
    # A deleted order comes through cancelled, so a row an earlier sync stored
    # stops counting as an open commitment.
    assert [(p.po_number, p.status) for p in pos] == [
        ("11", "open"),
        ("12", "closed"),
        ("13", "cancelled"),
        ("14", "cancelled"),
    ]
    assert pos[0].total == Decimal("1250.5") and all(p.currency is None for p in pos)


def test_list_pos_links_the_vendor_by_its_fe_id():
    """#652: the PO named its vendor only by `vendor_name`, which a rename or
    a shared name mislinks. `PurchaseOrderSummary.vendor_id` is the id
    list_vendors reports as erp_vendor_id."""
    fake = FakeSky()
    fake.collections["/accountspayable/v1/purchaseorders"] = [
        {"order_number": 11, "vendor_id": 136, "vendor_name": "A", "order_total": 5},
        {"order_number": 12, "vendor_name": "B", "order_total": 5},
    ]
    fake.collections["/accountspayable/v1/vendors"] = [{"vendor_id": 136, "vendor_name": "A"}]
    adapter = BlackbaudFeNxtAdapter(CONFIG)
    pos = _run(fake, lambda: adapter.list_pos())
    [vendor] = _run(fake, lambda: adapter.list_vendors())
    assert pos[0].vendor_erp_id == vendor.erp_vendor_id == "136"
    assert pos[1].vendor_erp_id is None


def test_list_pos_skips_a_po_with_no_total_rather_than_syncing_zero():
    fake = FakeSky()
    fake.collections["/accountspayable/v1/purchaseorders"] = [
        {"order_number": 11, "vendor_id": 136, "order_total": 0},
        {"order_number": 12, "vendor_id": 136},
        {"order_number": 13, "vendor_id": 136, "order_total": None},
    ]
    pos = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos())
    assert [(p.po_number, p.total) for p in pos] == [("11", Decimal("0"))]


def test_list_pos_report_names_every_po_it_left_out_for_want_of_a_total():
    """§323: a PO synced at a total and later listed without one kept that
    total in the 3-way match, because the skip was silent. Each one the
    adapter drops for want of a total is now named, so the sync marks it.
    A template or a deleted order is not a PO the match may use, and is not
    reported."""
    fake = FakeSky()
    fake.collections["/accountspayable/v1/purchaseorders"] = [
        {"order_number": 11, "order_total": 40},
        {"order_number": 12},
        {"order_number": 13, "order_total": None},
        {"order_number": 14, "order_total": "not a number"},
        {"order_number": 15, "order_total": "NaN"},
        {"order_number": 16, "order_status": "DeletedOrder"},
        {"order_number": 17, "type": "Template"},
    ]
    listing = _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos_report())
    assert [p.po_number for p in listing.pos] == ["11"]
    assert listing.skipped == [
        SkippedPo(number, PoSkipReason.unpriceable) for number in ("12", "13", "14", "15")
    ]


def test_a_po_listing_that_cannot_be_read_whole_raises_rather_than_finding_none():
    """It used to swallow every failure into [], which the sync read as "the
    ERP has no POs" — and which dropped the skip report with it."""
    path = "/accountspayable/v1/purchaseorders"
    fake = FakeSky()
    fake.collections[path] = [{"order_number": i, "order_total": 1} for i in range(150)]
    fake.collection_fail_offset = 100
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos_report())
    assert exc.value.gap is ListingGap.fetch_failed

    fake.collection_fail_offset = 0
    with pytest.raises(ErpListingIncomplete):
        _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos())

    # Past the sync bound: truncated, never the first N rows (it stopped at
    # 1,000 rows and returned them as the whole list).
    fake.collection_fail_offset = None
    fake.collections[path] = [
        {"order_number": i, "order_total": 1} for i in range(SYNC_LISTING_MAX_ROWS + 1)
    ]
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos())
    assert exc.value.gap is ListingGap.truncated

    from app.api.refusals import erp_pull_failure

    http = erp_pull_failure(exc.value)
    assert http.status_code == 502 and http.detail["code"] == "erp_listing_incomplete"


@pytest.mark.parametrize("listing", ["list_pos", "list_vendors", "list_gl_accounts"])
def test_a_transport_error_on_a_listing_is_an_incomplete_listing(listing):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fake = FakeSky()
    fake.handler = boom
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, lambda: getattr(BlackbaudFeNxtAdapter(CONFIG), listing)())
    assert exc.value.gap is ListingGap.fetch_failed


def test_a_po_listing_without_a_connection_says_so(monkeypatch):
    """A lost connection is the sync's coded 409, not "no purchase orders"."""

    async def not_connected(self):
        raise erp_oauth.ErpNotConnectedError("blackbaud_fe_nxt")

    monkeypatch.setattr(BlackbaudFeNxtAdapter, "access_token", not_connected)
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        _run(FakeSky(), lambda: BlackbaudFeNxtAdapter(CONFIG).list_pos_report())


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


def test_oauth_endpoints_default_to_blackbaud_and_honour_the_overrides(monkeypatch):
    from app.services import erp_oauth as oauth
    from app.services.erp_adapters.blackbaud_fe_nxt import BLACKBAUD_OAUTH as spec

    monkeypatch.setattr(settings, "erp_blackbaud_authorize_url", "")
    monkeypatch.setattr(settings, "erp_blackbaud_token_url", "")
    assert oauth.authorize_endpoint(spec) == "https://app.blackbaud.com/oauth/authorize"
    assert oauth.token_endpoint(spec) == "https://oauth2.sky.blackbaud.com/token"

    # The operator override is what lets the connect flow run against
    # fake-erp locally (guard rail 7); each one moves only its own endpoint.
    fake = "http://localhost:12112/blackbaud/oauth2"
    monkeypatch.setattr(settings, "erp_blackbaud_authorize_url", f"{fake}/authorization")
    assert oauth.authorize_endpoint(spec) == f"{fake}/authorization"
    assert oauth.token_endpoint(spec) == "https://oauth2.sky.blackbaud.com/token"


# ---------------------------------------------------------------------------
# The SKY call budget (services/erp_call_budget, decisions §322)
# ---------------------------------------------------------------------------


def _org_config(org_id: str, **extra) -> dict:
    return {**CONFIG, **extra, "oauth": {"external_tenant_id": "p-env-123", "org_id": org_id}}


def _budget_refusal(result) -> dict:
    assert not result.success and result.not_posted
    assert result.refusal_code == "erp_call_budget_exhausted"
    assert result.refusal_provider == "Blackbaud FE NXT"
    return result.refusal_params


def test_a_quota_403_pauses_every_tenant_on_the_key_until_retry_after(quota_notices):
    fake = FakeSky()
    fake.list_status = 403
    fake.list_headers = {"Retry-After": "3600"}
    assert _post(fake, _org_config("org-a")).message.endswith("HTTP 403 (rate_limited)")
    assert quota_notices == [("org-a", "subscription_quota")]

    # Another tenant on the same platform key: refused before its first call,
    # until SKY's Retry-After, and not retried by services/erp's backoff.
    other = FakeSky()
    result = _post(other, _org_config("org-b"))
    params = _budget_refusal(result)
    assert params["reason"] == "subscription_quota" and not result.retryable
    assert other.requests == []
    # The admins of each org hear once a day, however often they are refused.
    _post(FakeSky(), _org_config("org-a"))
    assert quota_notices.count(("org-a", "subscription_quota")) == 1


def test_a_429_cools_the_key_down_briefly_and_stays_retryable(quota_notices):
    fake = FakeSky()
    fake.list_status = 429
    fake.list_headers = {"Retry-After": "30"}
    first = _post(fake)
    assert first.retryable and first.message.endswith("HTTP 429 (rate_limited)")

    second = FakeSky()
    result = _post(second)
    assert _budget_refusal(result)["reason"] == "rate_limited"
    assert result.retryable  # seconds, not a day: the backoff may outlast it
    assert second.requests == []
    assert quota_notices == []  # a rate limit is not worth an admin's attention

    adapter = BlackbaudFeNxtAdapter(CONFIG)
    later = datetime.now(UTC) + timedelta(seconds=31)
    assert asyncio.run(adapter.call_budget().check(now=later)) is None


def test_a_tenant_on_the_platform_key_is_held_to_its_daily_share(monkeypatch, quota_notices):
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 18)
    fake = FakeSky()
    assert _post(fake, _org_config("org-a")).success
    # A first push: lookup, fiscal years, create, status, result. The fiscal
    # years are then cached, so the next costs four.
    assert len(fake.requests) == 5
    assert bb.push_call_reserve(False) == 14
    # 5 spent + a 14-call reserve is more than 18: the next push never starts.
    third = FakeSky()
    result = _post(third, _org_config("org-a"))
    params = _budget_refusal(result)
    assert params["reason"] == "tenant_budget" and not result.retryable
    assert params["retry_after"].endswith("T00:00:00+00:00")  # SKY resets at 00:00 UTC
    assert third.requests == []
    assert ("org-a", "tenant_budget") in quota_notices
    # Another tenant's share is untouched.
    assert _post(FakeSky(), _org_config("org-b")).success


def test_the_share_warns_the_admins_at_80_percent_once(monkeypatch, quota_notices):
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 20)
    budget = BlackbaudFeNxtAdapter(_org_config("org-a")).call_budget()

    async def spend(n):
        for _ in range(n):
            await budget.record_call()

    asyncio.run(spend(15))
    assert quota_notices == []
    asyncio.run(spend(3))  # 16 of 20 is 80%
    assert quota_notices == [("org-a", "warning")]


def test_a_tenant_with_its_own_key_is_not_held_to_the_platform_share(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 1)
    own = _org_config("org-a", subscription_key="tenant-key")
    assert _post(FakeSky(), own).success
    assert _post(FakeSky(), own).success
    # ...while its platform-key neighbours are.
    assert not _post(FakeSky(), _org_config("org-b")).success


def test_the_whole_keys_quota_refuses_every_tenant(monkeypatch, quota_notices):
    monkeypatch.setattr(settings, "erp_blackbaud_daily_call_quota", 22)
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 0)
    assert _post(FakeSky(), _org_config("org-a")).success
    assert _post(FakeSky(), _org_config("org-b")).success  # 9 of 22 used
    result = _post(FakeSky(), _org_config("org-c"))  # 9 + a 14-call reserve > 22
    assert _budget_refusal(result)["reason"] == "subscription_quota"
    assert ("org-c", "subscription_quota") in quota_notices


def test_a_refused_push_keeps_the_earlier_attempts_job(monkeypatch):
    # 19 < the 20-call reserve of a push that reads an earlier job first.
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 19)
    fake = FakeSky()
    result = _post(fake, _org_config("org-a"), pending_job_id="843")
    assert _budget_refusal(result)["reason"] == "tenant_budget"
    assert result.pending_job_id == "843"
    assert fake.requests == []


def test_a_sync_is_refused_call_by_call_and_returns_nothing(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 3)
    fake = FakeSky()
    fake.collections["/accountspayable/v1/vendors"] = [
        {"vendor_id": i, "vendor_name": f"V{i}"} for i in range(450)
    ]
    adapter = BlackbaudFeNxtAdapter(_org_config("org-a"))
    with pytest.raises(ErpCallBudgetExhausted) as caught:
        _run(fake, adapter.list_vendors)
    assert caught.value.reason == "tenant_budget"
    assert len(fake.requests) == 3  # three pages paid for, the fourth never sent

    from app.api.refusals import erp_pull_failure

    http = erp_pull_failure(caught.value)
    assert http.status_code == 429 and int(http.headers["Retry-After"]) >= 1
    assert http.detail["code"] == "erp_call_budget_exhausted"
    assert http.detail["params"]["reason"] == "tenant_budget"


def test_a_status_read_under_a_cool_down_is_unknown_without_a_call():
    fake = FakeSky()
    fake.list_status = 429
    fake.list_headers = {"Retry-After": "60"}
    _post(fake)
    status_fake = FakeSky()
    status = _run(status_fake, lambda: BlackbaudFeNxtAdapter(CONFIG).get_invoice_status("4975"))
    assert status is ErpInvoiceStatus.unknown
    assert status_fake.requests == []


def test_redis_unreachable_fails_open(monkeypatch):
    async def down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(erp_call_budget, "get_redis", down)
    assert _post(FakeSky()).success


def test_retry_after_reads_seconds_and_http_dates_and_is_clamped():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def wait(value):
        headers = {"Retry-After": value} if value is not None else {}
        return erp_call_budget.retry_after_seconds(httpx.Response(429, headers=headers), now)

    assert wait("120") == 120
    assert wait("Mon, 26 Oct 2026 12:05:00 GMT") == 300
    assert wait(None) == 1
    assert wait("soon") == 1
    assert wait("999999") == 86_400
    assert erp_call_budget.next_reset(now) == datetime(2026, 10, 27, tzinfo=UTC)


def test_a_notice_nobody_received_is_tried_again(monkeypatch, quota_notices):
    attempts: list[str] = []

    async def deliver(org_id, *, notice, **_kwargs):
        attempts.append(notice.value)
        return len(attempts) > 1  # the first finds no admin to tell

    monkeypatch.setattr(erp_call_budget, "deliver_quota_notice", deliver)
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 1)
    for _ in range(3):
        _post(FakeSky(), _org_config("org-a"))
    assert attempts == ["tenant_budget", "tenant_budget"]


def test_a_po_sync_refused_by_the_budget_says_so_rather_than_finding_none(monkeypatch):
    monkeypatch.setattr(settings, "erp_blackbaud_tenant_daily_call_budget", 1)
    fake = FakeSky()
    fake.collections["/accountspayable/v1/purchaseorders"] = [
        {"order_number": i, "order_total": 1} for i in range(250)
    ]
    with pytest.raises(ErpCallBudgetExhausted):
        _run(fake, BlackbaudFeNxtAdapter(_org_config("org-a")).list_pos)
    assert len(fake.requests) == 1
