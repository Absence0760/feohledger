"""QuickBooks Online adapter (`erp_adapters/quickbooks_online.py`).

Bill request shape, money exactness, the two idempotency layers (`requestid`
plus the DocNumber + PrivateNote pre-check), every fail-closed refusal, the
one-shot 401 retry, PII-free failures, status mapping, delete-as-void, the
read syncs and the base-URL selection.

HTTP is mocked with `patch("httpx.AsyncClient")` as in
`test_erp_adapter_idempotency.py`; tokens with a patched
`erp_oauth.get_access_token` (the token flow has its own suite,
`test_erp_oauth.py`).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services import erp_oauth
from app.services.erp_adapters.base import (
    ErpInvoiceStatus,
    InvoicePayload,
    LineItemPayload,
)
from app.services.erp_adapters.dispatcher import get_erp_adapter
from app.services.erp_adapters.quickbooks_online import QuickBooksOnlineAdapter
from app.utils.json_money import dumps_exact_json

REALM = "9130"
CONFIG = {
    "type": "quickbooks_online",
    "integration_method": "direct",
    "environment": "production",
    "oauth": {"provider": "quickbooks_online", "external_tenant_id": REALM},
}


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.content = dumps_exact_json(body).encode() if body is not None else b""
    return r


def _prefs(home: str = "USD", multi: bool = False) -> dict:
    return {
        "Preferences": {
            "CurrencyPrefs": {"HomeCurrency": {"value": home}, "MultiCurrencyEnabled": multi}
        }
    }


def _object_not_found() -> MagicMock:
    """What Intuit answers a read of a deleted bill with."""
    return _resp(400, {"Fault": {"Error": [{"Message": "Object Not Found", "code": "610"}]}})


class _Qbo:
    """Routes `client.request(method, url, params=…, content=…, headers=…)`.

    Stateful like QuickBooks: bills live by Id until deleted; a deleted bill
    reads as Fault 610; a repeated `requestid` replays the original create
    response, even for a bill since deleted.
    """

    def __init__(
        self, *, prefs=None, existing_bills=None, post=None, bill=None, total_shift=Decimal(0)
    ):
        self.calls: list[dict] = []
        self.prefs = prefs or _prefs()
        self.existing_bills = existing_bills or []
        self.post = post
        self.bills: dict[str, dict] = {}
        if bill:
            self.bills[str(bill["Id"])] = bill
        self.deleted: set[str] = set()
        self.replays: dict[str, dict] = {}
        self.next_id = 145
        # Added to the TotalAmt QuickBooks reports, as a default tax code would.
        self.total_shift = total_shift

    def _created(self, content: str, request_id: str | None) -> MagicMock:
        """Like QuickBooks: TotalAmt is computed from the lines it was sent."""
        if request_id in self.replays:
            return _resp(200, self.replays[request_id])
        body = json.loads(content, parse_float=Decimal)
        total = sum((line["Amount"] for line in body["Line"]), Decimal(0)) + self.total_shift
        bill_id = str(self.next_id)
        self.next_id += 1
        bill = {"Id": bill_id, "DocNumber": body["DocNumber"], "SyncToken": "0", "TotalAmt": total}
        self.bills[bill_id] = {**bill, "Balance": total}
        if request_id:
            self.replays[request_id] = {"Bill": bill}
        return _resp(200, {"Bill": bill})

    def delete(self, bill_id: str) -> None:
        """Delete a bill as a person (or an earlier push) would in QuickBooks."""
        self.bills.pop(bill_id, None)
        self.deleted.add(bill_id)

    async def request(self, method, url, params=None, content=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "content": content, "headers": headers}
        )
        path = url.split(f"/v3/company/{REALM}/", 1)[1]
        if path == "preferences":
            return _resp(200, self.prefs)
        if path == "query":
            return _resp(200, {"QueryResponse": {"Bill": self.existing_bills}})
        if method == "POST" and path == "bill":
            if (params or {}).get("operation") == "delete":
                bill_id = str(json.loads(content)["Id"])
                if bill_id not in self.bills:
                    return _object_not_found()
                self.delete(bill_id)
                return _resp(200, {"Bill": {"Id": bill_id, "status": "Deleted"}})
            if self.post is not None:
                if self.post.status_code == 200:
                    created = json.loads(self.post.content, parse_float=Decimal).get("Bill")
                    if created and created.get("Id") is not None:
                        self.bills.setdefault(str(created["Id"]), created)
                return self.post
            return self._created(content, (params or {}).get("requestid"))
        if path.startswith("bill/"):
            bill_id = path.split("/", 1)[1]
            if bill_id in self.deleted:
                return _object_not_found()
            bill = self.bills.get(bill_id)
            return _resp(200, {"Bill": bill}) if bill else _resp(400, {})
        if path.startswith("companyinfo/"):
            return _resp(200, {"CompanyInfo": {}})
        raise AssertionError(f"unexpected {method} {url}")

    def posts(self):
        return [
            c
            for c in self.calls
            if c["method"] == "POST" and (c["params"] or {}).get("operation") != "delete"
        ]

    def deletes(self):
        return [c for c in self.calls if (c["params"] or {}).get("operation") == "delete"]


@pytest.fixture
def token():
    with patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value="tok-1")) as mocked:
        yield mocked


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_api_base", "")


def _run_with(qbo: _Qbo, coro_factory):
    import asyncio

    with patch("httpx.AsyncClient") as cm:
        cm.return_value.__aenter__.return_value.request = qbo.request
        return asyncio.run(coro_factory())


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        correlation_id="corr-7d1f",
        invoice_number="INV-1",
        vendor_name="Acme",
        vendor_erp_id="56",
        amount=Decimal("100.10"),
        currency="USD",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        gl_account_erp_id="7",
        vendor_tax_id="12-3456789",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _adapter(config=None) -> QuickBooksOnlineAdapter:
    return QuickBooksOnlineAdapter(config or CONFIG)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registered_as_a_direct_adapter():
    assert isinstance(get_erp_adapter(CONFIG), QuickBooksOnlineAdapter)


# ---------------------------------------------------------------------------
# post_invoice — shape, money, idempotency
# ---------------------------------------------------------------------------


def test_post_sends_an_exact_bill_with_request_id(token, no_override):
    qbo = _Qbo()
    payload = _payload(
        line_items=[
            LineItemPayload(line_number=1, description="Paper", total=Decimal("0.10")),
            LineItemPayload(
                line_number=2,
                description="Toner",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                gl_account_erp_id="8",
            ),
        ]
    )
    result = _run_with(qbo, lambda: _adapter().post_invoice(payload))

    assert result.success, result.message
    assert result.erp_document_id == "145"
    (post,) = qbo.posts()
    assert post["url"] == f"https://quickbooks.api.intuit.com/v3/company/{REALM}/bill"
    assert post["params"] == {"minorversion": "75", "requestid": "corr-7d1f"}
    assert post["headers"]["Authorization"] == "Bearer tok-1"
    body = json.loads(post["content"], parse_float=Decimal)
    assert body["VendorRef"] == {"value": "56"}
    assert body["DocNumber"] == "INV-1"
    assert body["PrivateNote"] == "FeohLedger corr-7d1f"
    assert body["TxnDate"] == "2026-09-01"
    assert body["DueDate"] == "2026-10-01"
    assert "CurrencyRef" not in body  # home currency
    assert body["Line"] == [
        {
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": Decimal("0.10"),
            "Description": "Paper",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "7"}},
        },
        {
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": Decimal("100.00"),
            "Description": "Toner",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "8"}},
        },
    ]
    # Exact decimal literals on the wire, never a float repr.
    assert '"Amount": 0.10' in post["content"] or '"Amount":0.10' in post["content"]
    # Never the vendor's tax id.
    assert "12-3456789" not in post["content"]


def test_post_without_lines_uses_the_header_account(token, no_override):
    qbo = _Qbo()
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.success
    body = json.loads(qbo.posts()[0]["content"], parse_float=Decimal)
    assert body["Line"][0]["Amount"] == Decimal("100.10")
    assert body["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "7"}


def test_retry_finds_the_bill_already_posted(token, no_override):
    existing = {
        "Id": "145",
        "DocNumber": "INV-1",
        "VendorRef": {"value": "56"},
        "PrivateNote": "FeohLedger corr-7d1f",
        "TotalAmt": Decimal("100.10"),
    }
    qbo = _Qbo(existing_bills=[existing])
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.success
    assert result.erp_document_id == "145"
    assert qbo.posts() == []
    query = next(c for c in qbo.calls if c["url"].endswith("/query"))
    assert query["params"]["query"] == "select * from Bill where DocNumber = 'INV-1'"


@pytest.mark.parametrize(
    "other",
    [
        # Same number, another vendor's bill.
        {"Id": "9", "DocNumber": "INV-1", "VendorRef": {"value": "99"},
         "PrivateNote": "FeohLedger corr-7d1f"},
        # Same number and vendor, keyed by a human — not ours.
        {"Id": "9", "DocNumber": "INV-1", "VendorRef": {"value": "56"}, "PrivateNote": ""},
    ],
)  # fmt: skip
def test_pre_check_ignores_bills_that_are_not_ours(token, no_override, other):
    qbo = _Qbo(existing_bills=[other])
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.success
    assert len(qbo.posts()) == 1


def test_doc_number_quotes_are_escaped_in_the_query(token, no_override):
    qbo = _Qbo()
    _run_with(qbo, lambda: _adapter().post_invoice(_payload(invoice_number="O'Brien-1")))
    query = next(c for c in qbo.calls if c["url"].endswith("/query"))
    assert query["params"]["query"] == "select * from Bill where DocNumber = 'O\\'Brien-1'"


def test_a_total_quickbooks_changed_deletes_the_bill_and_is_not_retried(token, no_override):
    """A default tax code added tax: QuickBooks' TotalAmt is not the approved
    amount, so the bill just created is deleted and the push fails for good."""
    qbo = _Qbo(total_shift=Decimal("8.01"))
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.message == (
        "QuickBooks Online post failed: posted_total_mismatch (the bill was voided)"
    )
    assert result.erp_document_id == "145"
    (delete,) = qbo.deletes()
    assert json.loads(delete["content"]) == {"Id": "145", "SyncToken": "0"}


def test_a_created_bill_with_no_total_is_unconfirmed_not_success(token, no_override):
    qbo = _Qbo(post=_resp(200, {"Bill": {"Id": "145", "DocNumber": "INV-1"}}))
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert result.message.startswith("QuickBooks Online post unconfirmed: posted_total_unconfirmed")
    assert result.erp_document_id == "145"


# ---------------------------------------------------------------------------
# post_invoice — a `requestid` replay of a bill since deleted
# ---------------------------------------------------------------------------


def _request_ids(qbo: _Qbo) -> list[str]:
    return [c["params"]["requestid"] for c in qbo.posts()]


def test_a_retry_after_a_mismatch_creates_afresh_under_the_next_request_id(token, no_override):
    """Push 1 books a taxed total and deletes the bill. The operator fixes the
    tax code and retries: Intuit replays push 1's response for a deleted bill,
    so the adapter moves to the next requestid instead of re-checking a ghost."""
    qbo = _Qbo(total_shift=Decimal("8.01"))
    first = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert first.message == (
        "QuickBooks Online post failed: posted_total_mismatch (the bill was voided)"
    )
    assert qbo.deleted == {"145"}

    qbo.total_shift = Decimal(0)
    qbo.calls.clear()
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))

    assert result.success, result.message
    assert result.erp_document_id == "146"
    assert _request_ids(qbo) == ["corr-7d1f", "corr-7d1f#r2"]
    assert qbo.deletes() == []
    # Both attempts carry the same PrivateNote, so the pre-check finds either.
    assert {json.loads(p["content"])["PrivateNote"] for p in qbo.posts()} == {
        "FeohLedger corr-7d1f"
    }


def test_a_replayed_matching_total_for_a_deleted_bill_is_never_success(token, no_override):
    """The replayed total matches, but the bill it names was deleted in
    QuickBooks: reporting it posted would point the ledger at nothing."""
    qbo = _Qbo()
    assert _run_with(qbo, lambda: _adapter().post_invoice(_payload())).success
    qbo.delete("145")
    qbo.calls.clear()

    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))

    assert result.success
    assert result.erp_document_id == "146"
    assert set(qbo.bills) == {"146"}


def test_a_retry_that_still_mismatches_says_the_new_bill_was_voided(token, no_override):
    """Before the fix this read "could not be voided": the replayed bill was
    already gone, so deleting it failed."""
    qbo = _Qbo(total_shift=Decimal("8.01"))
    _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    qbo.calls.clear()

    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))

    assert result.retryable is False
    assert result.message == (
        "QuickBooks Online post failed: posted_total_mismatch (the bill was voided)"
    )
    assert result.erp_document_id == "146"
    assert qbo.deleted == {"145", "146"}


def test_an_unreadable_bill_is_never_taken_for_a_deleted_one(token, no_override):
    """Only Fault 610 / status Deleted mean gone. A 5xx on the read-back fails
    the push (retryably) without creating a second bill."""
    qbo = _Qbo()
    real_request = qbo.request

    async def read_back_fails(method, url, **kw):
        if method == "GET" and "/bill/" in url:
            qbo.calls.append({"method": method, "url": url, **kw})
            return _resp(503, {})
        return await real_request(method, url, **kw)

    qbo.request = read_back_fails
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))

    assert not result.success and result.retryable is True
    assert result.message.startswith("QuickBooks Online post failed: HTTP 503 ")
    assert _request_ids(qbo) == ["corr-7d1f"]


def test_an_exhausted_request_id_walk_is_refused_for_good(token, no_override):
    from app.services.erp_adapters.posted_total import MAX_CREATE_ATTEMPTS, create_attempt_key

    qbo = _Qbo()
    for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
        bill_id = str(900 + attempt)
        qbo.replays[create_attempt_key("corr-7d1f", attempt, 50)] = {
            "Bill": {"Id": bill_id, "DocNumber": "INV-1", "TotalAmt": Decimal("100.10")}
        }
        qbo.deleted.add(bill_id)

    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))

    assert not result.success and result.retryable is False
    assert result.message.startswith("QuickBooks Online post failed: previous_bill_removed")
    assert len(qbo.posts()) == MAX_CREATE_ATTEMPTS
    assert qbo.bills == {}


def test_request_ids_fit_intuits_limit(token, no_override):
    from app.services.erp_adapters.posted_total import create_attempt_key

    long_id = "c" * 60
    assert create_attempt_key(long_id, 1, 50) == "c" * 50
    assert create_attempt_key(long_id, 2, 50) == "c" * 47 + "#r2"
    assert create_attempt_key(long_id, 10, 50) == "c" * 46 + "#r10"
    assert len({create_attempt_key(long_id, n, 50) for n in range(1, 11)}) == 10


@pytest.mark.parametrize(
    ("arrange", "expected"),
    [
        (lambda q: None, "voided"),
        (lambda q: q.delete("145"), "already_gone"),
        (lambda q: q.bills["145"].update(Balance=Decimal("40")), "not_voided"),
    ],
)
def test_void_of_a_mismatched_bill_reports_three_outcomes(token, no_override, arrange, expected):
    bill = {"Id": "145", "SyncToken": "0", "Balance": Decimal("1"), "TotalAmt": Decimal("1")}
    qbo = _Qbo(bill=bill)
    arrange(qbo)
    outcome = _run_with(qbo, lambda: _adapter()._void_mismatched_bill("145"))
    assert outcome == expected


def test_an_earlier_bill_with_a_different_total_is_not_reported_as_posted(token, no_override):
    existing = {
        "Id": "145",
        "DocNumber": "INV-1",
        "VendorRef": {"value": "56"},
        "PrivateNote": "FeohLedger corr-7d1f",
        "TotalAmt": Decimal("108.11"),
    }
    qbo = _Qbo(existing_bills=[existing], bill={**existing, "Balance": Decimal("108.11")})
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success and result.retryable is False
    assert "posted_total_mismatch" in result.message
    assert qbo.posts() == []


def test_not_connected_is_a_non_retryable_refusal(no_override):
    with patch.object(
        erp_oauth,
        "get_access_token",
        new=AsyncMock(side_effect=erp_oauth.ErpNotConnectedError("quickbooks_online")),
    ):
        result = _run_with(_Qbo(), lambda: _adapter().post_invoice(_payload()))
    assert result.message == "QuickBooks Online post refused: not_connected"
    assert result.retryable is False


# ---------------------------------------------------------------------------
# post_invoice — fail-closed refusals (no HTTP at all for the payload ones)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"vendor_erp_id": None}, "vendor_not_linked"),
        ({"invoice_number": "X" * 22}, "doc_number_too_long"),
        ({"gl_account_erp_id": None}, "account_not_linked"),
        (
            {"line_items": [LineItemPayload(line_number=1, total=Decimal("99.10"))]},
            "amount_mismatch",
        ),
        (
            {"line_items": [LineItemPayload(line_number=1, description="no amount")]},
            "line_amount_missing",
        ),
        # A coded line whose account has no QuickBooks id is refused, never
        # moved onto the (linked) header account.
        (
            {
                "line_items": [
                    LineItemPayload(
                        line_number=1, total=Decimal("100.10"), gl_account="6300-Travel"
                    )
                ]
            },
            "account_not_linked",
        ),
        (
            {
                "gl_account_erp_id": None,
                "line_items": [LineItemPayload(line_number=1, total=Decimal("100.10"))],
            },
            "account_not_linked",
        ),
    ],
)
def test_payload_refusals_never_call_quickbooks(token, no_override, overrides, reason):
    qbo = _Qbo()
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload(**overrides)))
    assert not result.success
    assert result.message == f"QuickBooks Online post refused: {reason}"
    assert qbo.calls == []


def test_doc_number_at_the_limit_is_accepted(token, no_override):
    qbo = _Qbo()
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload(invoice_number="X" * 21)))
    assert result.success


def test_foreign_currency_without_multicurrency_is_refused(token, no_override):
    qbo = _Qbo(prefs=_prefs("USD", multi=False))
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload(currency="EUR")))
    assert not result.success
    assert result.message == "QuickBooks Online post refused: currency_not_enabled"
    assert qbo.posts() == []


def test_foreign_currency_with_multicurrency_names_the_currency(token, no_override):
    qbo = _Qbo(prefs=_prefs("USD", multi=True))
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload(currency="eur")))
    assert result.success
    body = json.loads(qbo.posts()[0]["content"])
    assert body["CurrencyRef"] == {"value": "EUR"}


def test_unknown_home_currency_is_refused(token, no_override):
    qbo = _Qbo(prefs={"Preferences": {"CurrencyPrefs": {}}})
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.message == "QuickBooks Online post refused: currency_unknown"


# ---------------------------------------------------------------------------
# failures, auth
# ---------------------------------------------------------------------------


def test_failure_message_is_pii_free(token, no_override):
    fault = {
        "Fault": {
            "Error": [{"Message": "Duplicate", "Detail": "Vendor 12-3456789 at 1 Main St"}],
        }
    }
    qbo = _Qbo(post=_resp(400, fault))
    result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success
    assert result.message == "QuickBooks Online post failed: HTTP 400 (invalid_request)"


def test_401_refreshes_once_and_retries(token, no_override):
    qbo = _Qbo()
    statuses = iter([401, 200])
    real_request = qbo.request

    async def flaky(method, url, **kw):
        if url.endswith("/preferences"):
            status = next(statuses)
            if status == 401:
                qbo.calls.append({"method": method, "url": url, **kw})
                return _resp(401, {})
        return await real_request(method, url, **kw)

    qbo.request = flaky
    current = {"token": "tok-1"}

    async def stored_token(spec, config, *, rejected_token=None):
        if rejected_token == current["token"]:
            current["token"] = "tok-2"  # the refresh the 401 asked for
        return current["token"]

    with patch.object(
        erp_oauth, "get_access_token", new=AsyncMock(side_effect=stored_token)
    ) as tokens:
        result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.success
    assert tokens.await_args_list[1].kwargs == {"rejected_token": "tok-1"}
    assert qbo.calls[1]["headers"]["Authorization"] == "Bearer tok-2"


def test_a_second_401_is_reported_not_retried_forever(token, no_override):
    calls = []

    async def always_401(method, url, **kw):
        calls.append(url)
        return _resp(401, {})

    qbo = _Qbo()
    qbo.request = always_401
    with patch.object(erp_oauth, "get_access_token", new=AsyncMock(return_value="tok")):
        result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert result.message == "QuickBooks Online post failed: HTTP 401 (unauthorized)"
    assert len(calls) == 2


def test_not_connected_is_a_failed_result(no_override):
    qbo = _Qbo()
    with patch.object(
        erp_oauth,
        "get_access_token",
        new=AsyncMock(side_effect=erp_oauth.ErpNotConnectedError("quickbooks_online")),
    ):
        result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success
    assert result.message == "QuickBooks Online post refused: not_connected"
    assert result.retryable is False
    assert qbo.calls == []


def test_token_refresh_outage_is_a_plain_retryable_failure(no_override):
    qbo = _Qbo()
    outage = erp_oauth.ErpTokenRefreshError("quickbooks_online", "HTTP 503")
    with patch.object(erp_oauth, "get_access_token", new=AsyncMock(side_effect=outage)):
        result = _run_with(qbo, lambda: _adapter().post_invoice(_payload()))
    assert not result.success
    assert result.message == "quickbooks_online: token refresh failed (HTTP 503)"
    assert result.retryable is True


def test_no_realm_is_not_connected(no_override):
    adapter = _adapter({**CONFIG, "oauth": {}})
    with pytest.raises(erp_oauth.ErpNotConnectedError):
        adapter._company_url("bill")


# ---------------------------------------------------------------------------
# status + void
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "total", "expected"),
    [
        ("0", "100.10", ErpInvoiceStatus.paid),
        ("0.10", "100.10", ErpInvoiceStatus.partially_paid),
        ("100.10", "100.10", ErpInvoiceStatus.open),
    ],
)
def test_status_mapping(token, no_override, balance, total, expected):
    bill = {"Id": "145", "Balance": Decimal(balance), "TotalAmt": Decimal(total)}
    qbo = _Qbo(bill=bill)
    assert _run_with(qbo, lambda: _adapter().get_invoice_status("145")) == expected


def test_status_unknown_when_bill_missing(token, no_override):
    assert _run_with(_Qbo(), lambda: _adapter().get_invoice_status("1")) == ErpInvoiceStatus.unknown


def test_void_deletes_an_untouched_bill(token, no_override):
    bill = {"Id": "145", "SyncToken": "3", "Balance": 100.1, "TotalAmt": 100.1}
    qbo = _Qbo(bill=bill)
    assert _run_with(qbo, lambda: _adapter().void_invoice("145")) is True
    (post,) = qbo.deletes()
    assert post["params"] == {"minorversion": "75", "operation": "delete"}
    assert json.loads(post["content"]) == {"Id": "145", "SyncToken": "3"}


def test_void_refuses_a_bill_with_a_payment_applied(token, no_override):
    bill = {"Id": "145", "SyncToken": "3", "Balance": 40, "TotalAmt": 100.1}
    qbo = _Qbo(bill=bill)
    assert _run_with(qbo, lambda: _adapter().void_invoice("145")) is False
    assert qbo.posts() == []


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _query_router(pages: dict[str, list[list[dict]]]):
    seen: list[str] = []

    async def request(method, url, params=None, **kw):
        q = params["query"]
        seen.append(q)
        entity = q.split(" from ")[1].split()[0]
        start = int(q.split("STARTPOSITION ")[1].split()[0])
        idx = (start - 1) // 1000
        rows = pages[entity][idx] if idx < len(pages[entity]) else []
        return _resp(200, {"QueryResponse": {entity: rows}})

    return request, seen


def test_list_vendors_pages_and_maps(token, no_override):
    page1 = [{"Id": str(i), "DisplayName": f"V{i}"} for i in range(1000)]
    page2 = [
        {
            "Id": "2000",
            "DisplayName": "Acme Ltd",
            "PrimaryEmailAddr": {"Address": "ap@acme.test"},
            "PrimaryPhone": {"FreeFormNumber": "555"},
            "TaxIdentifier": "XXXXXX6789",
            "TermRef": {"value": "3", "name": "Net 30"},
            "BillAddr": {"Line1": "1 Main", "City": "Austin"},
        }
    ]
    qbo = _Qbo()
    qbo.request, seen = _query_router({"Vendor": [page1, page2]})
    vendors = _run_with(qbo, lambda: _adapter().list_vendors())
    assert len(vendors) == 1001
    assert seen[1] == "select * from Vendor STARTPOSITION 1001 MAXRESULTS 1000"
    acme = vendors[-1]
    assert (acme.erp_vendor_id, acme.name, acme.email, acme.payment_terms) == (
        "2000",
        "Acme Ltd",
        "ap@acme.test",
        "Net 30",
    )
    assert acme.tax_id is None  # QuickBooks returns it masked
    assert acme.address == "1 Main, Austin"


def test_list_gl_accounts_maps_classification_and_parent(token, no_override):
    rows = [
        {"Id": "7", "Name": "Office", "AcctNum": "6100", "Classification": "Expense"},
        {"Id": "8", "Name": "Toner", "Classification": "Expense", "ParentRef": {"value": "7"}},
        {"Id": "33", "Name": "A/P", "Classification": "Liability"},
    ]
    qbo = _Qbo()
    qbo.request, _ = _query_router({"Account": [rows]})
    accounts = _run_with(qbo, lambda: _adapter().list_gl_accounts())
    by_id = {a.erp_account_id: a for a in accounts}
    assert by_id["7"].code == "6100" and by_id["7"].account_type == "expense"
    assert by_id["8"].code == "Toner" and by_id["8"].parent_code == "6100"
    assert by_id["33"].account_type == "liability"


def test_list_pos_is_exact(token, no_override):
    po = {
        "Id": "130",
        "DocNumber": "PO-1",
        "POStatus": "Closed",
        "TotalAmt": Decimal("250.10"),
        "VendorRef": {"value": "56", "name": "Acme"},
        "CurrencyRef": {"value": "USD"},
        "Line": [
            {
                "Amount": Decimal("250.10"),
                "Description": "Widgets",
                "ItemBasedExpenseLineDetail": {"Qty": 3, "UnitPrice": Decimal("83.3666")},
            },
            {"DetailType": "SubTotalLineDetail", "Amount": 250.1},
        ],
    }
    qbo = _Qbo()
    qbo.request, _ = _query_router({"PurchaseOrder": [[po]]})
    (got,) = _run_with(qbo, lambda: _adapter().list_pos())
    assert got.po_number == "PO-1" and got.status == "closed" and got.currency == "USD"
    assert got.total == Decimal("250.10")
    (line,) = got.line_items
    assert line.unit_price == Decimal("83.3666") and line.quantity == Decimal("3")


def test_read_failure_raises_instead_of_an_empty_sync(token, no_override):
    async def fail(method, url, **kw):
        return _resp(500, {"Fault": {"Error": [{"Detail": "LEAKY"}]}})

    qbo = _Qbo()
    qbo.request = fail
    with pytest.raises(RuntimeError) as exc:
        _run_with(qbo, lambda: _adapter().list_vendors())
    assert "LEAKY" not in str(exc.value)


def test_test_connection_reads_company_info(token, no_override):
    qbo = _Qbo()
    assert _run_with(qbo, lambda: _adapter().test_connection()) is True
    assert qbo.calls[0]["url"].endswith(f"/companyinfo/{REALM}")


# ---------------------------------------------------------------------------
# base URL
# ---------------------------------------------------------------------------


def test_sandbox_environment_uses_the_sandbox_host(no_override):
    adapter = _adapter({**CONFIG, "environment": "sandbox"})
    assert adapter._company_url("bill") == (
        f"https://sandbox-quickbooks.api.intuit.com/v3/company/{REALM}/bill"
    )


def test_operator_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "erp_qbo_api_base", "http://localhost:12112/qbo/")
    adapter = _adapter({**CONFIG, "environment": "sandbox"})
    assert adapter._company_url("bill") == f"http://localhost:12112/qbo/v3/company/{REALM}/bill"
