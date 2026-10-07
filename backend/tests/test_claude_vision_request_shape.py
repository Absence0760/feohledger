"""The Claude extraction adapter's request shape and its token-usage capture.

`claude_vision` is the platform's invoice reader and runs on whichever Claude
model the platform (`FEOH_EXTRACTION_MODEL`) or a BYOK org names. Current models
(Sonnet 5.5, Opus 5.5) return a 400 for several fields older code sent freely —
`temperature`, `thinking: {type: "disabled"}` / `budget_tokens`, forced
`tool_choice`, an assistant prefill — so these tests pin the body that goes out,
not just the parse of what comes back. Network is stubbed throughout; nothing
here calls Anthropic.

Also pinned: the `usage` block of every answered call lands on
`ExtractionResult.usage` — including a refused, truncated or unparseable read,
which the provider still billed (`backend/docs/ai-extraction.md` § Token
tracking).
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services.extraction_adapters.base import ExtractionTokenUsage
from app.services.extraction_adapters.claude_vision import ClaudeVisionAdapter

_INVOICE_JSON = {
    "invoice_number": {"value": "INV-77", "confidence": 0.97},
    "vendor_name": {"value": "Acme Corp", "confidence": 0.95},
    "amount": {"value": "1200.00", "confidence": 0.96},
    "invoice_date": {"value": "2026-09-30", "confidence": 0.9},
    "line_items": [],
}

_USAGE = {
    "input_tokens": 2310,
    "output_tokens": 912,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}

# Fields a current Claude model rejects with a 400. None may ever be sent.
_REJECTED_FIELDS = ("temperature", "top_p", "top_k", "tool_choice", "thinking")


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, response: _Resp):
        self._response = response
        self.calls: list[dict] = []
        self.timeout = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._response


def _stub(monkeypatch, payload: dict, status: int = 200) -> _Client:
    from app.services.extraction_adapters import claude_vision as mod

    client = _Client(_Resp(status, payload))

    def _factory(**kw):
        client.timeout = kw.get("timeout")
        return client

    monkeypatch.setattr(mod.httpx, "AsyncClient", _factory)
    return client


def _answer(**overrides) -> dict:
    payload = {
        "model": "claude-sonnet-5-5",
        "stop_reason": "end_turn",
        # A thinking-on model may lead with a thinking block (empty text under
        # the default display); the parser must read by block type.
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": json.dumps(_INVOICE_JSON)},
        ],
        "usage": _USAGE,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_the_platform_default_model_is_current_sonnet():
    # Operator's cost-based choice (docs/decisions.md §253); the old default,
    # claude-sonnet-4-20250514, is deprecated.
    assert settings.extraction_model == "claude-sonnet-5-5"


async def test_extract_sends_only_fields_current_models_accept(monkeypatch):
    client = _stub(monkeypatch, _answer())
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(
        b"%PDF-1.4 fake", "i.pdf", "application/pdf"
    )
    assert result.success is True

    body = client.calls[0]["json"]
    for field in _REJECTED_FIELDS:
        assert field not in body, f"{field} is a 400 on current Claude models"
    # One user turn, no assistant prefill.
    assert [m["role"] for m in body["messages"]] == ["user"]
    # No model in the config → the platform default, never a hardcoded id.
    assert body["model"] == settings.extraction_model
    # Thinking counts toward max_tokens; 4096 left too little room for it.
    assert body["max_tokens"] >= 16000
    # Effort is never sent unasked (older BYOK models reject it).
    assert "output_config" not in body
    assert client.calls[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert client.timeout >= 300


async def test_a_pdf_goes_as_a_document_block_and_an_image_as_an_image(monkeypatch):
    client = _stub(monkeypatch, _answer())
    adapter = ClaudeVisionAdapter({"api_key": "k"})
    await adapter.extract(b"%PDF-1.4", "i.pdf", "application/pdf")
    await adapter.extract(b"\x89PNG", "i.png", "image/png")

    first, second = (c["json"]["messages"][0]["content"][0] for c in client.calls)
    assert first["type"] == "document"
    assert first["source"]["media_type"] == "application/pdf"
    assert second["type"] == "image"
    assert second["source"]["media_type"] == "image/png"


async def test_configured_effort_is_sent_in_output_config(monkeypatch):
    client = _stub(monkeypatch, _answer())
    await ClaudeVisionAdapter({"api_key": "k", "effort": "low"}).extract(b"%PDF-1.4")
    assert client.calls[0]["json"]["output_config"] == {"effort": "low"}


async def test_a_byok_model_choice_is_honoured(monkeypatch):
    client = _stub(monkeypatch, _answer())
    await ClaudeVisionAdapter({"api_key": "k", "model": "claude-opus-5-5"}).extract(b"%PDF-1.4")
    assert client.calls[0]["json"]["model"] == "claude-opus-5-5"


def test_platform_mode_passes_the_configured_effort():
    from app.services import extraction as ext

    assert ext._resolve_extraction_config({}, announce=False)["effort"] == (
        settings.extraction_effort
    )


async def test_statement_read_uses_the_same_request_shape(monkeypatch):
    client = _stub(
        monkeypatch,
        _answer(content=[{"type": "text", "text": json.dumps({"lines": []})}]),
    )
    await ClaudeVisionAdapter({"api_key": "k", "effort": "low"}).extract_statement(b"%PDF-1.4")
    body = client.calls[0]["json"]
    for field in _REJECTED_FIELDS:
        assert field not in body
    assert body["model"] == settings.extraction_model
    assert body["output_config"] == {"effort": "low"}


async def test_test_connection_pings_the_configured_model_not_a_retired_one(monkeypatch):
    client = _stub(monkeypatch, _answer(stop_reason="max_tokens"))
    ok = await ClaudeVisionAdapter({"api_key": "k", "model": "claude-opus-5-5"}).test_connection()
    # A ping cut off at its tiny max_tokens is still a 200 — the key works.
    assert ok is True
    body = client.calls[0]["json"]
    assert body["model"] == "claude-opus-5-5"
    for field in _REJECTED_FIELDS:
        assert field not in body


# --------------------------------------------------------------------------- #
# Response handling + usage capture
# --------------------------------------------------------------------------- #


async def test_a_successful_read_carries_the_usage_block(monkeypatch):
    _stub(monkeypatch, _answer())
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(b"%PDF-1.4")
    assert result.success is True
    assert result.invoice_number.value == "INV-77"
    assert result.usage == ExtractionTokenUsage(
        input_tokens=2310,
        output_tokens=912,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        model="claude-sonnet-5-5",
    )


async def test_an_unparseable_read_still_reports_what_it_cost(monkeypatch):
    _stub(monkeypatch, _answer(content=[{"type": "text", "text": "no json here"}]))
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(b"%PDF-1.4")
    assert result.success is False
    assert result.usage is not None and result.usage.output_tokens == 912


async def test_a_refusal_is_named_instead_of_reported_as_a_parse_failure(monkeypatch):
    _stub(
        monkeypatch,
        _answer(
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "general_harms"},
            content=[],
        ),
    )
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(b"%PDF-1.4")
    assert result.success is False
    assert "refusal" in result.error and "general_harms" in result.error
    assert result.usage is not None


async def test_a_truncated_read_is_named(monkeypatch):
    _stub(monkeypatch, _answer(stop_reason="max_tokens"))
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(b"%PDF-1.4")
    assert result.success is False
    assert "max_tokens" in result.error


async def test_a_provider_error_carries_no_usage(monkeypatch):
    _stub(monkeypatch, {"error": {"type": "invalid_request_error"}}, status=400)
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract(b"%PDF-1.4")
    assert result.success is False
    assert result.usage is None


# --------------------------------------------------------------------------- #
# ExtractionTokenUsage parsing
# --------------------------------------------------------------------------- #


def test_anthropic_usage_falls_back_to_the_requested_model_and_drops_junk():
    usage = ExtractionTokenUsage.from_anthropic(
        {"usage": {"input_tokens": -1, "output_tokens": True, "cache_read_input_tokens": 5}},
        "claude-sonnet-5-5",
    )
    assert usage.input_tokens is None  # negative → not a count
    assert usage.output_tokens is None  # a bool is not a count
    assert usage.cache_read_input_tokens == 5
    assert usage.cache_creation_input_tokens is None
    assert usage.model == "claude-sonnet-5-5"


def test_anthropic_usage_tolerates_a_missing_block():
    usage = ExtractionTokenUsage.from_anthropic({}, None)
    assert usage == ExtractionTokenUsage()


def test_openai_usage_splits_cached_tokens_out_of_the_prompt_count():
    usage = ExtractionTokenUsage.from_openai(
        {
            "model": "gpt-4o-2024-08-06",
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 400},
            },
        },
        "gpt-4o",
    )
    # input_tokens means "uncached input" for every provider.
    assert (usage.input_tokens, usage.cache_read_input_tokens) == (600, 400)
    assert usage.output_tokens == 300
    assert usage.cache_creation_input_tokens is None
    assert usage.model == "gpt-4o-2024-08-06"


async def test_openai_vision_reports_usage_on_its_result(monkeypatch):
    from app.services.extraction_adapters import openai_vision as mod
    from app.services.extraction_adapters.openai_vision import OpenAIVisionAdapter

    payload = {
        "model": "gpt-4o",
        "choices": [{"message": {"content": json.dumps(_INVOICE_JSON)}}],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 200},
    }
    client = _Client(_Resp(200, payload))
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: client)

    result = await OpenAIVisionAdapter({"api_key": "k"}).extract(b"\x89PNG", "i.png", "image/png")
    assert result.success is True
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1500, 200)
    assert result.usage.model == "gpt-4o"


def test_usage_columns_match_the_model():
    from app.models.usage import ExtractionUsage

    cols = set(ExtractionUsage.__table__.columns.keys())
    assert set(ExtractionTokenUsage().as_columns()) <= cols


@pytest.mark.parametrize("model", [None, "", "   "])
def test_a_blank_model_id_is_not_recorded(model):
    assert ExtractionTokenUsage.from_anthropic({"model": model}, model).model is None
