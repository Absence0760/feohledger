"""A failed ERP post must never put the provider's response body in `message`.

`services/erp.py` raises `RuntimeError(result.message)` on a failed post and
stores `str(exc)` into `details={"error": …}` on the **append-only**
`invoice.erp_failed` audit row (migration 0022's BEFORE-DELETE trigger) and onto
`WorkflowInstance.state_data["last_error"]`. `audit_log_shipper` then ships that
row to CloudWatch Logs / S3 Object Lock. Nothing downstream can redact any of
those.

An ERP's validation error routinely echoes the submitted fields back, and
`InvoicePayload` carries `vendor_tax_id`, `vendor_address`, `remit_to_address`
and `bill_to_address` — so `message=f"… {resp.status_code}: {resp.text}"` wrote
vendor PII into an immutable, WORM-shipped row. This file pins the fix for all
three real adapters, modelled on
`tests/test_billing_webhook.py::test_stripe_provider_error_is_pii_free`, which
does the same for `stripe_billing`.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.erp_adapters.base import (
    ACCOUNT_NOT_LINKED,
    VENDOR_NOT_LINKED,
    InvoicePayload,
    LineItemPayload,
    erp_failure_message,
    erp_failure_reason,
    erp_refusal_message,
)
from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter
from app.services.erp_adapters.merge_dev import MergeDevAdapter
from app.services.erp_adapters.netsuite import NetSuiteAdapter

# A body shaped like a real ERP validation error: it echoes back the fields we
# sent, including every PII-bearing one on InvoicePayload.
_PII_BODY = {
    "error": {
        "message": "Validation failed",
        "submitted": {
            "vendor_tax_id": "12-3456789",
            "vendor_address": "17 Bank Street, Springfield IL 62704",
            "remit_to_address": "PO Box 9001, Springfield IL 62704",
            "bill_to_address": "1 Corporate Plaza, Chicago IL 60601",
            "iban": "GB29NWBK60161331926819",
        },
    }
}

_PII_TOKENS = (
    "12-3456789",
    "Bank Street",
    "PO Box 9001",
    "Corporate Plaza",
    "GB29NWBK60161331926819",
    "Validation failed",
)


def _run(coro):
    return asyncio.run(coro)


def _error_response(status: int) -> MagicMock:
    """A provider error response whose body echoes the submitted PII."""
    import json

    resp = MagicMock()
    resp.status_code = status
    body = json.dumps(_PII_BODY)
    resp.content = body.encode()
    resp.text = body
    resp.json = MagicMock(return_value=_PII_BODY)
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    return resp


def _ok_response(body: dict) -> MagicMock:
    import json

    resp = MagicMock()
    resp.status_code = 200
    resp.content = json.dumps(body).encode()
    resp.text = json.dumps(body)
    resp.json = MagicMock(return_value=body)
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    return resp


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        invoice_number="INV-PII-1",
        vendor_name="Acme",
        amount=Decimal("100.00"),
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-pii-1",
        vendor_tax_id="12-3456789",
        vendor_address="17 Bank Street, Springfield IL 62704",
        remit_to_address="PO Box 9001, Springfield IL 62704",
        bill_to_address="1 Corporate Plaza, Chicago IL 60601",
        vendor_erp_id="ERP-V-1",
        gl_account_erp_id="ERP-6000",
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _assert_pii_free(message: str, *, status: int, provider_hint: str) -> None:
    assert message, "a failed post must still explain itself"
    for token in _PII_TOKENS:
        assert token not in message, f"provider body leaked into ErpPostResult.message: {message!r}"
    # Still actionable: the status code and a stable reason code survive.
    assert str(status) in message
    assert erp_failure_reason(status) in message
    assert provider_hint in message


# ---------------------------------------------------------------------------
# The shared primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (422, "validation_failed"),
        (429, "rate_limited"),
        (418, "client_error"),
        (500, "provider_error"),
        (503, "provider_error"),
        (302, "unexpected_status"),
    ],
)
def test_reason_codes_are_stable_and_status_derived(status, expected):
    assert erp_failure_reason(status) == expected


def test_failure_message_shape():
    assert (
        erp_failure_message("NetSuite", 422) == "NetSuite post failed: HTTP 422 (validation_failed)"
    )


# ---------------------------------------------------------------------------
# The three real adapters
# ---------------------------------------------------------------------------


def test_netsuite_failure_message_is_pii_free():
    adapter = NetSuiteAdapter(
        {
            "account_id": "TSTDRV",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "ti",
            "token_secret": "ts",
        }
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        # The pre-create externalId lookup must miss so we reach the create POST.
        client.get = AsyncMock(return_value=_ok_response({"count": 0, "items": []}))
        client.post = AsyncMock(return_value=_error_response(422))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    _assert_pii_free(result.message, status=422, provider_hint="NetSuite")


def test_merge_dev_failure_message_is_pii_free():
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_error_response(400))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    _assert_pii_free(result.message, status=400, provider_hint="Merge.dev")


def test_business_central_failure_message_is_pii_free():
    adapter = BusinessCentralAdapter(
        {
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
            "environment": "sandbox",
            "company_id": "co",
            "base_url": "https://api.businesscentral.dynamics.com/v2.0",
        }
    )
    with (
        patch.object(BusinessCentralAdapter, "_get_token", AsyncMock(return_value="tok")),
        patch("httpx.AsyncClient") as cm,
    ):
        client = cm.return_value.__aenter__.return_value
        # The pre-create externalDocumentNumber lookup must miss.
        client.get = AsyncMock(return_value=_ok_response({"value": []}))
        client.post = AsyncMock(return_value=_error_response(409))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success is False
    _assert_pii_free(result.message, status=409, provider_hint="Business Central")


# ---------------------------------------------------------------------------
# Pre-flight refusals — an unlinked vendor or account never reaches the ERP
# ---------------------------------------------------------------------------


def test_refusal_message_shape():
    assert (
        erp_refusal_message("NetSuite", VENDOR_NOT_LINKED)
        == "NetSuite post refused: vendor_not_linked"
    )
    assert ACCOUNT_NOT_LINKED == "account_not_linked"


_BC_CONFIG = {
    "tenant_id": "t",
    "client_id": "c",
    "client_secret": "s",
    "environment": "sandbox",
    "company_id": "co",
    "base_url": "https://api.businesscentral.dynamics.com/v2.0",
}
_NS_CONFIG = {
    "account_id": "TSTDRV",
    "consumer_key": "ck",
    "consumer_secret": "cs",
    "token_id": "ti",
    "token_secret": "ts",
}
_MERGE_CONFIG = {"api_key": "k", "account_token": "tok"}


@pytest.mark.parametrize(
    ("adapter", "provider"),
    [
        (lambda: NetSuiteAdapter(_NS_CONFIG), "NetSuite"),
        (lambda: BusinessCentralAdapter(_BC_CONFIG), "Business Central"),
        (lambda: MergeDevAdapter(_MERGE_CONFIG), "Merge.dev"),
    ],
    ids=["netsuite", "dynamics_365_bc", "merge_dev"],
)
def test_unlinked_vendor_is_refused_before_any_http_call(adapter, provider):
    """No `vendor_erp_id` → a stable, PII-free refusal, and no request at all
    (not even the token exchange or the idempotency lookup). The vendor's
    name is right there on the payload and is never used instead."""
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter().post_invoice(_payload(vendor_erp_id=None)))

    assert result.success is False
    assert result.message == f"{provider} post refused: vendor_not_linked"
    assert "Acme" not in result.message
    cm.assert_not_called()


def _coded_line(**overrides) -> LineItemPayload:
    base = dict(line_number=1, total=Decimal("100.00"), gl_account="6000")
    base.update(overrides)
    return LineItemPayload(**base)


@pytest.mark.parametrize(
    ("adapter", "provider", "payload"),
    [
        # A coded line whose account never synced.
        (
            lambda: NetSuiteAdapter(_NS_CONFIG),
            "NetSuite",
            _payload(line_items=[_coded_line(gl_account_erp_id=None)]),
        ),
        # A header-only bill with no account id: an expense line needs one.
        (
            lambda: NetSuiteAdapter(_NS_CONFIG),
            "NetSuite",
            _payload(line_items=[], gl_account_erp_id=None),
        ),
        # An uncoded line falls back to the header account — which is unlinked.
        (
            lambda: NetSuiteAdapter(_NS_CONFIG),
            "NetSuite",
            _payload(line_items=[_coded_line(gl_account=None)], gl_account_erp_id=None),
        ),
        (
            lambda: MergeDevAdapter(_MERGE_CONFIG),
            "Merge.dev",
            _payload(line_items=[_coded_line(gl_account_erp_id=None)]),
        ),
    ],
    ids=["netsuite-line", "netsuite-header-only", "netsuite-uncoded-line", "merge-line"],
)
def test_unlinked_account_is_refused_before_any_http_call(adapter, provider, payload):
    """A GL code is never posted as text where the ERP wants an account id,
    and a coded line is never silently re-homed on the header's account."""
    with patch("httpx.AsyncClient") as cm:
        result = _run(adapter().post_invoice(payload))

    assert result.success is False
    assert result.message == f"{provider} post refused: account_not_linked"
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# Drift guard — no adapter may reintroduce the body
# ---------------------------------------------------------------------------


def test_no_erp_adapter_interpolates_a_response_body_into_a_message():
    """Source scan, in the shape of `tests/test_payment_methods.py`'s.

    A future adapter added by copy-paste would otherwise silently reinstate the
    leak: the code reads naturally and nothing fails until an ERP echoes a tax
    id into an immutable audit row.
    """
    import ast  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    adapters_dir = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "erp_adapters"

    offenders: list[str] = []
    for path in sorted(adapters_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            # keyword `message=<f-string>` where the f-string interpolates a
            # `.text` / `.content` / `.json()` attribute off anything.
            if not (isinstance(node, ast.keyword) and node.arg == "message"):
                continue
            if not isinstance(node.value, ast.JoinedStr):
                continue
            for part in ast.walk(node.value):
                is_body_attr = isinstance(part, ast.Attribute) and part.attr in {
                    "text",
                    "content",
                }
                is_body_call = (
                    isinstance(part, ast.Call)
                    and isinstance(part.func, ast.Attribute)
                    and part.func.attr == "json"
                )
                if is_body_attr or is_body_call:
                    offenders.append(f"{path.relative_to(adapters_dir.parent)}:{node.value.lineno}")
                    break

    assert not offenders, (
        "an ERP adapter interpolates the provider's response body into "
        f"ErpPostResult.message at: {offenders}. That message lands on the "
        "append-only invoice.erp_failed audit row and is shipped to WORM "
        "storage. Use base.erp_failure_message(provider, status_code)."
    )
