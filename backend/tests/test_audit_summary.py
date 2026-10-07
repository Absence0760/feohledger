"""Unit tests for `services.audit_summary`.

The DB-backed orchestration (`get_or_build_summary` fingerprint freshness)
is exercised against a live tenant in `test_invoices_summary_api.py`. Here we
cover the pure pieces:

  - `build_prompt` includes every event type + confidence context, and
    excludes PII / banking fields
  - `parse_response` tolerates code-fenced + stray-prose output, falls back
    to None on garbage
  - `build_template_summary` is deterministic and covers all event types
  - `summarize` fails soft to the template (no api key / mock / error /
    unparseable) and returns parsed text with an injected http_post
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.audit_summary import (
    AuditEvent,
    build_prompt,
    build_template_summary,
    parse_response,
    summarize,
)


def _invoice(**overrides):
    base = dict(
        invoice_number="INV-42",
        vendor_name="Acme Hosting",
        amount=Decimal("4200.00"),
        currency="USD",
        status="approved",
        vendor_tax_id="TAX-SECRET-999",
        remit_to_address="Bank acct 12345678",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _events():
    return [
        AuditEvent(
            action="invoice.extraction_completed",
            actor_name=None,
            created_at="2026-05-01T10:00:00+00:00",
            details={"method": "claude_vision", "confidence": 0.95},
        ),
        AuditEvent(
            action="invoice.submitted_for_review",
            actor_name="Dana Clerk",
            created_at="2026-05-01T10:05:00+00:00",
            details={"to_status": "ready_for_review"},
        ),
        AuditEvent(
            action="invoice.corrected",
            actor_name="Dana Clerk",
            created_at="2026-05-01T10:06:00+00:00",
            details={"fields_corrected": ["amount"]},
        ),
        # Real exception-queue actions (services/exception_lifecycle) — these
        # are correlation-keyed to the invoice, so they land on its trail.
        AuditEvent(
            action="exception.raised",
            actor_name=None,
            created_at="2026-05-01T10:06:30+00:00",
            details={"exception_type": "po_mismatch"},
        ),
        AuditEvent(
            action="exception.resolved",
            actor_name="Manny Manager",
            created_at="2026-05-01T10:07:00+00:00",
            details={"exception_type": "po_mismatch", "resolution": "approved variance"},
        ),
        AuditEvent(
            action="invoice.approved",
            actor_name="Manny Manager",
            created_at="2026-05-01T10:08:00+00:00",
            details={"to_status": "approved"},
        ),
        AuditEvent(
            action="invoice.erp_submitted",
            actor_name=None,
            created_at="2026-05-01T10:09:00+00:00",
            details={"erp_reference": "ERP-77", "to_status": "sent_to_erp"},
        ),
    ]


def _extraction_meta():
    return {
        "confidence": 0.95,
        "method": "claude_vision",
        "vendor_cache_applied": ["currency"],
        "rag_neighbor_count": 3,
    }


# ---------- build_prompt ---------------------------------------------------


def test_build_prompt_includes_all_event_types_and_confidence():
    prompt = build_prompt(_invoice(), _events(), _extraction_meta())
    # Status transitions, corrections, exception resolution, ERP sync.
    assert "invoice.submitted_for_review" in prompt
    assert "fields_corrected" in prompt
    assert "exception_type" in prompt
    assert "invoice.erp_submitted" in prompt
    # Confidence context fed in.
    assert "0.95" in prompt
    assert "rag_neighbor_count" in prompt
    # The contract keys the model must emit.
    assert '"text"' in prompt and '"confidence_context"' in prompt


def test_build_prompt_excludes_pii_and_banking():
    """The throwaway prompt must never carry tax ids / bank details."""
    prompt = build_prompt(_invoice(), _events(), _extraction_meta())
    assert "TAX-SECRET-999" not in prompt
    assert "12345678" not in prompt
    assert "remit_to_address" not in prompt
    assert "vendor_tax_id" not in prompt


def test_build_prompt_amount_is_string_not_float():
    """Amount only goes into the prompt as a string — never coerced to a
    float for storage (money invariant)."""
    prompt = build_prompt(_invoice(amount=Decimal("4200.00")), _events(), _extraction_meta())
    assert '"amount": "4200.00"' in prompt


# ---------- parse_response -------------------------------------------------


def test_parse_response_plain_json():
    text = '{"text": "All good.", "confidence_context": "auto-extracted at 95% confidence"}'
    result = parse_response(text)
    assert result is not None
    assert result.text == "All good."
    assert result.confidence_context == "auto-extracted at 95% confidence"


def test_parse_response_strips_code_fence():
    text = '```json\n{"text": "Fenced summary.", "confidence_context": null}\n```'
    result = parse_response(text)
    assert result is not None
    assert result.text == "Fenced summary."
    assert result.confidence_context is None


def test_parse_response_extracts_from_prose():
    text = 'Here you go:\n{"text": "Prose-wrapped.", "confidence_context": null}\nThanks!'
    result = parse_response(text)
    assert result is not None
    assert result.text == "Prose-wrapped."


def test_parse_response_garbage_returns_none():
    assert parse_response("the model just rambled") is None


def test_parse_response_missing_text_returns_none():
    assert parse_response('{"confidence_context": "x"}') is None


# ---------- build_template_summary -----------------------------------------


def test_template_summary_is_deterministic_and_covers_events():
    a = build_template_summary(_invoice(), _events(), _extraction_meta())
    b = build_template_summary(_invoice(), _events(), _extraction_meta())
    assert a.text == b.text  # deterministic
    assert "INV-42" in a.text
    assert "Acme Hosting" in a.text
    # actor attribution for approval
    assert "Manny Manager" in a.text
    assert "sent to ERP" in a.text
    # Exception activity is why an invoice stalls — the reviewer catching up
    # needs to see it named, and who cleared it. The type is rendered as its
    # humanized label (EXCEPTION_TYPE_LABELS), not the raw snake_case code.
    assert "flagged (PO Mismatch)" in a.text
    assert "had its exception cleared (PO Mismatch) by Manny Manager" in a.text
    assert "po_mismatch" not in a.text
    # confidence clause
    assert a.confidence_context is not None
    assert "95%" in a.confidence_context
    assert "RAG priors" in a.confidence_context


def test_template_summary_excludes_pii():
    summary = build_template_summary(_invoice(), _events(), _extraction_meta())
    assert "TAX-SECRET-999" not in summary.text
    assert "12345678" not in summary.text


def test_template_summary_no_events():
    summary = build_template_summary(_invoice(status="new"), [], None)
    assert "INV-42" in summary.text
    assert "no recorded timeline activity" in summary.text
    assert summary.confidence_context is None


def test_template_summary_uses_humanized_exception_label():
    """The generated sentence must read the same humanized label the
    exception queue UI shows (`api.exceptions.EXCEPTION_TYPE_LABELS`), not
    the raw snake_case `exception_type` code — a reviewer catching up on an
    invoice shouldn't see internal codes like `fraud_flag` in prose."""
    events = [
        AuditEvent(
            action="exception.raised",
            actor_name=None,
            created_at="2026-05-01T10:06:30+00:00",
            details={"exception_type": "fraud_flag"},
        ),
    ]
    summary = build_template_summary(_invoice(), events, None)
    assert "Fraud Flag" in summary.text
    assert "fraud_flag" not in summary.text


def test_template_summary_falls_back_to_raw_code_for_unmapped_type():
    """An exception_type with no entry in EXCEPTION_TYPE_LABELS (a stale or
    corrupted row) still renders something rather than raising."""
    events = [
        AuditEvent(
            action="exception.raised",
            actor_name=None,
            created_at="2026-05-01T10:06:30+00:00",
            details={"exception_type": "totally_unknown_type"},
        ),
    ]
    summary = build_template_summary(_invoice(), events, None)
    assert "totally_unknown_type" in summary.text


# ---------- summarize fail-soft + happy path -------------------------------


def _http_response(status: int, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.json = MagicMock(
        return_value=({"content": [{"type": "text", "text": text}]} if status == 200 else {})
    )
    return resp


def test_summarize_no_api_key_uses_template_without_network():
    called = {"n": 0}

    async def fake_post(*, json, headers):  # pragma: no cover - must not run
        called["n"] += 1
        return _http_response(200, '{"text": "x"}')

    result = asyncio.run(
        summarize(
            _invoice(),
            _events(),
            _extraction_meta(),
            config={"api_key": "", "model": "m"},
            http_post=fake_post,
        )
    )
    assert called["n"] == 0  # no network call
    assert "INV-42" in result.text  # template text


def test_summarize_empty_events_uses_template():
    result = asyncio.run(
        summarize(
            _invoice(status="new"),
            [],
            None,
            config={"api_key": "sk-x", "model": "m"},
        )
    )
    assert "no recorded timeline activity" in result.text


def test_summarize_happy_path_parses_llm_text():
    captured = {}

    async def fake_post(*, json, headers):
        captured["model"] = json["model"]
        captured["x-api-key"] = headers["x-api-key"]
        captured["body"] = json
        return _http_response(
            200,
            '{"text": "LLM paragraph.", "confidence_context": "auto-extracted at 95% confidence"}',
        )

    result = asyncio.run(
        summarize(
            _invoice(),
            _events(),
            _extraction_meta(),
            config={"api_key": "org-key", "model": "claude-x"},
            http_post=fake_post,
        )
    )
    assert result.text == "LLM paragraph."
    assert result.confidence_context == "auto-extracted at 95% confidence"
    assert captured["x-api-key"] == "org-key"
    assert captured["model"] == "claude-x"
    # Current models think by default and thinking counts toward max_tokens:
    # the old 600 could end the turn before any text and silently fall back
    # to the template. Nothing a current model rejects is sent either.
    assert captured["body"]["max_tokens"] >= 4096
    for field in ("temperature", "top_p", "top_k", "tool_choice", "thinking"):
        assert field not in captured["body"]


def test_summarize_non_200_falls_back_to_template():
    async def fake_post(*, json, headers):
        return _http_response(429, "rate limited")

    result = asyncio.run(
        summarize(
            _invoice(),
            _events(),
            _extraction_meta(),
            config={"api_key": "sk-x", "model": "m"},
            http_post=fake_post,
        )
    )
    assert "INV-42" in result.text


def test_summarize_network_error_falls_back_to_template():
    import httpx

    async def fake_post(*, json, headers):
        raise httpx.ConnectError("dns fail")

    result = asyncio.run(
        summarize(
            _invoice(),
            _events(),
            _extraction_meta(),
            config={"api_key": "sk-x", "model": "m"},
            http_post=fake_post,
        )
    )
    assert "INV-42" in result.text


def test_summarize_unparseable_response_falls_back_to_template():
    async def fake_post(*, json, headers):
        return _http_response(200, "<<< not json >>>")

    result = asyncio.run(
        summarize(
            _invoice(),
            _events(),
            _extraction_meta(),
            config={"api_key": "sk-x", "model": "m"},
            http_post=fake_post,
        )
    )
    assert "INV-42" in result.text


def test_scrub_details_drops_unwhitelisted_keys():
    from app.services.audit_summary import _scrub_details

    scrubbed = _scrub_details(
        {
            "to_status": "approved",
            "vendor_tax_id": "TAX-123",
            "remit_to_address": "Bank 999",
            "raw_payload": {"secret": 1},
        }
    )
    assert scrubbed == {"to_status": "approved"}


@pytest.mark.parametrize("bad", [None, {}])
def test_scrub_details_handles_empty(bad):
    from app.services.audit_summary import _scrub_details

    assert _scrub_details(bad) == {}


@pytest.mark.parametrize("bad", [[1, 2, 3], ["to_status"], "a-string", 7])
def test_scrub_details_handles_a_non_object_details_column(bad):
    """`audit_log.details` is JSONB with no object-shape constraint, so a
    non-object value can reach here from a hand-written / corrupted row. It must
    scrub to nothing rather than index into it — `["to_status"]` would otherwise
    pass the `key in details` membership test and then TypeError on lookup,
    500ing the whole invoice's summary over one bad row."""
    from app.services.audit_summary import _scrub_details

    assert _scrub_details(bad) == {}
