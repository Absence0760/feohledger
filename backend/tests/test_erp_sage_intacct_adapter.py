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
    SYNC_LISTING_MAX_ROWS,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    InvoicePayload,
    LineItemPayload,
    ListingGap,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.sage_intacct import (
    DEFAULT_API_BASE,
    SageIntacctAdapter,
    clear_token_cache,
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
        # A query page at this index (or later) answers 503.
        self.query_fail_from_page: int | None = None
        self.create_status = 201
        # Statuses the next creates answer with, before `create_status`.
        self.create_statuses: list[int] = []
        self.create_body: dict = {"ia::result": {"key": "777", "id": "777"}}
        self.bill: dict | None = None
        # A bill created while `bill` is None is read back as Intacct holds
        # it: the state the create asked for (else `draft`, the spec's
        # default) at the sum of its lines, unless these override it.
        self.read_back_state: str | None = None
        self.read_back_total: str | None = None
        self.delete_status = 204
        self.reverse_status = 200
        self.tokens_minted = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth2/token"):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            self.tokens_minted += 1
            return httpx.Response(200, json={"access_token": "tok-abc", "expires_in": 43200})
        if path.endswith("/services/core/query"):
            if self.query_status != 200:
                return httpx.Response(self.query_status, json={})
            body = json.loads(request.content)
            pages = self.query_rows.get(body["object"], [[]])
            idx = (body["start"] - 1) // body["size"]
            if self.query_fail_from_page is not None and idx >= self.query_fail_from_page:
                return httpx.Response(503, json={})
            page = pages[idx] if idx < len(pages) else []
            nxt = body["start"] + body["size"] if idx + 1 < len(pages) else None
            return httpx.Response(200, json={"ia::result": page, "ia::meta": {"next": nxt}})
        if path.endswith("/objects/accounts-payable/bill") and request.method == "POST":
            status = self.create_statuses.pop(0) if self.create_statuses else self.create_status
            if status in (200, 201) and self.bill is None:
                body = json.loads(request.content)
                total = sum((Decimal(li["txnAmount"]) for li in body["lines"]), Decimal(0))
                self.bill = {
                    "key": "777",
                    "state": self.read_back_state or body.get("state") or "draft",
                    "totalTxnAmount": self.read_back_total or str(total),
                    "postingDate": body.get("createdDate"),
                }
            return httpx.Response(status, json=self.create_body)
        if path.endswith("/workflows/accounts-payable/bill/reverse"):
            return httpx.Response(self.reverse_status, json={"ia::result": {"state": "reversed"}})
        if "/objects/accounts-payable/bill/" in path and request.method == "GET":
            if self.bill is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"ia::result": self.bill})
        if "/objects/accounts-payable/bill/" in path and request.method == "DELETE":
            return httpx.Response(self.delete_status)
        return httpx.Response(599)

    def of(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    """Tokens are cached per process; every test starts (and ends) with none."""
    clear_token_cache()
    yield
    clear_token_cache()


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


def test_lines_that_do_not_sum_to_the_header_are_refused():
    """Never post a bill whose total differs from the approved amount — and
    never fold coded lines onto the header's account to make it fit (the
    one-header-line fallback this replaced moved line 1's 6200 expense to
    6100, an account the approver never saw)."""
    lines = [LineItemPayload(line_number=1, total=Decimal("90.00"), gl_account_erp_id="6200")]
    fake = FakeIntacct()
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(line_items=lines)))
    assert not result.success and result.retryable is False
    assert result.message == "Sage Intacct post refused: amount_mismatch"
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


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
    assert result.not_posted is True
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
    fake.bill = {"key": "555", "state": "posted", "totalTxnAmount": "100.00"}
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.success and result.erp_document_id == "555"
    assert fake.of("POST", "/objects/accounts-payable/bill") == []
    assert fake.of("GET", "/objects/accounts-payable/bill/555")


def test_existing_bill_counts_only_when_posted_at_the_approved_total():
    """A retry that finds our earlier bill holds it to the same terms as a bill
    just created: a draft is not posted, and a different total is a mismatch."""
    fake = FakeIntacct()
    fake.query_rows["accounts-payable/bill"] = [[{"key": "555", "id": "555"}]]
    fake.bill = {"key": "555", "state": "draft", "totalTxnAmount": "100.00"}
    draft = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not draft.success and draft.refusal_code == "bill_left_unposted"
    assert draft.erp_document_id == "555"

    fake.bill = {"key": "555", "state": "posted", "totalTxnAmount": "115.00"}
    mismatch = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not mismatch.success and mismatch.refusal_code == "posted_total_mismatch"
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


def test_failed_lookup_is_not_read_as_a_miss():
    """A 5xx on the idempotency query must not lead to a create — the first
    attempt may already have posted."""
    fake = FakeIntacct()
    fake.query_status = 503
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


_ACTIVE_VENDOR = {"key": "11", "id": "V-ACME", "status": "active"}
_ACTIVE_ACCOUNT = {"key": "61", "id": "6100", "status": "active"}


def test_create_refusal_message_is_pii_free():
    fake = FakeIntacct()
    fake.create_status = 422
    fake.query_rows["accounts-payable/vendor"] = [[_ACTIVE_VENDOR]]
    fake.query_rows["general-ledger/account"] = [[_ACTIVE_ACCOUNT]]
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
    assert result.message == "Sage Intacct post refused: bill_refused"
    for token in ("12-3456789", "Bank Street", "PO Box", "GB29", "Validation failed"):
        assert token not in result.message


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_a_refused_create_is_coded_never_retried_and_created_nothing(status):
    """QuickBooks lesson 3 (decisions §285): a deterministic 4xx answer to the
    create is a refusal of the bill, not an outcome we can't know."""
    fake = FakeIntacct()
    fake.create_status = status
    fake.query_rows["accounts-payable/vendor"] = [[_ACTIVE_VENDOR]]
    fake.query_rows["general-ledger/account"] = [[_ACTIVE_ACCOUNT]]
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.refusal_code == "bill_refused" and result.refusal_provider == "Sage Intacct"
    assert result.not_posted is True


@pytest.mark.parametrize(
    "vendor",
    [
        {"key": "11", "id": "V-ACME", "status": "inactive"},
        {"key": "11", "id": "V-ACME", "status": "activeNonPosting"},
        {"key": "11", "id": "V-ACME", "status": "active", "state": "s"},
        None,  # no such vendor at all
    ],
)
def test_a_refused_create_names_an_unusable_vendor(vendor):
    fake = FakeIntacct()
    fake.create_status = 422
    fake.query_rows["accounts-payable/vendor"] = [[vendor] if vendor else []]
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.refusal_code == "vendor_inactive"
    assert result.not_posted is True and result.retryable is False
    [vendor_query] = [
        json.loads(r.content)
        for r in fake.of("POST", "/services/core/query")
        if json.loads(r.content)["object"] == "accounts-payable/vendor"
    ]
    assert vendor_query["filters"] == [{"$eq": {"id": "V-ACME"}}]


def test_a_refused_create_names_inactive_accounts_by_our_code():
    fake = FakeIntacct()
    fake.create_status = 422
    fake.query_rows["accounts-payable/vendor"] = [[_ACTIVE_VENDOR]]
    fake.query_rows["general-ledger/account"] = [
        [{"key": "61", "id": "6100", "status": "inactive"}]
    ]
    result = _run(
        fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(gl_account="Supplies"))
    )
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["Supplies"]}
    assert result.not_posted is True


def test_an_unreadable_reference_is_not_read_as_inactive():
    """A vendor lookup that fails is "don't know", never "inactive"."""
    fake = FakeIntacct()
    fake.create_status = 422
    fake.query_rows["accounts-payable/bill"] = [[]]

    def handler(request, _inner=fake.handler):
        if request.url.path.endswith("/services/core/query"):
            obj = json.loads(request.content)["object"]
            if obj != "accounts-payable/bill":
                fake.requests.append(request)
                return httpx.Response(503, json={})
        return _inner(request)

    fake.handler = handler
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.refusal_code == "bill_refused"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_create_that_may_succeed_later_stays_retryable_and_possibly_posted(status):
    fake = FakeIntacct()
    fake.create_status = status
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is True
    assert result.not_posted is False
    assert result.message == erp_failure_message("Sage Intacct", status)


def test_a_401_on_the_create_mints_a_fresh_token_once_then_succeeds():
    fake = FakeIntacct()
    fake.create_statuses = [401]
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.success
    assert len(fake.of("POST", "/objects/accounts-payable/bill")) == 2
    assert fake.tokens_minted == 2


def test_a_401_twice_on_the_create_is_not_connected_and_created_nothing():
    fake = FakeIntacct()
    fake.create_status = 401
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert result.refusal_code == "not_connected"
    assert result.not_posted is True and result.retryable is False
    assert len(fake.of("POST", "/objects/accounts-payable/bill")) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_refused_credentials_are_a_coded_not_connected_refusal(status):
    fake = FakeIntacct()
    fake.token_status = status
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.refusal_code == "not_connected" and result.not_posted is True
    assert "s3cr3t" not in result.message and "cid-123" not in result.message
    assert fake.of("POST", "/objects/accounts-payable/bill") == []


def test_a_transient_token_failure_is_retryable_and_names_the_status_only():
    fake = FakeIntacct()
    fake.token_status = 503
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is True and result.refusal_code is None
    assert result.message == "Sage Intacct token exchange failed: HTTP 503"
    assert "s3cr3t" not in result.message and "cid-123" not in result.message


def test_the_token_is_cached_across_operations_until_it_nears_expiry():
    """Intacct tokens live 12 h; minting one per call spends a transaction."""
    fake = FakeIntacct()
    adapter = SageIntacctAdapter(CONFIG)
    _run(fake, lambda: adapter.post_invoice(_payload()))
    _run(fake, lambda: SageIntacctAdapter(CONFIG).get_invoice_status("777"))
    assert fake.tokens_minted == 1
    # Other credentials are another token.
    _run(fake, lambda: SageIntacctAdapter({**CONFIG, "user_id": "other"}).get_invoice_status("1"))
    assert fake.tokens_minted == 2
    # The cache is keyed by a digest: no secret is held a second time.
    from app.services.erp_adapters import sage_intacct

    assert not any("s3cr3t" in key for key in sage_intacct._TOKEN_CACHE)


# ---------------------------------------------------------------------------
# Posting state + the booked total (audit rows 1, 4, 6)
# ---------------------------------------------------------------------------


def test_the_bill_is_created_posted_not_as_intacct_s_default_draft():
    fake = FakeIntacct()
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    body, _ = _create_json(fake)
    assert body["state"] == "posted"
    assert result.success and result.erp_document_id == "777"
    # Read back before it counts.
    assert fake.of("GET", "/objects/accounts-payable/bill/777")


def test_a_bill_intacct_keeps_as_a_draft_is_never_reported_posted():
    """What the old adapter did: Intacct answered 201 for a draft that booked
    nothing, and we recorded "Posted to Sage Intacct"."""
    fake = FakeIntacct()
    fake.read_back_state = "submitted"
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.refusal_code == "bill_left_unposted"
    assert result.not_posted is False  # the bill exists in Intacct
    assert fake.of("DELETE", "/objects/accounts-payable/bill/777") == []


def test_a_different_booked_total_is_a_mismatch_and_the_bill_is_removed():
    fake = FakeIntacct()
    fake.read_back_total = "108.25"  # a tax solution added tax
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.refusal_code == "posted_total_mismatch"
    assert "the bill was voided" in result.message
    assert fake.of("DELETE", "/objects/accounts-payable/bill/777")


def test_a_bill_that_cannot_be_read_back_is_never_success():
    fake = FakeIntacct()

    def handler(request, _inner=fake.handler):
        if request.method == "GET":
            fake.requests.append(request)
            return httpx.Response(503, json={})
        return _inner(request)

    fake.handler = handler
    result = _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    assert not result.success and result.retryable is True
    assert result.erp_document_id == "777"


@pytest.mark.parametrize("missing", ["invoice_date", "due_date"])
def test_both_dates_are_required_before_any_call(missing):
    fake = FakeIntacct()
    result = _run(
        fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload(**{missing: None}))
    )
    assert result.refusal_code == "missing_dates" and result.not_posted is True
    assert fake.requests == []


def test_every_query_is_ordered_so_pages_cannot_shift():
    fake = FakeIntacct()
    _run(fake, SageIntacctAdapter(CONFIG).list_vendors)
    _run(fake, lambda: SageIntacctAdapter(CONFIG).post_invoice(_payload()))
    queries = [json.loads(r.content) for r in fake.of("POST", "/services/core/query")]
    assert queries and all(q["orderBy"] == [{"key": "asc"}] for q in queries)


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
        # Nothing due is not "paid" until Intacct's own state says so: an
        # applied credit zeroes the due amount too (decisions §303).
        (
            {"state": "posted", "totalTxnAmount": "100.00", "totalTxnAmountDue": "0.00"},
            ErpInvoiceStatus.open,
        ),
        ({"state": "analyzing"}, ErpInvoiceStatus.draft),
        # "Posted but has not affected the GL": nothing owing, no payment (§303).
        ({"state": "noValue"}, ErpInvoiceStatus.settled_no_payment),
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
    assert fake.of("POST", "/workflows/accounts-payable/bill/reverse") == []


def test_void_reverses_a_posted_bill_a_reversal_enabled_company_will_not_delete():
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": "posted", "postingDate": "2999-01-01"}
    fake.delete_status = 400
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is True
    [reverse] = fake.of("POST", "/workflows/accounts-payable/bill/reverse")
    body = json.loads(reverse.content)
    assert body["key"] == "777"
    # Intacct requires the reversal to be dated after the bill's posting date.
    assert body["reverseDate"] == "2999-01-02"


def test_void_reverses_today_when_the_bill_was_posted_earlier():
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": "posted", "postingDate": "2000-01-01"}
    fake.delete_status = 400
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is True
    [reverse] = fake.of("POST", "/workflows/accounts-payable/bill/reverse")
    assert json.loads(reverse.content)["reverseDate"] == date.today().isoformat()


def test_void_fails_when_neither_delete_nor_reverse_is_allowed():
    fake = FakeIntacct()
    fake.bill = {"key": "777", "state": "posted", "postingDate": "2000-01-01"}
    fake.delete_status = 400
    fake.reverse_status = 400
    assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is False


def test_void_never_reverses_a_draft_or_after_a_transient_failure():
    for state, status in (("draft", 400), ("posted", 503)):
        fake = FakeIntacct()
        fake.bill = {"key": "777", "state": state}
        fake.delete_status = status
        assert _run(fake, lambda: SageIntacctAdapter(CONFIG).void_invoice("777")) is False
        assert fake.of("POST", "/workflows/accounts-payable/bill/reverse") == []


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
    """`totalInTxnCurrency` and a top-level `txnCurrency` are the purchasing
    document's fields; `txnTotal` / `currency.txnCurrency` do not exist there
    and Intacct 400s a query naming them."""
    fake = FakeIntacct()
    fake.query_rows["purchasing/document::Purchase Order"] = [
        [
            {
                "documentNumber": "PO-1",
                "vendor.name": "Acme",
                "state": "pending",
                "totalInTxnCurrency": "1250.10",
                "txnCurrency": "USD",
            },
            {
                "documentNumber": "PO-2",
                "vendor": {"name": "Beta"},
                "state": "closed",
                "totalInTxnCurrency": "5.00",
                "txnCurrency": "ZAR",
            },
            # No total reported: skipped, never synced at 0.
            {"documentNumber": "PO-3", "state": "pending", "txnCurrency": "USD"},
        ]
    ]
    pos = _run(fake, lambda: SageIntacctAdapter(CONFIG).list_pos())
    assert [(p.po_number, p.vendor_name, p.total, p.status, p.currency) for p in pos] == [
        ("PO-1", "Acme", Decimal("1250.10"), "open", "USD"),
        ("PO-2", "Beta", Decimal("5.00"), "closed", "ZAR"),
    ]
    [query] = fake.of("POST", "/services/core/query")
    fields = json.loads(query.content)["fields"]
    assert "totalInTxnCurrency" in fields and "txnCurrency" in fields
    assert "txnTotal" not in fields and "currency.txnCurrency" not in fields


def test_list_vendors_emits_unusable_vendors_inactive():
    fake = FakeIntacct()
    fake.query_rows["accounts-payable/vendor"] = [
        [
            {"id": "V1", "name": "A", "status": "active"},
            {"id": "V2", "name": "B", "status": "inactive"},
            {"id": "V3", "name": "C", "status": "activeNonPosting"},
            {"id": "V4", "name": "D", "status": "active", "state": "s"},
            {"id": "V5", "name": "E", "status": None, "state": "a"},
        ]
    ]
    vendors = _run(fake, SageIntacctAdapter(CONFIG).list_vendors)
    assert [(v.erp_vendor_id, v.active) for v in vendors] == [
        ("V1", True),
        ("V2", False),
        ("V3", False),
        ("V4", False),
        ("V5", True),
    ]
    [query] = fake.of("POST", "/services/core/query")
    assert {"status", "state"} <= set(json.loads(query.content)["fields"])


def test_list_gl_accounts_emits_inactive_accounts_inactive():
    fake = FakeIntacct()
    fake.query_rows["general-ledger/account"] = [
        [
            {"id": "6100", "name": "A", "status": "active"},
            {"id": "6900", "name": "B", "status": "inactive"},
        ]
    ]
    accounts = _run(fake, SageIntacctAdapter(CONFIG).list_gl_accounts)
    assert [(a.erp_account_id, a.active) for a in accounts] == [("6100", True), ("6900", False)]


def test_chart_and_vendor_listings_raise_on_failure_the_po_list_degrades():
    """The chart and vendor syncs read absence as evidence, so a failed listing
    raises instead of returning [] (§284). The PO list keeps degrading."""
    fake = FakeIntacct()
    fake.query_status = 500
    adapter = SageIntacctAdapter(CONFIG)
    for listing in (adapter.list_vendors, adapter.list_gl_accounts):
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(fake, listing)
        assert exc.value.gap is ListingGap.fetch_failed
    assert _run(fake, adapter.list_pos) == []


def test_a_later_page_failing_raises_never_returns_the_first_pages():
    fake = FakeIntacct()
    fake.query_rows["accounts-payable/vendor"] = [
        [{"id": f"V{p}{i}", "name": "A"} for i in range(100)] for p in range(2)
    ]
    fake.query_fail_from_page = 1
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SageIntacctAdapter(CONFIG).list_vendors)
    assert exc.value.gap is ListingGap.fetch_failed


def test_a_chart_past_the_sync_bound_raises_truncated():
    fake = FakeIntacct()
    pages = SYNC_LISTING_MAX_ROWS // 100
    fake.query_rows["general-ledger/account"] = [
        [{"id": f"{p}-{i}", "name": "A"} for i in range(100)] for p in range(pages + 1)
    ]
    with pytest.raises(ErpListingIncomplete) as exc:
        _run(fake, SageIntacctAdapter(CONFIG).list_gl_accounts)
    assert exc.value.gap is ListingGap.truncated


def test_test_connection():
    fake = FakeIntacct()
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is True
    clear_token_cache()
    fake.token_status = 401
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is False


def test_test_connection_drops_a_token_intacct_no_longer_accepts():
    """A cached token for an app since de-authorized: the 401 re-mints, and the
    refused exchange fails the test rather than a stale token passing it."""
    fake = FakeIntacct()
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is True
    fake.query_status = 401
    fake.token_status = 401
    assert _run(fake, SageIntacctAdapter(CONFIG).test_connection) is False


def test_bill_lines_refuses_a_coded_line_whose_account_is_not_linked():
    """Shared by Intacct, SYSPRO and Sage SA: a coded line with no ERP id is
    refused rather than folded into one line on the header's account, even
    when the line totals would not have been posted per line anyway."""
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("60.00"), gl_account="6300"),
            LineItemPayload(
                line_number=2, total=Decimal("40.00"), gl_account="6100", gl_account_erp_id="6100"
            ),
        ]
    )
    assert bill_lines(payload) == "account_not_linked"


def test_bill_lines_puts_an_uncoded_line_on_the_header_account():
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        line_items=[
            LineItemPayload(
                line_number=1, total=Decimal("60.00"), gl_account="6200", gl_account_erp_id="6200"
            ),
            LineItemPayload(line_number=2, total=Decimal("40.00")),
        ]
    )
    assert [(gl, amt) for gl, amt, _ in bill_lines(payload)] == [
        ("6200", Decimal("60.00")),
        ("6100", Decimal("40.00")),
    ]


def test_bill_lines_refuses_a_line_with_no_amount():
    """Same rule as ``bill_allocation``: a line with neither a total nor
    quantity x unit price is refused, never replaced by one header line."""
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("60.00"), gl_account_erp_id="6200"),
            LineItemPayload(line_number=2, description="no amount"),
        ]
    )
    assert bill_lines(payload) == "line_amount_missing"


def test_bill_lines_derives_an_amount_from_quantity_and_unit_price():
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        line_items=[
            LineItemPayload(
                line_number=1,
                quantity=Decimal("3"),
                unit_price=Decimal("20.00"),
                gl_account_erp_id="6200",
            ),
            LineItemPayload(line_number=2, total=Decimal("40.00")),
        ]
    )
    assert [(gl, amt) for gl, amt, _ in bill_lines(payload)] == [
        ("6200", Decimal("60.00")),
        ("6100", Decimal("40.00")),
    ]


def test_bill_lines_grosses_up_tax_exclusive_lines_with_their_stated_tax():
    """Lines + tax_amount == amount: each line posts its own gross, so the
    coding survives and the total is still exactly the approved amount."""
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        tax_amount=Decimal("15.00"),
        amount=Decimal("115.00"),
        line_items=[
            LineItemPayload(
                line_number=1, total=Decimal("60.00"), tax=Decimal("9.00"), gl_account_erp_id="6200"
            ),
            LineItemPayload(line_number=2, total=Decimal("40.00"), tax=Decimal("6.00")),
        ],
    )
    lines = bill_lines(payload)
    assert [(gl, amt) for gl, amt, _ in lines] == [
        ("6200", Decimal("69.00")),
        ("6100", Decimal("46.00")),
    ]
    assert sum(amt for _, amt, _ in lines) == payload.amount


def test_bill_lines_refuses_unitemised_tax_on_several_exclusive_lines():
    from app.services.erp_adapters.bill_lines import bill_lines

    payload = _payload(
        tax_amount=Decimal("15.00"),
        amount=Decimal("115.00"),
        line_items=[
            LineItemPayload(line_number=1, total=Decimal("60.00"), gl_account_erp_id="6200"),
            LineItemPayload(line_number=2, total=Decimal("40.00")),
        ],
    )
    assert bill_lines(payload) == "tax_not_itemised"
