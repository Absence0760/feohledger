"""Unit tests for `services.llm_fraud_detection`.

The integration-level wiring lives in `services.invoice_warnings`
(`_llm_anomaly_check`). Here we cover the pure-Python pieces that
shape what the LLM sees and how the response gets parsed:

  - Prompt structure: vendor name, history JSON, candidate JSON
  - Response parsing tolerates code-fenced + stray-prose output
  - Failure-soft: missing API key, empty history, network error,
    non-200, malformed JSON all return is_anomaly=False
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.llm_fraud_detection import (
    CandidateInvoice,
    HistoricalInvoice,
    build_prompt,
    detect_anomaly,
    invoice_to_candidate,
    invoice_to_history,
    parse_response,
)


def _candidate(**overrides) -> CandidateInvoice:
    base = dict(
        invoice_number="INV-99",
        invoice_date="2026-05-10",
        amount=Decimal("4200.00"),
        currency="USD",
        description="Website hosting — May",
        payment_method="ach",
        remit_to_address="100 Main St",
        po_number=None,
        vendor_name="Acme Hosting",
    )
    base.update(overrides)
    return CandidateInvoice(**base)


def _history_item(**overrides) -> HistoricalInvoice:
    base = dict(
        invoice_number="INV-1",
        invoice_date="2026-04-10",
        amount=Decimal("4200.00"),
        currency="USD",
        description="Website hosting — April",
        payment_method="ach",
        remit_to_address="100 Main St",
        po_number=None,
    )
    base.update(overrides)
    return HistoricalInvoice(**base)


# ---------- Prompt rendering ---------------------------------------------


def test_build_prompt_includes_vendor_and_both_payloads():
    candidate = _candidate(vendor_name="Acme Hosting")
    history = [_history_item(), _history_item(invoice_number="INV-2")]

    prompt = build_prompt(candidate, history)

    assert "Acme Hosting" in prompt
    assert '"invoice_number": "INV-99"' in prompt
    assert '"invoice_number": "INV-1"' in prompt
    assert '"invoice_number": "INV-2"' in prompt
    # Tells the model the vocabulary it has to use.
    assert '"is_anomaly"' in prompt and '"reason"' in prompt and '"confidence"' in prompt


def test_build_prompt_serialises_amounts_as_numbers_not_strings():
    """The amount stays an exact `Decimal` in Python and becomes a JSON number
    only in the prompt payload, so the model reasons about magnitude instead of
    treating it as an opaque string."""
    candidate = _candidate(amount=Decimal("4200.00"))
    prompt = build_prompt(candidate, [_history_item(amount=Decimal("4200.00"))])
    # Look for the JSON form `"amount": 4200.0` (no surrounding quotes).
    assert '"amount": 4200' in prompt
    # And NOT the string-quoted form.
    assert '"amount": "4200"' not in prompt


def test_build_prompt_handles_empty_history():
    """Edge case — caller usually short-circuits before this, but
    `build_prompt` shouldn't blow up on []."""
    candidate = _candidate()
    prompt = build_prompt(candidate, [])
    assert "[]" in prompt  # empty JSON array


# ---------- Response parsing ---------------------------------------------


def test_parse_response_plain_json_anomaly():
    text = '{"is_anomaly": true, "reason": "Service description shifted", "confidence": 0.85}'
    result = parse_response(text)
    assert result.is_anomaly is True
    assert result.reason == "Service description shifted"
    assert result.confidence == pytest.approx(0.85)


def test_parse_response_plain_json_clean():
    text = '{"is_anomaly": false, "reason": null, "confidence": 0.9}'
    result = parse_response(text)
    assert result.is_anomaly is False
    assert result.reason is None
    assert result.confidence == pytest.approx(0.9)


def test_parse_response_strips_markdown_code_fence():
    """Models love wrapping JSON in ```json ... ``` blocks. Strip
    them or the inner JSON parse fails."""
    text = '```json\n{"is_anomaly": true, "reason": "Bigger than usual", "confidence": 0.7}\n```'
    result = parse_response(text)
    assert result.is_anomaly is True
    assert result.reason == "Bigger than usual"


def test_parse_response_strips_unlabelled_code_fence():
    text = '```\n{"is_anomaly": false}\n```'
    result = parse_response(text)
    assert result.is_anomaly is False


def test_parse_response_extracts_json_from_surrounding_prose():
    """Fallback path: model emits text + JSON. We grab the {...}."""
    text = (
        "Here's my analysis:\n"
        '{"is_anomaly": true, "reason": "Remit-to subtly different"}\n'
        "Let me know if you need more detail."
    )
    result = parse_response(text)
    assert result.is_anomaly is True


def test_parse_response_handles_garbage():
    """If we can't extract JSON at all, fail-soft to no-anomaly so
    the AP queue isn't blocked."""
    result = parse_response("the model just said 'sure thing'")
    assert result.is_anomaly is False
    assert result.reason is None


def test_parse_response_clears_reason_when_not_anomalous():
    """Even if the model populates `reason` for a non-anomalous
    response (some do), we drop it — reasons only travel with flags."""
    text = '{"is_anomaly": false, "reason": "looks fine"}'
    result = parse_response(text)
    assert result.is_anomaly is False
    assert result.reason is None


# ---------- detect_anomaly happy path + degradation -----------------------


def _http_response(status: int, content_text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = content_text
    if status == 200:
        resp.json = MagicMock(
            return_value={
                "content": [{"type": "text", "text": content_text}],
            }
        )
    else:
        resp.json = MagicMock(return_value={})
    return resp


def test_detect_anomaly_returns_no_when_api_key_missing():
    candidate = _candidate()
    history = [_history_item()]
    result = asyncio.run(detect_anomaly(candidate, history, api_key=None))
    assert result.is_anomaly is False


def test_detect_anomaly_returns_no_when_history_empty():
    """No baseline → can't judge in/out of pattern."""
    candidate = _candidate()
    result = asyncio.run(detect_anomaly(candidate, [], api_key="sk-x"))
    assert result.is_anomaly is False


def test_detect_anomaly_passes_through_a_flagged_response():
    candidate = _candidate()
    history = [_history_item()]

    async def fake_post(*, json: dict, headers: dict):
        return _http_response(
            200,
            content_text=(
                '{"is_anomaly": true, '
                '"reason": "Rounded to nearest hundred for first time in 6 invoices", '
                '"confidence": 0.78}'
            ),
        )

    result = asyncio.run(detect_anomaly(candidate, history, api_key="sk-x", http_post=fake_post))
    assert result.is_anomaly is True
    assert "Rounded" in (result.reason or "")
    assert result.confidence == pytest.approx(0.78)


def test_detect_anomaly_falls_through_on_non_200():
    candidate = _candidate()
    history = [_history_item()]

    async def fake_post(*, json, headers):
        return _http_response(429, content_text="rate limited")

    result = asyncio.run(detect_anomaly(candidate, history, api_key="sk-x", http_post=fake_post))
    assert result.is_anomaly is False


def test_detect_anomaly_falls_through_on_http_exception():
    """Network blip mid-extraction must not abort the AP flow."""
    import httpx

    candidate = _candidate()
    history = [_history_item()]

    async def fake_post(*, json, headers):
        raise httpx.ConnectError("dns fail")

    result = asyncio.run(detect_anomaly(candidate, history, api_key="sk-x", http_post=fake_post))
    assert result.is_anomaly is False


def test_detect_anomaly_falls_through_on_unparseable_response():
    candidate = _candidate()
    history = [_history_item()]

    async def fake_post(*, json, headers):
        return _http_response(200, content_text="<<< not json >>>")

    result = asyncio.run(detect_anomaly(candidate, history, api_key="sk-x", http_post=fake_post))
    assert result.is_anomaly is False


def test_detect_anomaly_uses_org_byok_key_when_present():
    """Verifies the prompt is actually sent with the supplied key —
    a regression in API-key plumbing would silently make every
    anomaly check use the platform key (or no key)."""
    candidate = _candidate()
    history = [_history_item()]
    captured = {}

    async def fake_post(*, json, headers):
        captured["x-api-key"] = headers.get("x-api-key")
        return _http_response(200, content_text='{"is_anomaly": false}')

    asyncio.run(detect_anomaly(candidate, history, api_key="org-byok-key", http_post=fake_post))
    assert captured["x-api-key"] == "org-byok-key"


# ---------- Invoice → dataclass adapters ---------------------------------


def test_invoice_to_candidate_handles_none_optionals():
    """Real invoices have nullable amounts / dates / descriptions.
    Adapter must not blow up on the missing pieces."""
    from datetime import date as date_cls

    inv = SimpleNamespace(
        invoice_number=None,
        invoice_date=None,
        amount=None,
        currency=None,
        description=None,
        payment_method=None,
        remit_to_address=None,
        po_number=None,
        vendor_name=None,
    )
    candidate = invoice_to_candidate(inv)
    assert candidate.invoice_number == ""
    assert candidate.invoice_date is None
    assert candidate.amount == Decimal("0")
    assert candidate.currency == "USD"
    assert candidate.vendor_name == ""

    inv2 = SimpleNamespace(
        invoice_number="INV-X",
        invoice_date=date_cls(2026, 5, 10),
        amount=Decimal("1234.56"),
        currency="EUR",
        description="x",
        payment_method="ach",
        remit_to_address="addr",
        po_number="PO-1",
        vendor_name="Acme",
    )
    c2 = invoice_to_candidate(inv2)
    assert c2.invoice_date == "2026-05-10"
    assert c2.amount == Decimal("1234.56")
    assert c2.currency == "EUR"


def test_invoice_to_history_isolates_only_the_fields_the_llm_needs():
    """File URLs, line items, etc. don't ride along — keeps the
    prompt narrow and the cost predictable."""
    from datetime import date as date_cls

    inv = SimpleNamespace(
        invoice_number="INV-Y",
        invoice_date=date_cls(2026, 4, 10),
        amount=Decimal("999.00"),
        currency="USD",
        description="d",
        payment_method="wire",
        remit_to_address="addr",
        po_number="PO-9",
        vendor_name="Acme",
        file_url="should-not-appear",
        notes="should-not-appear",
    )
    h = invoice_to_history(inv)
    # Spot-check what's there.
    assert h.invoice_number == "INV-Y"
    assert h.amount == Decimal("999.00")
    # And what's not — fields not on HistoricalInvoice are dropped by
    # construction. Verifying the dataclass shape is enough.
    assert not hasattr(h, "file_url")
    assert not hasattr(h, "notes")
