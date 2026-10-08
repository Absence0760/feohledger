"""Real ERP adapters are idempotent on `payload.correlation_id` (issue #143).

`erp.py::send_to_erp_internal`'s 3-attempt retry loop means a client-side timeout AFTER
the ERP already accepted a create can otherwise retry into a SECOND vendor
bill for the same invoice. Each real adapter's `post_invoice` must be
retry-safe via whichever mechanism its target ERP actually supports:

  - `merge_dev`  — an `X-Idempotency-Key` header on the create POST.
  - `netsuite`   — a pre-create lookup by `externalId` (NetSuite enforces
    uniqueness on it); a match short-circuits to success WITHOUT posting.
  - `dynamics_365_bc` — a pre-create lookup by `externalDocumentNumber`; same
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

from app.services.erp_adapters.base import InvoicePayload
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
        vendor_erp_id="ERP-V-1",
        gl_account_erp_id="ERP-6000",
    )
    base.update(overrides)
    return InvoicePayload(**base)


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
    duplicate (issue #143's exact failure scenario)."""
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
        client.get = AsyncMock(
            return_value=_mock_response(200, {"items": [{"id": "9001"}], "count": 1})
        )
        client.post = AsyncMock(
            side_effect=AssertionError("must not POST when externalId already exists")
        )
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-existing")))

    assert result.success
    assert result.erp_document_id == "9001"
    assert "idempotent" in result.message.lower()
    client.get.assert_awaited_once()
    client.post.assert_not_awaited()

    # The lookup query carries the correlation_id as the externalId filter.
    lookup_url = client.get.await_args.args[0]
    assert "externalId" in lookup_url
    assert "corr-existing" in lookup_url


def test_netsuite_post_invoice_proceeds_to_create_when_no_match():
    """The normal (first-attempt) path: no existing bill found → POST as
    before. Proves the idempotency check doesn't break ordinary creates."""
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
        client.get = AsyncMock(return_value=_mock_response(200, {"items": [], "count": 0}))
        client.post = AsyncMock(
            return_value=_mock_response(204, None, headers={"Location": "https://x/vendorBill/42"})
        )
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-new")))

    assert result.success
    assert result.erp_document_id == "42"
    client.get.assert_awaited_once()
    client.post.assert_awaited_once()
    # The bill names its vendor (`entity`) and the expense line's account by
    # NetSuite internal id — never `refName` text.
    import json

    body = json.loads(client.post.await_args.kwargs["content"])
    assert body["entity"] == {"id": "ERP-V-1"}
    assert body["expense"]["items"] == [
        {"account": {"id": "ERP-6000"}, "amount": 100.00, "memo": ""}
    ]
    assert "item" not in body


def test_netsuite_post_invoice_proceeds_when_lookup_fails():
    """A non-200 on the lookup (e.g. transient error) must fail OPEN to the
    create path, not silently refuse to post at all."""
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
        client.post = AsyncMock(
            return_value=_mock_response(204, None, headers={"Location": "https://x/vendorBill/77"})
        )
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-lookup-fails")))

    assert result.success
    assert result.erp_document_id == "77"
    client.post.assert_awaited_once()


# ---------------------------------------------------------------------------
# dynamics_365_bc — pre-create lookup by externalDocumentNumber
# ---------------------------------------------------------------------------


def _bc_adapter() -> BusinessCentralAdapter:
    adapter = BusinessCentralAdapter(
        {
            "tenant_id": "tid-1",
            "client_id": "cid",
            "client_secret": "sec",
            "environment": "sandbox",
            "company_id": "c-1",
            "base_url": "https://api.businesscentral.dynamics.com/v2.0",
        }
    )
    return adapter


def test_d365_post_invoice_short_circuits_when_external_document_number_already_exists():
    adapter = _bc_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),  # token exchange
                AssertionError("must not create when externalDocumentNumber already exists"),
            ]
        )
        client.get = AsyncMock(return_value=_mock_response(200, {"value": [{"id": "bc-doc-1"}]}))
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-bc-existing")))

    assert result.success
    assert result.erp_document_id == "bc-doc-1"
    assert "idempotent" in result.message.lower()
    client.get.assert_awaited_once()
    # Only the token exchange POST happened — no purchaseInvoices create.
    assert client.post.await_count == 1

    filter_params = client.get.await_args.kwargs["params"]
    assert "corr-bc-existing" in filter_params["$filter"]


def test_d365_post_invoice_proceeds_to_create_when_no_match():
    adapter = _bc_adapter()
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
        client.get = AsyncMock(return_value=_mock_response(200, {"value": []}))
        result = _run(adapter.post_invoice(_payload(correlation_id="corr-bc-new")))

    assert result.success
    assert result.erp_document_id == "bc-doc-2"
    client.get.assert_awaited_once()
    assert client.post.await_count == 3
    # The purchase invoice names its vendor by BC id. `vendorNumber` holds a
    # vendor NUMBER, and the name we used to send there matched nothing — or
    # another vendor whose number happened to equal it.
    import json

    body = json.loads(client.post.await_args_list[1].kwargs["content"])
    assert body["vendorId"] == "ERP-V-1"
    assert "vendorNumber" not in body
