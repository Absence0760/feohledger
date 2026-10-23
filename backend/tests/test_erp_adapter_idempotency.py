"""Real ERP adapters are idempotent on `payload.correlation_id` (issue #143).

`erp.py::send_to_erp_internal`'s 3-attempt retry loop means a client-side timeout AFTER
the ERP already accepted a create can otherwise retry into a SECOND vendor
bill for the same invoice. Each real adapter's `post_invoice` must be
retry-safe via whichever mechanism its target ERP actually supports:

  - `merge_dev`  — an `X-Idempotency-Key` header on the create POST.
  - `netsuite`   — a pre-create lookup by `externalId` (NetSuite enforces
    uniqueness on it); a match short-circuits to success WITHOUT posting.
  - `dynamics_365_bc` — a pre-create lookup by vendor + `vendorInvoiceNumber`; same
    short-circuit shape.

HTTP is mocked with the same `patch("httpx.AsyncClient")` style as
`test_erp_base_url_overrides.py` — no live fake-erp container required for
these unit tests (see `frontend/tests-e2e/erp/*.spec.ts` + `pnpm test:erp`
for the full live-fake-erp end-to-end coverage of the same behavior).
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.erp_adapters.base import InvoicePayload, LineItemPayload
from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter
from app.services.erp_adapters.merge_dev import MergeDevAdapter
from app.services.erp_adapters.netsuite import NetSuiteAdapter


def _run(coro):
    return asyncio.run(coro)


def _mock_response(status: int, body: dict | None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}" if body is not None else b""
    resp.json = MagicMock(return_value=body or {})
    resp.headers = {"content-type": "application/json", **(headers or {})}
    resp.raise_for_status = MagicMock()
    return resp


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-idem-1",
        vendor_erp_id="00000000-0000-0000-0000-0000000000a1",
        gl_account_erp_id="ERP-6000",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _ns_bill(total: str | None, status: str = "open") -> MagicMock:
    """A NetSuite ``GET /vendorBill/{id}`` response carrying ``total`` as a
    JSON number literal (the adapter parses the raw bytes, never a float)."""
    fields = [f'"id": "42", "status": {{"id": "{status}"}}']
    if total is not None:
        fields.append(f'"total": {total}')
    resp = _mock_response(200, {})
    resp.content = ("{" + ", ".join(fields) + "}").encode()
    return resp


def _ns_adapter() -> NetSuiteAdapter:
    return NetSuiteAdapter(
        {
            "account_id": "123456",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )


_NS_MISS = _mock_response(200, {"items": [], "count": 0})
_NS_CREATED = _mock_response(204, None, headers={"Location": "https://x/vendorBill/42"})


# ---------------------------------------------------------------------------
# merge_dev — Idempotency-Key header
# ---------------------------------------------------------------------------


def test_merge_dev_sends_idempotency_key_header_on_create():
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(201, {"model": {"id": "m1"}}))
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-abc")))

    assert result.success
    headers = client.post.await_args.kwargs["headers"]
    assert headers["X-Idempotency-Key"] == "corr-abc"


def test_merge_dev_get_invoice_status_does_not_send_idempotency_key():
    """Only the create call is idempotency-keyed — a GET has no request body
    to de-dupe and must not carry a stray key."""
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"status": "OPEN"}))
        _run(adapter.get_invoice_status("doc-1"))

    headers = client.get.await_args.kwargs["headers"]
    assert "X-Idempotency-Key" not in headers


# ---------------------------------------------------------------------------
# netsuite — pre-create lookup by externalId
# ---------------------------------------------------------------------------


def test_netsuite_post_invoice_short_circuits_when_external_id_already_exists():
    """A retried push finds the already-created bill by externalId and never
    issues the POST at all — the strongest possible guarantee against a
    duplicate (issue #143's exact failure scenario). It still counts only once
    the found bill's total reads back as the approved amount."""
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"items": [{"id": "9001"}], "count": 1}),
                _ns_bill("100.00"),
            ]
        )
        client.post = AsyncMock(
            side_effect=AssertionError("must not POST when externalId already exists")
        )
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-existing")))

    assert result.success, result.message
    assert result.erp_document_id == "9001"
    assert "idempotent" in result.message.lower()
    client.post.assert_not_awaited()

    # The lookup query carries the correlation_id as the externalId filter.
    lookup_url = client.get.await_args_list[0].args[0]
    assert "externalId" in lookup_url
    assert "corr-existing" in lookup_url
    # Then the found bill itself is read back.
    assert client.get.await_args_list[1].args[0].endswith("/vendorBill/9001")


def test_netsuite_idempotent_hit_with_a_different_total_is_not_success():
    """An earlier attempt's bill that NetSuite booked at a different total (a
    tax code added tax) is not reported as posted on the retry either."""
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"items": [{"id": "9001"}], "count": 1}),
                _ns_bill("110.00"),
                # void_invoice's own status read: approved, so left alone.
                _mock_response(200, {"status": {"id": "open"}}),
            ]
        )
        client.post = AsyncMock()
        client.delete = AsyncMock()
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-existing")))

    assert result.success is False
    assert result.retryable is False
    assert result.message.startswith("NetSuite post failed: posted_total_mismatch")
    assert "could not be deleted" in result.message
    client.post.assert_not_awaited()
    client.delete.assert_not_awaited()


def test_netsuite_idempotent_hit_with_no_total_is_unconfirmed():
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"items": [{"id": "9001"}], "count": 1}),
                _ns_bill(None),
            ]
        )
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-existing")))

    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "9001"
    assert result.message.startswith("NetSuite post unconfirmed: posted_total_unconfirmed")


def test_netsuite_created_bill_whose_total_differs_is_deleted_while_pending():
    """A tax-enabled account adds tax to the expense lines: 100.00 sent,
    110.00 booked. The bill is still pending approval (nothing has hit the GL),
    so it is deleted, and the push fails non-retryably saying so."""
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _NS_MISS,
                _ns_bill("110.00", status="pendingApproval"),
                _mock_response(200, {"status": {"id": "pendingApproval"}}),
            ]
        )
        client.post = AsyncMock(return_value=_NS_CREATED)
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is False
    assert result.message == "NetSuite post failed: posted_total_mismatch (the bill was deleted)"
    client.delete.assert_awaited_once()
    assert client.delete.await_args.args[0].endswith("/vendorBill/42")


def test_netsuite_created_bill_whose_total_differs_is_left_when_approved():
    """An approved bill has posted to the GL; it is never deleted, and the
    message says it was not."""
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _NS_MISS,
                _ns_bill("110.00"),
                _mock_response(200, {"status": {"id": "open"}}),
            ]
        )
        client.post = AsyncMock(return_value=_NS_CREATED)
        client.delete = AsyncMock()
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "42"
    assert result.message.startswith("NetSuite post failed: posted_total_mismatch (")
    assert "could not be deleted" in result.message
    client.delete.assert_not_awaited()


def test_netsuite_created_bill_compares_its_total_exactly():
    """100.00 and 100.0 are the same amount; 100.001 is not."""
    for total, ok in (("100.0", True), ("100", True), ("100.001", False)):
        with patch("httpx.AsyncClient") as cm:
            client = cm.return_value.__aenter__.return_value
            client.get = AsyncMock(
                side_effect=[
                    _NS_MISS,
                    _ns_bill(total),
                    _mock_response(200, {"status": {"id": "open"}}),
                ]
            )
            client.post = AsyncMock(return_value=_NS_CREATED)
            client.delete = AsyncMock()
            result = _run(_ns_adapter().post_invoice(_payload()))
        assert result.success is ok, (total, result.message)


def test_netsuite_created_bill_that_cannot_be_read_back_is_unconfirmed():
    """No Location, a failed read, or a record with no usable total: never
    success, and nothing is deleted."""
    no_location = _mock_response(204, None)
    cases = [
        (no_location, [_NS_MISS]),
        (_NS_CREATED, [_NS_MISS, _mock_response(404, None)]),
        (_NS_CREATED, [_NS_MISS, _ns_bill(None)]),
        (_NS_CREATED, [_NS_MISS, _ns_bill('"not-a-number"')]),
    ]
    for created, gets in cases:
        with patch("httpx.AsyncClient") as cm:
            client = cm.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=gets)
            client.post = AsyncMock(return_value=created)
            client.delete = AsyncMock()
            result = _run(_ns_adapter().post_invoice(_payload()))
        assert result.success is False
        assert result.retryable is False
        assert result.message == (
            "NetSuite post unconfirmed: posted_total_unconfirmed "
            "(the bill was created but NetSuite reported no total)"
        )
        client.delete.assert_not_awaited()


def test_netsuite_post_invoice_proceeds_to_create_when_no_match():
    """The normal (first-attempt) path: no existing bill found → POST as
    before. Proves the idempotency check doesn't break ordinary creates."""
    adapter = _ns_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[_NS_MISS, _ns_bill("100.00")])
        client.post = AsyncMock(return_value=_NS_CREATED)
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-new")))

    assert result.success, result.message
    assert result.erp_document_id == "42"
    assert client.get.await_count == 2  # the lookup, then the read-back
    client.post.assert_awaited_once()
    # Synchronous create: `respond-async` would answer with a job, not the bill.
    assert "Prefer" not in client.post.await_args.kwargs["headers"]
    # The bill names its vendor (`entity`) and the expense line's account by
    # NetSuite internal id — never `refName` text.
    import json

    body = json.loads(client.post.await_args.kwargs["content"])
    assert body["entity"] == {"id": "00000000-0000-0000-0000-0000000000a1"}
    assert body["expense"]["items"] == [
        {"account": {"id": "ERP-6000"}, "amount": 100.00, "memo": "INV-1"}
    ]
    assert "item" not in body


def test_netsuite_post_invoice_fails_retryably_when_lookup_fails():
    """A non-200 on the lookup is not a miss. Reading it as one re-creates the
    duplicate bill this lookup exists to prevent: the lookup fails exactly when
    NetSuite is struggling, which is when the first attempt's response was most
    likely lost. So the push fails — retryably, with a PII-free message — and
    the retry looks again (Sage Intacct and SYSPRO behave the same)."""
    adapter = NetSuiteAdapter(
        {
            "account_id": "123456",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(500, None))
        client.post = AsyncMock()
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-lookup-fails")))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "NetSuite post failed: HTTP 500 (provider_error)"
    client.post.assert_not_awaited()


def test_d365_post_invoice_fails_retryably_when_lookup_fails():
    """Same rule for Business Central's vendor + vendorInvoiceNumber lookup."""
    adapter = _bc_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                AssertionError("must not create when the lookup failed"),
            ]
        )
        client.get = AsyncMock(return_value=_mock_response(429, None))
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-bc-lookup-fails")))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "Business Central post failed: HTTP 429 (rate_limited)"
    assert client.post.await_count == 1  # the token exchange only


# ---------------------------------------------------------------------------
# dynamics_365_bc — pre-create lookup by vendor + vendorInvoiceNumber
# ---------------------------------------------------------------------------


def _bc_adapter() -> BusinessCentralAdapter:
    adapter = BusinessCentralAdapter(
        {
            "tenant_id": "tid-1",
            "client_id": "cid",
            "client_secret": "sec",
            "environment": "sandbox",
            "company_id": "1b2c3d4e-0000-0000-0000-00000000c001",
            "base_url": "https://api.businesscentral.dynamics.com/v2.0",
        }
    )
    return adapter


def test_d365_post_invoice_short_circuits_when_the_vendor_invoice_number_already_exists(
    monkeypatch,
):
    """An Open invoice found by the lookup is a success — once BC's own total,
    re-read exactly, is the approved amount."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),  # token exchange
                AssertionError("must not create when the invoice already exists"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-doc-1", "status": "Open"}]}),
                _bc_record("bc-doc-1", "Open", Decimal("100.00")),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("a posted invoice is never deleted"))
        result = _run(adapter.post_invoice(_payload(invoice_number="inv-o'brien-1")))

    assert result.success
    assert result.erp_document_id == "bc-doc-1"
    assert "idempotent" in result.message.lower()
    assert client.get.await_count == 2
    # Only the token exchange POST happened — no purchaseInvoices create.
    assert client.post.await_count == 1

    # API v2.0's purchaseInvoice has no externalDocumentNumber: the key is the
    # vendor and its Vendor Invoice No. — a Code field, so upper-cased, with an
    # OData-doubled quote.
    filter_params = client.get.await_args_list[0].kwargs["params"]
    assert filter_params["$filter"] == (
        "vendorId eq 00000000-0000-0000-0000-0000000000a1 "
        "and vendorInvoiceNumber eq 'INV-O''BRIEN-1'"
    )


def _bc_record(
    doc_id: str,
    status: str,
    total: Decimal | None,
    etag: str = 'W/"e1"',
    *,
    excluding: Decimal | None = None,
    tax: Decimal | None = None,
):
    """A purchaseInvoice read, with its body on the wire exactly as BC sends it.
    ``excluding`` / ``tax`` are its ``totalAmountExcludingTax`` /
    ``totalTaxAmount``, stated only when given."""
    from app.utils.json_money import dumps_exact_json

    record = {"id": doc_id, "number": "PI-9", "status": status, "@odata.etag": etag}
    if total is not None:
        record["totalAmountIncludingTax"] = total
    if excluding is not None:
        record["totalAmountExcludingTax"] = excluding
    if tax is not None:
        record["totalTaxAmount"] = tax
    resp = _mock_response(200, record)
    resp.content = dumps_exact_json(record).encode()
    return resp


def _offline_bc(monkeypatch) -> BusinessCentralAdapter:
    """The operator overrides keep token + API calls off the network (and the
    SSRF guard's DNS lookup out of a unit test)."""
    from app.config import settings

    monkeypatch.setattr(settings, "erp_d365_api_base", "http://fake-erp:12112/d365")
    monkeypatch.setattr(settings, "erp_d365_token_url", "http://fake-erp:12112/token")
    return _bc_adapter()


def test_d365_post_invoice_proceeds_to_create_when_no_match(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    create_resp = _mock_response(201, {"id": "bc-doc-2", "number": "PI-1"})
    post_finalize_resp = _mock_response(204, None)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),  # token exchange
                create_resp,  # create purchase invoice
                post_finalize_resp,  # Microsoft.NAV.post finalize
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),  # lookup: miss
                _bc_record("bc-doc-2", "Draft", Decimal("100.00")),  # the draft's total
                _bc_record("bc-doc-2", "Open", Decimal("100.00")),  # the posted total
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("a matching draft is posted"))
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-bc-new")))

    assert result.success
    assert result.erp_document_id == "bc-doc-2"
    assert client.get.await_count == 3
    assert client.get.await_args_list[2].args[0].endswith("purchaseInvoices(bc-doc-2)")
    assert client.post.await_count == 3
    assert (
        client.post.await_args_list[2]
        .args[0]
        .endswith("purchaseInvoices(bc-doc-2)/Microsoft.NAV.post")
    )
    # The purchase invoice names its vendor by BC id. `vendorNumber` holds a
    # vendor NUMBER, and the name we used to send there matched nothing — or
    # another vendor whose number happened to equal it.
    import json

    body = json.loads(client.post.await_args_list[1].kwargs["content"])
    assert body["vendorId"] == "00000000-0000-0000-0000-0000000000a1"
    assert "vendorNumber" not in body
    # Each line on the G/L account's id from the chart sync, never its No.
    assert body["purchaseInvoiceLines"] == [
        {
            "lineType": "Account",
            "accountId": "ERP-6000",
            "description": "INV-1",
            "quantity": 1,
            "unitCost": 100.00,
        }
    ]


# ---------------------------------------------------------------------------
# dynamics_365_bc — the draft's total is checked before it is posted
# ---------------------------------------------------------------------------


def test_d365_refuses_and_deletes_a_draft_bc_taxed_above_the_approved_amount(monkeypatch):
    """A VAT company: 1,200 approved, BC adds 20% → a 1,440 draft. Posting it
    would book 1,440 and BC's own payment run would overpay by 240. The draft
    (never posted) is deleted with the etag just read, `Microsoft.NAV.post` is
    never called, and the refusal is final — a re-send gets the same tax."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-vat-1", "number": "PI-2"}),
                AssertionError("a mismatched draft must never be posted"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-vat-1", "Draft", Decimal("1440.00"), etag='W/"vat"'),
            ]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_payload(amount=Decimal("1200.00"))))

    assert result.success is False
    assert result.retryable is False
    assert result.message == "Business Central post refused: posted_total_mismatch"
    assert client.post.await_count == 2  # token + create; no post step
    client.delete.assert_awaited_once()
    assert client.delete.await_args.args[0].endswith("purchaseInvoices(bc-vat-1)")
    assert client.delete.await_args.kwargs["headers"]["If-Match"] == 'W/"vat"'


def test_d365_refuses_a_draft_with_no_stated_total(monkeypatch):
    """No `totalAmountIncludingTax` means the total cannot be checked: fail
    closed, exactly like a mismatch."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-nt", "number": "PI-3"}),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _bc_record("bc-nt", "Draft", None)]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_payload()))

    assert result.retryable is False
    assert result.message == "Business Central post refused: posted_total_mismatch"
    client.delete.assert_awaited_once()


def test_d365_a_failed_post_step_is_a_retryable_failure_not_a_success(monkeypatch):
    """The post step used to be `except Exception: pass` — the invoice moved to
    `sent_to_erp` while BC held only a draft. Now it fails, retryably, with a
    PII-free message, and the retry's lookup finds the draft to finish."""
    adapter = _offline_bc(monkeypatch)
    failed_post = _mock_response(500, {"error": {"message": "Vendor Acme, IBAN GB00..."}})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-p", "number": "PI-4"}),
                failed_post,
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-p", "Draft", Decimal("100.00")),
            ]
        )
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "Business Central post failed: HTTP 500 (provider_error)"


def test_d365_a_failed_draft_read_is_retryable_and_posts_nothing(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-r", "number": "PI-5"}),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _mock_response(503, None)]
        )
        client.delete = AsyncMock()
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "Business Central post failed: HTTP 503 (provider_error)"
    assert client.post.await_count == 2
    client.delete.assert_not_awaited()


def test_d365_lookup_hit_on_a_draft_rechecks_the_total_and_posts_it(monkeypatch):
    """A draft found by externalDocumentNumber is not "already posted": its
    total is re-checked and the post step re-run — no second create."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(204, None),  # Microsoft.NAV.post
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-d", "status": "Draft"}]}),
                _bc_record("bc-d", "Draft", Decimal("100.00")),
                _bc_record("bc-d", "Open", Decimal("100.00")),
            ]
        )
        result = _run(adapter.post_invoice(_payload()))

    assert result.success
    assert result.erp_document_id == "bc-d"
    assert (
        client.post.await_args_list[1].args[0].endswith("purchaseInvoices(bc-d)/Microsoft.NAV.post")
    )
    assert "content" not in client.post.await_args_list[1].kwargs  # no second create


def test_d365_lookup_hit_on_a_mismatched_draft_is_refused_and_left_in_bc(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                AssertionError("neither a create nor a post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-m", "status": "Draft"}]}),
                _bc_record("bc-m", "Draft", Decimal("120.00")),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("never delete a found draft"))
        result = _run(adapter.post_invoice(_payload()))

    # Found by vendor + number, so it may be a BC user's own draft: refused,
    # never deleted, and the message says it is still there.
    assert result.retryable is False
    assert result.message == (
        "Business Central post refused: posted_total_mismatch "
        "(the draft was not deleted in Business Central)"
    )
    client.delete.assert_not_awaited()


def _posted_after_a_failed_delete(adapter, *, lookup_status: str, total: Decimal | None):
    """The retry after a draft taxed above the approved amount could not be
    deleted and a BC user then posted it: the lookup finds it ``Open``."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                AssertionError("neither a create nor a post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-o", "status": lookup_status}]}),
                _bc_record("bc-o", "Open", total),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("a posted invoice is never deleted"))
        result = _run(adapter.post_invoice(_payload(amount=Decimal("1000.00"))))
    return result, client


def test_d365_lookup_hit_on_an_open_invoice_with_the_wrong_total_is_refused(monkeypatch):
    """1,000 approved; the draft came to 1,150; the DELETE failed; a BC user
    posted it. The manual retry used to report that as success, and our
    payment run would then pay 1,000 against a 1,150 bill. Now it is refused,
    finally, and the posted invoice is left for an accountant in BC."""
    result, client = _posted_after_a_failed_delete(
        _offline_bc(monkeypatch), lookup_status="Open", total=Decimal("1150.00")
    )

    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "bc-o"
    assert result.message == (
        "Business Central post refused: posted_total_mismatch "
        "(the invoice is posted in Business Central and was not deleted)"
    )
    client.delete.assert_not_awaited()


def test_d365_lookup_hit_on_a_paid_invoice_with_the_wrong_total_is_refused(monkeypatch):
    result, _ = _posted_after_a_failed_delete(
        _offline_bc(monkeypatch), lookup_status="Paid", total=Decimal("1150.00")
    )
    assert result.success is False
    assert "posted_total_mismatch" in result.message


def test_d365_an_open_invoice_with_no_stated_total_is_unconfirmed_not_success(monkeypatch):
    result, _ = _posted_after_a_failed_delete(
        _offline_bc(monkeypatch), lookup_status="Open", total=None
    )
    assert result.success is False
    assert result.retryable is False
    assert result.message.startswith("Business Central post refused: posted_total_unconfirmed")


def test_d365_a_draft_posted_between_create_and_read_has_its_total_checked(monkeypatch):
    """The fresh read finds the new invoice already ``Open`` (a BC user posted
    it in between): success only at the approved total."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-x", "number": "PI-6"}),
                AssertionError("an Open invoice is never posted again"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-x", "Open", Decimal("120.00")),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("a posted invoice is never deleted"))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is False
    assert "posted_total_mismatch" in result.message
    assert "was not deleted" in result.message


def _mismatched_new_draft(adapter, *, read_etag, create_body, delete_status=204):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, create_body),
                AssertionError("a mismatched draft must never be posted"),
            ]
        )
        record = _bc_record("bc-e", "Draft", Decimal("120.00"))
        if read_etag is None:
            from app.utils.json_money import dumps_exact_json

            body = {"id": "bc-e", "status": "Draft", "totalAmountIncludingTax": Decimal("120.00")}
            record.content = dumps_exact_json(body).encode()
        client.get = AsyncMock(side_effect=[_mock_response(200, {"value": []}), record])
        client.delete = AsyncMock(return_value=_mock_response(delete_status, None))
        result = _run(adapter.post_invoice(_payload()))
    return result, client


def test_d365_a_failed_draft_delete_is_reported_not_ignored(monkeypatch):
    """The DELETE's status used to be ignored, so the refusal read the same
    whether the draft was gone or still sitting in BC for someone to post."""
    result, client = _mismatched_new_draft(
        _offline_bc(monkeypatch),
        read_etag='W/"e1"',
        create_body={"id": "bc-e", "number": "PI-7"},
        delete_status=412,
    )
    client.delete.assert_awaited_once()
    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "bc-e"
    assert result.message == (
        "Business Central post refused: posted_total_mismatch "
        "(the draft was not deleted in Business Central)"
    )


def test_d365_draft_delete_falls_back_to_the_create_responses_etag(monkeypatch):
    result, client = _mismatched_new_draft(
        _offline_bc(monkeypatch),
        read_etag=None,
        create_body={"id": "bc-e", "number": "PI-7", "@odata.etag": 'W/"from-create"'},
    )
    assert client.delete.await_args.kwargs["headers"]["If-Match"] == 'W/"from-create"'
    assert result.message == "Business Central post refused: posted_total_mismatch"


def test_d365_never_deletes_a_draft_with_a_wildcard_etag(monkeypatch):
    """``If-Match: *`` deletes whatever is there, posted in between or not. With
    no real etag the DELETE is not sent, and the refusal says so."""
    result, client = _mismatched_new_draft(
        _offline_bc(monkeypatch), read_etag=None, create_body={"id": "bc-e", "number": "PI-7"}
    )
    client.delete.assert_not_awaited()
    assert result.retryable is False
    assert result.message.endswith("(the draft was not deleted in Business Central)")


def test_d365_lookup_hit_on_a_cancelled_invoice_is_refused_not_recreated(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                AssertionError("a cancelled invoice is never re-created"),
            ]
        )
        client.get = AsyncMock(
            return_value=_mock_response(200, {"value": [{"id": "bc-c", "status": "Canceled"}]})
        )
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    assert result.retryable is False
    assert result.message == "Business Central post refused: existing_invoice_not_open"


# ---------------------------------------------------------------------------
# dynamics_365_bc — the posted invoice's total is read back
# ---------------------------------------------------------------------------


def _bc_post_then_read(adapter, posted_read):
    """A fresh create whose draft matches, a 204 post, then ``posted_read``."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-pr", "number": "PI-8"}),
                _mock_response(204, None),  # Microsoft.NAV.post
                AssertionError("one create, one post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-pr", "Draft", Decimal("100.00")),
                posted_read,
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("a posted invoice is never deleted"))
        result = _run(adapter.post_invoice(_payload()))
    return result, client


def test_d365_a_draft_edited_between_the_check_and_the_post_is_refused(monkeypatch):
    """The draft read 100.00, then a BC user added a line before our post ran:
    BC posted 130.00. Reporting success would let our payment run pay 100.00
    against a 130.00 bill. The posted invoice is read back and refused, and
    left in BC for an accountant — never deleted."""
    result, client = _bc_post_then_read(
        _offline_bc(monkeypatch), _bc_record("bc-pr", "Open", Decimal("130.00"))
    )
    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "bc-pr"
    assert result.refusal_code == "posted_total_mismatch"
    assert result.message == (
        "Business Central post refused: posted_total_mismatch "
        "(the invoice is posted in Business Central and was not deleted)"
    )
    client.delete.assert_not_awaited()


def test_d365_a_posted_invoice_with_no_stated_total_is_unconfirmed(monkeypatch):
    result, _ = _bc_post_then_read(_offline_bc(monkeypatch), _bc_record("bc-pr", "Open", None))
    assert result.success is False
    assert result.retryable is False
    assert result.refusal_code == "posted_total_unconfirmed"


def test_d365_a_failed_read_after_the_post_is_retryable_never_success(monkeypatch):
    """The post went through; whatever the read answered, the push is
    retryable, and the retry's lookup finds the posted invoice and checks it."""
    for status in (503, 404):
        result, _ = _bc_post_then_read(_offline_bc(monkeypatch), _mock_response(status, None))
        assert result.success is False
        assert result.retryable is True
        assert result.erp_document_id == "bc-pr"


def test_d365_a_draft_still_unposted_after_the_post_is_retryable(monkeypatch):
    result, _ = _bc_post_then_read(
        _offline_bc(monkeypatch), _bc_record("bc-pr", "Draft", Decimal("100.00"))
    )
    assert result.success is False
    assert result.retryable is True


def test_d365_an_invoice_in_review_after_the_post_is_refused(monkeypatch):
    """A BC approval workflow can leave the invoice ``In Review``: not posted,
    and not ours to finish."""
    result, _ = _bc_post_then_read(
        _offline_bc(monkeypatch), _bc_record("bc-pr", "In Review", Decimal("100.00"))
    )
    assert result.success is False
    assert result.retryable is False
    assert result.erp_document_id == "bc-pr"
    assert result.message == "Business Central post refused: existing_invoice_not_open"


# ---------------------------------------------------------------------------
# dynamics_365_bc — a VAT company: the draft is repriced to the net amounts
# ---------------------------------------------------------------------------


def _vat_payload(**overrides):
    """100.00 net + 20.00 VAT = 120.00 approved, one header line."""
    return _payload(
        amount=Decimal("120.00"), tax_amount=Decimal("20.00"), line_items=[], **overrides
    )


def _gross_draft_taxed_on_top(doc_id="bc-g", etag='W/"gross"'):
    """BC put 20% on top of the 120.00 gross line: 120.00 + 24.00."""
    return _bc_record(
        doc_id,
        "Draft",
        Decimal("144.00"),
        etag,
        excluding=Decimal("120.00"),
        tax=Decimal("24.00"),
    )


def test_d365_a_vat_company_gets_the_net_amounts_and_posts_the_approved_gross(monkeypatch):
    """The gross draft comes back taxed on top (144.00). It is deleted with its
    own etag and created again at the net 100.00; BC's 20% brings that to the
    approved 120.00, which is posted and read back."""
    import json

    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),  # gross draft
                _mock_response(201, {"id": "bc-n", "number": "PI-2"}),  # net draft
                _mock_response(204, None),  # Microsoft.NAV.post (the net draft)
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _gross_draft_taxed_on_top(),
                _bc_record(
                    "bc-n",
                    "Draft",
                    Decimal("120.00"),
                    excluding=Decimal("100.00"),
                    tax=Decimal("20.00"),
                ),
                _bc_record("bc-n", "Open", Decimal("120.00")),
            ]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.success, result.message
    assert result.erp_document_id == "bc-n"
    client.delete.assert_awaited_once()
    assert client.delete.await_args.args[0].endswith("purchaseInvoices(bc-g)")
    assert client.delete.await_args.kwargs["headers"]["If-Match"] == 'W/"gross"'
    gross = json.loads(client.post.await_args_list[1].kwargs["content"])
    net = json.loads(client.post.await_args_list[2].kwargs["content"])
    assert [line["unitCost"] for line in gross["purchaseInvoiceLines"]] == [120.00]
    assert [line["unitCost"] for line in net["purchaseInvoiceLines"]] == [100.00]
    # The second draft is the same invoice: same correlation id, so a retry's
    # lookup finds it, and no tax field is sent (BC computes every one).
    assert net["vendorInvoiceNumber"] == gross["vendorInvoiceNumber"]
    assert "externalDocumentNumber" not in gross
    assert set(net["purchaseInvoiceLines"][0]) == set(gross["purchaseInvoiceLines"][0])
    assert (
        client.post.await_args_list[3].args[0].endswith("purchaseInvoices(bc-n)/Microsoft.NAV.post")
    )


def test_d365_a_vat_rate_other_than_the_suppliers_is_refused_after_one_reprice(monkeypatch):
    """BC's rate gives 21.00 on the 100.00 net, not the supplier's 20.00: the
    net draft is 121.00. It is deleted and refused, never repriced again and
    never posted."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),
                _mock_response(201, {"id": "bc-n", "number": "PI-2"}),
                AssertionError("no third draft and no post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _gross_draft_taxed_on_top(),
                _bc_record(
                    "bc-n",
                    "Draft",
                    Decimal("121.00"),
                    'W/"net"',
                    excluding=Decimal("100.00"),
                    tax=Decimal("21.00"),
                ),
            ]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.success is False
    assert result.retryable is False
    assert result.message == "Business Central post refused: posted_total_mismatch"
    assert client.delete.await_count == 2
    assert client.delete.await_args.kwargs["headers"]["If-Match"] == 'W/"net"'


def test_d365_a_vat_invoice_with_only_a_header_tax_over_several_lines_is_refused(monkeypatch):
    """Two tax-inclusive lines and one header tax give no per-line net; a
    pro-rated split would invent one. The taxed draft is deleted and the push
    refused ``tax_not_itemised`` — nothing at another total is posted."""
    adapter = _offline_bc(monkeypatch)
    lines = [_bc_line(total=Decimal("60.00")), _bc_line(line_number=2, total=Decimal("60.00"))]
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),
                AssertionError("no net draft and no post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _gross_draft_taxed_on_top()]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(
            adapter.post_invoice(
                _payload(amount=Decimal("120.00"), tax_amount=Decimal("20.00"), line_items=lines)
            )
        )

    assert result.success is False
    assert result.retryable is False
    assert result.refusal_code == "tax_not_itemised"
    assert result.message == "Business Central post refused: tax_not_itemised"
    client.delete.assert_awaited_once()


def test_d365_an_untaxed_invoice_bc_taxes_on_top_is_refused_not_repriced(monkeypatch):
    """The invoice states no tax, so there is no net to send: BC's VAT on top
    is a mismatch, refused as before."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),
                AssertionError("no net draft and no post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _gross_draft_taxed_on_top()]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_payload(amount=Decimal("120.00"), line_items=[])))

    assert result.refusal_code == "posted_total_mismatch"
    client.delete.assert_awaited_once()


def test_d365_a_taxed_draft_that_cannot_be_deleted_is_not_duplicated(monkeypatch):
    """The gross draft could not be deleted, so no second draft is created (two
    drafts for one invoice): refused, saying the draft is still in BC."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),
                AssertionError("no second draft"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _gross_draft_taxed_on_top()]
        )
        client.delete = AsyncMock(return_value=_mock_response(412, None))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.retryable is False
    assert result.erp_document_id == "bc-g"
    assert result.message == (
        "Business Central post refused: posted_total_mismatch "
        "(the draft was not deleted in Business Central)"
    )


def test_d365_a_failed_net_create_is_retryable_with_nothing_left_in_bc(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-g", "number": "PI-1"}),
                _mock_response(503, {"error": {"message": "busy"}}),
            ]
        )
        client.get = AsyncMock(
            side_effect=[_mock_response(200, {"value": []}), _gross_draft_taxed_on_top()]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.success is False
    assert result.retryable is True
    assert result.message == "Business Central post failed: HTTP 503 (provider_error)"


def test_d365_the_retry_reprices_a_taxed_gross_draft_found_by_the_lookup(monkeypatch):
    """An earlier attempt died after the gross create. The lookup finds that
    draft taxed on top and the retry finishes the job: net draft, post."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-n", "number": "PI-2"}),
                _mock_response(204, None),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-g", "status": "Draft"}]}),
                _gross_draft_taxed_on_top(),
                _bc_record("bc-n", "Draft", Decimal("120.00"), excluding=Decimal("100.00")),
                _bc_record("bc-n", "Open", Decimal("120.00")),
            ]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.success, result.message
    assert result.erp_document_id == "bc-n"


def test_d365_a_net_draft_found_by_the_lookup_is_not_repriced_again(monkeypatch):
    """A draft already at the net amounts (100.00 excluding) whose BC tax is
    not the supplier's: refused, not deleted-and-recreated in a loop."""
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                AssertionError("no create and no post"),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": [{"id": "bc-n", "status": "Draft"}]}),
                _bc_record(
                    "bc-n",
                    "Draft",
                    Decimal("121.00"),
                    excluding=Decimal("100.00"),
                    tax=Decimal("21.00"),
                ),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("never delete a found draft"))
        result = _run(adapter.post_invoice(_vat_payload()))

    assert result.refusal_code == "posted_total_mismatch"
    assert result.message.endswith("(the draft was not deleted in Business Central)")


def test_bc_net_lines_are_each_lines_gross_less_its_own_tax():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    # Tax-exclusive lines with per-line tax: net = the line amounts.
    lines = _bc_invoice_lines(
        _payload(
            amount=Decimal("120.00"),
            tax_amount=Decimal("20.00"),
            line_items=[
                _bc_line(total=Decimal("60.00"), tax=Decimal("12.00")),
                _bc_line(line_number=2, total=Decimal("40.00"), tax=Decimal("8.00")),
            ],
        )
    )
    assert [line["unitCost"] for line in lines.gross] == [Decimal("72.00"), Decimal("48.00")]
    assert [line["unitCost"] for line in lines.net] == [Decimal("60.00"), Decimal("40.00")]
    assert lines.net_refusal is None

    # No tax stated: there is no net set to fall back to.
    untaxed = _bc_invoice_lines(_payload(line_items=[]))
    assert untaxed.net is None and untaxed.net_refusal is None

    # Several tax-inclusive lines, header tax only: never pro-rated.
    unsplit = _bc_invoice_lines(
        _payload(
            amount=Decimal("120.00"),
            tax_amount=Decimal("20.00"),
            line_items=[_bc_line(total=Decimal("60.00")), _bc_line(total=Decimal("60.00"))],
        )
    )
    assert unsplit.net is None
    assert unsplit.net_refusal == "tax_not_itemised"

    # A net finer than the currency is not rounded.
    fine = _bc_invoice_lines(
        _payload(amount=Decimal("100.00"), tax_amount=Decimal("16.665"), line_items=[])
    )
    assert fine.net is None
    assert fine.net_refusal == "line_amount_precision"


# ---------------------------------------------------------------------------
# dynamics_365_bc — the lookup key, and refusals coded from facts (ERP audit)
# ---------------------------------------------------------------------------


def test_d365_never_sends_external_document_number():
    """API v2.0's purchaseInvoice has no `externalDocumentNumber` (only a
    salesInvoice does): BC answers a `$filter` or a body naming it with 400, so
    every push used to fail at the lookup."""
    import inspect

    from app.services.erp_adapters import dynamics_365_bc

    assert "externalDocumentNumber" not in inspect.getsource(dynamics_365_bc).replace(
        "no ``externalDocumentNumber``", ""
    )


def test_d365_several_invoices_under_the_number_are_refused_not_guessed(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[_mock_response(200, {"access_token": "tok"}), AssertionError("no create")]
        )
        client.get = AsyncMock(
            return_value=_mock_response(
                200, {"value": [{"id": "a", "status": "Open"}, {"id": "b", "status": "Draft"}]}
            )
        )
        result = _run(adapter.post_invoice(_payload()))
    assert result.retryable is False
    assert result.refusal_code == "duplicate_document_number"


def test_d365_preflight_refusals_send_nothing():
    """An invoice number past BC's Code[35] and a vendor id that is not a BC
    GUID (it is spliced into the lookup's `$filter`) are refused before any
    request, `not_posted`."""
    for payload, code in (
        (_payload(invoice_number="X" * 36), "doc_number_too_long"),
        (_payload(vendor_erp_id="V0001"), "vendor_not_linked"),
        (_payload(vendor_erp_id="x' or 1 eq 1 or vendorId eq '"), "vendor_not_linked"),
    ):
        with patch("httpx.AsyncClient") as cm:
            result = _run(_bc_adapter().post_invoice(payload))
        cm.assert_not_called()
        assert result.refusal_code == code
        assert result.not_posted is True


def test_d365_a_company_name_in_company_id_is_refused_before_any_request(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_d365_api_base", "")
    adapter = BusinessCentralAdapter(
        {
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "environment": "sandbox",
            "company_id": "CRONUS USA, Inc.",
        }
    )
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(_payload()))
    cm.assert_not_called()
    assert result.retryable is False
    assert result.message == (
        "Business Central config 'company_id' must be the company's id (a GUID)"
    )


def test_bc_line_descriptions_are_cut_to_bcs_text_100():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    lines = _bc_invoice_lines(_payload(description="d" * 150, line_items=[]))
    assert lines.gross[0]["description"] == "d" * 100


def _bc_error(status: int, code: str, message: str):
    return _mock_response(status, {"error": {"code": code, "message": message}})


def _create_refused(adapter, gets, payload=None):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _bc_error(400, "Application_DialogException", "You cannot post: Acme, Main St."),
            ]
        )
        client.get = AsyncMock(side_effect=[_mock_response(200, {"value": []}), *gets])
        result = _run(adapter.post_invoice(payload or _payload()))
    return result, client


def _bc_json(body: dict):
    from app.utils.json_money import dumps_exact_json

    resp = _mock_response(200, body)
    resp.content = dumps_exact_json(body).encode()
    return resp


def test_d365_a_create_refused_for_a_blocked_vendor_is_coded_and_not_posted(monkeypatch):
    """BC refused the create, and the vendor reads back blocked `All`: coded
    `vendor_inactive` from that fact (decisions §285's rule), never from BC's
    message, which can name the vendor; nothing was created."""
    result, client = _create_refused(
        _offline_bc(monkeypatch), [_bc_json({"id": "v", "blocked": "All"})]
    )
    assert result.refusal_code == "vendor_inactive"
    assert result.not_posted is True
    assert result.retryable is False
    assert "Acme" not in result.message
    assert (
        client.get.await_args_list[1]
        .args[0]
        .endswith("vendors(00000000-0000-0000-0000-0000000000a1)")
    )


def test_d365_a_create_refused_for_an_unpostable_account_names_our_code(monkeypatch):
    account = "a6100000-0000-0000-0000-000000006100"
    payload = _payload(gl_account="6100", gl_account_erp_id=account, line_items=[])
    result, _ = _create_refused(
        _offline_bc(monkeypatch),
        [_bc_json({"id": "v", "blocked": " "}), _bc_json({"id": account, "directPosting": False})],
        payload,
    )
    assert result.refusal_code == "account_inactive"
    assert result.refusal_params == {"accounts": ["6100"]}
    assert result.not_posted is True


def test_d365_an_unexplained_create_refusal_is_uncoded_but_not_posted(monkeypatch):
    account = "a6100000-0000-0000-0000-000000006100"
    payload = _payload(gl_account_erp_id=account, line_items=[])
    result, _ = _create_refused(
        _offline_bc(monkeypatch),
        [_bc_json({"id": "v", "blocked": " "}), _bc_json({"id": account, "blocked": False})],
        payload,
    )
    assert result.refusal_code is None
    assert result.not_posted is True
    assert result.message == "Business Central post failed: HTTP 400 (invalid_request)"


def test_d365_a_post_refused_for_a_closed_period_deletes_the_draft(monkeypatch):
    """BC refuses `Microsoft.NAV.post` for a posting date outside the allowed
    range. Re-posting that draft cannot succeed, so it is deleted (with its
    etag) and the refusal is `period_closed`."""
    adapter = _offline_bc(monkeypatch)
    account = "a6100000-0000-0000-0000-000000006100"
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-pc", "number": "PI-1"}),
                _bc_error(
                    400,
                    "Application_DialogException",
                    "Posting Date is not within your range of allowed posting dates.",
                ),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-pc", "Draft", Decimal("100.00"), 'W/"pc"'),
                _bc_json({"id": "v", "blocked": " "}),
                _bc_json({"id": account, "blocked": False}),
            ]
        )
        client.delete = AsyncMock(return_value=_mock_response(204, None))
        result = _run(adapter.post_invoice(_payload(gl_account_erp_id=account, line_items=[])))

    assert result.refusal_code == "period_closed"
    assert result.retryable is False
    client.delete.assert_awaited_once()
    assert client.delete.await_args.kwargs["headers"]["If-Match"] == 'W/"pc"'


def test_d365_an_uncoded_post_refusal_keeps_the_draft_for_the_retry(monkeypatch):
    adapter = _offline_bc(monkeypatch)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-u", "number": "PI-1"}),
                _bc_error(400, "Application_DialogException", "Something else."),
            ]
        )
        client.get = AsyncMock(
            side_effect=[
                _mock_response(200, {"value": []}),
                _bc_record("bc-u", "Draft", Decimal("100.00")),
                _bc_json({"id": "v", "blocked": " "}),
                _bc_json({"id": "ERP-6000"}),
            ]
        )
        client.delete = AsyncMock(side_effect=AssertionError("kept for the retry"))
        result = _run(adapter.post_invoice(_payload(line_items=[])))

    assert result.refusal_code is None
    assert result.erp_document_id == "bc-u"
    assert result.message == "Business Central post failed: HTTP 400 (invalid_request)"


def test_d365_status_corrective_and_blank_are_unknown(monkeypatch):
    from app.services.erp_adapters.base import ErpInvoiceStatus

    adapter = _offline_bc(monkeypatch)
    for status, expected in (
        ("Corrective", ErpInvoiceStatus.unknown),
        (" ", ErpInvoiceStatus.unknown),
        ("Canceled", ErpInvoiceStatus.cancelled),
        ("Paid", ErpInvoiceStatus.paid),
    ):
        with patch("httpx.AsyncClient") as cm:
            client = cm.return_value.__aenter__.return_value
            client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
            client.get = AsyncMock(return_value=_mock_response(200, {"status": status}))
            assert _run(adapter.get_invoice_status("d")) == expected


# ---------------------------------------------------------------------------
# dynamics_365_bc — which lines a purchaseInvoice carries
# ---------------------------------------------------------------------------


def _bc_line(**overrides) -> LineItemPayload:
    base = dict(
        line_number=1, total=Decimal("60.00"), gl_account="6100", gl_account_erp_id="g-6100"
    )
    base.update(overrides)
    return LineItemPayload(**base)


def test_bc_lines_post_per_line_when_they_sum_to_the_amount():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    lines = _bc_invoice_lines(
        _payload(
            line_items=[
                _bc_line(description="Paper", quantity=Decimal(3), unit_price=Decimal("20.00")),
                # Uncoded: takes the header account.
                _bc_line(total=Decimal("40.00"), gl_account=None, gl_account_erp_id=None),
            ]
        )
    )
    # Each line is quantity 1 at its gross (bill_lines' amount), and an
    # undescribed line takes the invoice's number as its memo.
    assert lines.gross == [
        {
            "lineType": "Account",
            "accountId": "g-6100",
            "description": "Paper",
            "quantity": Decimal(1),
            "unitCost": Decimal("60.00"),
        },
        {
            "lineType": "Account",
            "accountId": "ERP-6000",
            "description": "INV-1",
            "quantity": Decimal(1),
            "unitCost": Decimal("40.00"),
        },
    ]


def test_bc_lines_that_do_not_make_the_amount_are_refused():
    """BC totals a bill from its lines, so lines that disagree with the
    approved amount would post a different figure. They used to collapse onto
    one header line, which moved coded expense onto the header's account; now
    the bill is refused."""
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    short = [_bc_line(total=Decimal("60.00"))]  # 60 of 100.00
    assert _bc_invoice_lines(_payload(line_items=short)) == "amount_mismatch"


def test_bc_refuses_a_line_with_no_amount():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    items = [_bc_line(total=None, unit_price=None), _bc_line(total=Decimal("100.00"))]
    assert _bc_invoice_lines(_payload(line_items=items)) == "line_amount_missing"


def test_bc_refuses_a_unit_price_with_no_quantity_like_every_other_adapter():
    """BC's own copy of the line rule read a missing quantity as 1; the shared
    rule (``bill_allocation``) refuses it, and BC now uses the shared rule."""
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    items = [_bc_line(total=None, quantity=None, unit_price=Decimal("100.00"))]
    assert _bc_invoice_lines(_payload(line_items=items)) == "line_amount_missing"


def test_bc_never_sends_an_unrounded_quantity_times_price():
    """3 x 33.3333 is 99.9999: not the approved 100.00, so refused, never sent
    as an unrounded amount for BC to round its own way."""
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    items = [_bc_line(total=None, quantity=Decimal(3), unit_price=Decimal("33.3333"))]
    assert _bc_invoice_lines(_payload(line_items=items)) == "amount_mismatch"


def test_bc_refuses_a_line_finer_than_the_currency_rather_than_rounding_it():
    """Lines that sum exactly to the amount but carry sub-cent parts cannot be
    sent at USD's scale without rounding money; they are refused."""
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    items = [
        _bc_line(total=Decimal("99.995")),
        _bc_line(line_number=2, total=Decimal("0.005")),
    ]
    assert _bc_invoice_lines(_payload(line_items=items)) == "line_amount_precision"


def test_bc_line_amounts_go_at_the_currencys_scale():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    usd = _bc_invoice_lines(_payload(line_items=[_bc_line(total=Decimal("100.000000"))]))
    assert str(usd.gross[0]["unitCost"]) == "100.00"
    jpy = _bc_invoice_lines(
        _payload(
            currency="JPY", amount=Decimal("5000"), line_items=[_bc_line(total=Decimal("5000.00"))]
        )
    )
    assert str(jpy.gross[0]["unitCost"]) == "5000"
    assert (
        _bc_invoice_lines(
            _payload(
                currency="JPY",
                amount=Decimal("5000.50"),
                line_items=[_bc_line(total=Decimal("5000.50"))],
            )
        )
        == "line_amount_precision"
    )


def test_bc_header_only_invoice_posts_one_line_on_the_header_account():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    assert _bc_invoice_lines(_payload(description="Bill", line_items=[])).gross == [
        {
            "lineType": "Account",
            "accountId": "ERP-6000",
            "description": "Bill",
            "quantity": Decimal(1),
            "unitCost": Decimal("100.00"),
        }
    ]
    assert _bc_invoice_lines(_payload(line_items=[], gl_account_erp_id=None)) == (
        "account_not_linked"
    )


def test_bc_post_invoice_refusal_for_mismatched_lines_is_final_and_sends_nothing():
    adapter = _bc_adapter()
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(_payload(line_items=[_bc_line()])))
    cm.assert_not_called()
    assert result.retryable is False
    assert result.message == "Business Central post refused: amount_mismatch"


def test_bc_lines_never_move_a_coded_line_onto_the_header_account():
    from app.services.erp_adapters.dynamics_365_bc import _bc_invoice_lines

    assert (
        _bc_invoice_lines(_payload(line_items=[_bc_line(gl_account_erp_id=None)]))
        == "account_not_linked"
    )


# ---------------------------------------------------------------------------
# netsuite — the expense lines add up to the approved amount
# ---------------------------------------------------------------------------


def test_netsuite_lines_post_per_line_when_they_sum_to_the_amount():
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    lines = _netsuite_expense_lines(
        _payload(
            amount=Decimal("100.00"),
            line_items=[
                _bc_line(total=Decimal("60.00")),
                _bc_line(line_number=2, total=None, quantity=Decimal(2), unit_price=Decimal(20)),
            ],
        )
    )
    assert lines == [
        {"account": {"id": "g-6100"}, "amount": Decimal("60.00"), "memo": "INV-1"},
        {"account": {"id": "g-6100"}, "amount": Decimal(40), "memo": "INV-1"},
    ]


def test_netsuite_lines_are_the_shared_bill_lines_projection():
    """NetSuite keeps no line rules of its own: account, gross and memo are
    exactly ``bill_lines.bill_lines`` for any payload, refusals included."""
    from app.services.erp_adapters.bill_lines import bill_lines
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payloads = [
        _payload(line_items=[]),
        _payload(
            amount=Decimal("110.00"),
            tax_amount=Decimal("10.00"),
            line_items=[_bc_line(total=Decimal("100.00"), description="Paper")],
        ),
        _payload(line_items=[_bc_line(total=None, unit_price=Decimal("100.00"))]),
        _payload(line_items=[_bc_line(gl_account_erp_id=None)]),
    ]
    for payload in payloads:
        shared = bill_lines(payload)
        ours = _netsuite_expense_lines(payload)
        if isinstance(shared, str):
            assert ours == shared
        else:
            assert ours == [{"account": {"id": a}, "amount": g, "memo": m} for a, g, m in shared]


def test_netsuite_refuses_a_unit_price_with_no_quantity():
    """It used to price such a line as quantity 1, inventing a quantity the
    invoice never stated; the shared rule refuses it."""
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payload = _payload(
        amount=Decimal("100.00"),
        line_items=[_bc_line(total=None, quantity=None, unit_price=Decimal("100.00"))],
    )
    assert _netsuite_expense_lines(payload) == "line_amount_missing"


def test_netsuite_single_tax_exclusive_line_posts_its_gross():
    """One line of 100 on a 110 bill with 10 tax: the line carries the whole
    stated tax, so it posts at 110 — the approved amount — rather than short."""
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payload = _payload(
        amount=Decimal("110.00"),
        tax_amount=Decimal("10.00"),
        line_items=[_bc_line(total=Decimal("100.00"), description="Paper")],
    )
    assert _netsuite_expense_lines(payload) == [
        {"account": {"id": "g-6100"}, "amount": Decimal("110.00"), "memo": "Paper"}
    ]


def test_netsuite_refuses_a_line_with_no_amount():
    """A line with neither a total nor a unit price used to post as 0 — the
    bill then booked less than was approved. Now it refuses the bill."""
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payload = _payload(
        amount=Decimal("100.00"),
        line_items=[_bc_line(total=Decimal("100.00")), _bc_line(line_number=2, total=None)],
    )
    assert _netsuite_expense_lines(payload) == "line_amount_missing"


def test_netsuite_post_invoice_refusal_for_a_missing_line_amount_is_final():
    adapter = NetSuiteAdapter(
        {
            "account_id": "123456",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )
    payload = _payload(line_items=[_bc_line(total=None)])
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(payload))
    cm.assert_not_called()
    assert result.retryable is False
    assert result.message == "NetSuite post refused: line_amount_missing"


def test_netsuite_tax_exclusive_lines_are_refused_not_posted_short():
    """Lines of 100 + 50 on a 180 bill (30 tax on the header only): posting the
    lines would book 150, short by the tax, and pro-rating the 30 across them
    would invent a split the supplier never stated. So the bill is refused
    (``tax_not_itemised``, the shared rule's reason) — the header amount is
    never recomputed."""
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payload = _payload(
        amount=Decimal("180.00"),
        tax_amount=Decimal("30.00"),
        line_items=[
            _bc_line(total=Decimal("100.00")),
            _bc_line(
                line_number=2, total=Decimal("50.00"), gl_account=None, gl_account_erp_id=None
            ),
        ],
    )
    assert _netsuite_expense_lines(payload) == "tax_not_itemised"


def test_netsuite_post_invoice_refusal_for_mismatched_lines_is_final():
    adapter = NetSuiteAdapter(
        {
            "account_id": "123456",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter.post_invoice(_payload(line_items=[_bc_line()])))
    cm.assert_not_called()
    assert result.retryable is False
    assert result.message == "NetSuite post refused: amount_mismatch"


def test_netsuite_header_only_invoice_posts_one_line_on_the_header_account():
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    assert _netsuite_expense_lines(_payload(description="March", line_items=[])) == [
        {"account": {"id": "ERP-6000"}, "amount": Decimal("100.00"), "memo": "March"}
    ]
    assert (
        _netsuite_expense_lines(_payload(line_items=[], gl_account_erp_id=None))
        == "account_not_linked"
    )


def test_netsuite_never_moves_a_coded_line_onto_the_header_account():
    """A coded line whose account has no ERP id refuses the bill rather than
    vanish into the header account — checked before the amounts."""
    from app.services.erp_adapters.netsuite import _netsuite_expense_lines

    payload = _payload(
        amount=Decimal("999.00"),
        line_items=[_bc_line(total=Decimal("60.00"), gl_account="6200", gl_account_erp_id=None)],
    )
    assert _netsuite_expense_lines(payload) == "account_not_linked"
