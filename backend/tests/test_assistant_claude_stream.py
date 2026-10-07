"""Conversational AP Assistant — per-token claude SSE passthrough.

Covers the `claude` adapter's `respond_streaming` generator and the orchestrator
forwarding contract, all with a **mocked** Anthropic streaming client (no key, no
network). These are pure unit tests — no DB — so they're immune to the realdb
parallel-run flake and never need a real Anthropic key.

What's proven here:
  - the adapter forwards real Anthropic `text_delta`s as `TextDelta`s in order,
    token-by-token (not coarse post-hoc chunks);
  - tool-use blocks (streamed as `input_json_delta`) drive `run_tool` and surface
    as `ToolDelta`s, and the multi-hop loop feeds tool results back;
  - usage/token accounting matches the streamed usage (input from
    `message_start`, output from the cumulative `message_delta`), summed per hop;
  - a mid-stream transport error → a fail-soft `TextDelta` + terminal
    `StreamDone` (the generator never raises, never double-counts);
  - the `mock` adapter's default `respond_streaming` stays deterministic
    (coarse chunking, no network);
  - the dispatcher still downgrades `claude` → `mock` with no key (local-first).
"""

from __future__ import annotations

import json

import httpx

from app.services.assistant import claude_adapter as claude_mod
from app.services.assistant.base import (
    AssistantReply,
    StreamDone,
    TextDelta,
    ToolDelta,
    ToolInvocation,
)
from app.services.assistant.claude_adapter import ClaudeAssistantAdapter
from app.services.assistant.dispatcher import get_assistant_adapter
from app.services.assistant.mock_adapter import MockAssistantAdapter

# ---------------------------------------------------------------------------
# SSE stream synthesis helpers
# ---------------------------------------------------------------------------


def _sse(event_type: str, data: dict) -> str:
    """One Anthropic SSE frame: `event:` line + single-line `data:` JSON."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _text_only_stream(*, tokens: list[str], input_tokens: int, output_tokens: int) -> str:
    """A single-hop, text-only stream (no tool use)."""
    frames = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": input_tokens, "output_tokens": 0}},
            },
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for tok in tokens:
        frames.append(
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": tok},
                },
            )
        )
    frames.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
    frames.append(
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": output_tokens},
            },
        )
    )
    frames.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(frames)


def _tool_use_hop_stream(
    *, tool_name: str, tool_id: str, input_obj: dict, input_tokens: int, output_tokens: int
) -> str:
    """Hop 1: the model emits a tool_use block (args streamed as input_json_delta)."""
    raw = json.dumps(input_obj)
    # Split the arg JSON into two fragments to exercise reassembly.
    mid = len(raw) // 2
    frag_a, frag_b = raw[:mid], raw[mid:]
    return "".join(
        [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": input_tokens, "output_tokens": 0}},
                },
            ),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tool_name,
                        "input": {},
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": frag_a},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": frag_b},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": output_tokens},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


class _StreamRecorder:
    """Hands out queued SSE response bodies and records each request body."""

    def __init__(self, bodies: list[str]):
        self._bodies = list(bodies)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        body = self._bodies.pop(0)
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )


def _patch_client(monkeypatch, recorder: _StreamRecorder) -> None:
    """Force the adapter's `httpx.AsyncClient(...)` to use a MockTransport."""
    transport = httpx.MockTransport(recorder.handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(claude_mod.httpx, "AsyncClient", _factory)


def _adapter() -> ClaudeAssistantAdapter:
    # A non-empty api_key keeps the dispatcher from downgrading to mock; the
    # MockTransport means the key is never used against a real endpoint.
    return ClaudeAssistantAdapter({"api_key": "sk-test", "model": "claude-opus-4-8"})


async def _noop_run_tool(tool_name: str, raw_args: dict) -> ToolInvocation:  # pragma: no cover
    raise AssertionError("run_tool should not be called for a text-only stream")


async def _collect(gen) -> list:
    return [ev async for ev in gen]


# ===========================================================================
# Per-token passthrough (text only, single hop)
# ===========================================================================


async def test_text_deltas_forwarded_token_by_token_in_order(monkeypatch):
    tokens = ["You ", "have ", "3 ", "open ", "invoices."]
    rec = _StreamRecorder([_text_only_stream(tokens=tokens, input_tokens=42, output_tokens=11)])
    _patch_client(monkeypatch, rec)

    events = await _collect(
        _adapter().respond_streaming(
            message="how many open invoices?",
            history=[],
            tool_specs=[],
            run_tool=_noop_run_tool,
        )
    )

    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    done = [e for e in events if isinstance(e, StreamDone)]

    # One TextDelta per streamed token, in order — true passthrough, not coarse chunks.
    assert [d.text for d in text_deltas] == tokens
    assert len(done) == 1
    assert events[-1] is done[0], "StreamDone must be terminal"

    reply = done[0].reply
    # Concatenated deltas reconstruct the answer exactly (lossless).
    assert "".join(d.text for d in text_deltas) == reply.answer
    assert reply.answer == "You have 3 open invoices."
    # Token accounting comes straight off the streamed usage.
    assert reply.input_tokens == 42
    assert reply.output_tokens == 11
    assert reply.tool_invocations == []
    assert reply.provider == "claude"

    # The request was a streaming request.
    assert rec.requests[0]["stream"] is True


# ===========================================================================
# Tool-use loop over streaming: input_json_delta → run_tool → second hop
# ===========================================================================


async def test_streaming_tool_use_loop_runs_tool_and_sums_usage(monkeypatch):
    # Hop 1: tool_use(get_vendor_spend) ; Hop 2: final prose.
    hop1 = _tool_use_hop_stream(
        tool_name="get_vendor_spend",
        tool_id="toolu_1",
        input_obj={"period": "ytd", "top_n": 3},
        input_tokens=50,
        output_tokens=20,
    )
    hop2 = _text_only_stream(
        tokens=["Top ", "vendor: ", "BigCo."], input_tokens=70, output_tokens=8
    )
    rec = _StreamRecorder([hop1, hop2])
    _patch_client(monkeypatch, rec)

    seen_args: list[dict] = []

    async def run_tool(tool_name: str, raw_args: dict) -> ToolInvocation:
        seen_args.append({"tool": tool_name, "args": raw_args})
        return ToolInvocation(
            tool=tool_name,
            args={"period": "ytd", "top_n": 3},
            result={"vendors": [{"vendor_name": "BigCo"}], "total_spend": "1000.00"},
            error=None,
        )

    events = await _collect(
        _adapter().respond_streaming(
            message="top vendors this year?",
            history=[],
            tool_specs=[{"name": "get_vendor_spend"}],
            run_tool=run_tool,
        )
    )

    tool_deltas = [e for e in events if isinstance(e, ToolDelta)]
    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    done = [e for e in events if isinstance(e, StreamDone)][0]

    # The streamed input_json_delta fragments reassembled into the tool args.
    assert seen_args == [{"tool": "get_vendor_spend", "args": {"period": "ytd", "top_n": 3}}]

    # One ToolDelta surfaced, carrying the run_tool result.
    assert len(tool_deltas) == 1
    assert tool_deltas[0].invocation.tool == "get_vendor_spend"
    assert tool_deltas[0].invocation.result["total_spend"] == "1000.00"

    # The ToolDelta precedes the prose deltas (chart-before-text ordering).
    first_tool = next(i for i, e in enumerate(events) if isinstance(e, ToolDelta))
    first_text = next(i for i, e in enumerate(events) if isinstance(e, TextDelta))
    assert first_tool < first_text

    # Final answer is the hop-2 prose; usage is summed across BOTH hops.
    assert done.reply.answer == "Top vendor: BigCo."
    assert "".join(d.text for d in text_deltas) == done.reply.answer
    assert done.reply.input_tokens == 50 + 70
    assert done.reply.output_tokens == 20 + 8
    assert len(done.reply.tool_invocations) == 1

    # The second hop echoed the assistant tool_use turn + the tool_result back.
    assert len(rec.requests) == 2
    hop2_messages = rec.requests[1]["messages"]
    assert hop2_messages[-2]["role"] == "assistant"
    assert hop2_messages[-1]["role"] == "user"
    assert hop2_messages[-1]["content"][0]["type"] == "tool_result"


def _thinking_then_tool_use_stream() -> str:
    """Hop 1 on a thinking-on model: a thinking block (text + signature
    streamed as deltas), then a tool_use block."""
    frames = [
        _sse(
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 40}}},
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Need vendor "},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "spend."},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "EqQBCkgsig=="},
            },
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_t",
                    "name": "get_vendor_spend",
                    "input": {},
                },
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"period": "ytd"}'},
            },
        ),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 30},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]
    return "".join(frames)


async def test_streamed_thinking_block_is_echoed_back_with_its_signature(monkeypatch):
    """Current models think by default. The tool-use hop must replay the
    thinking block exactly — text AND signature — or the API rejects hop 2."""
    rec = _StreamRecorder(
        [
            _thinking_then_tool_use_stream(),
            _text_only_stream(tokens=["Done."], input_tokens=60, output_tokens=4),
        ]
    )
    _patch_client(monkeypatch, rec)

    async def run_tool(tool_name: str, raw_args: dict) -> ToolInvocation:
        return ToolInvocation(tool=tool_name, args=raw_args, result={"ok": True}, error=None)

    events = await _collect(
        _adapter().respond_streaming(
            message="spend?",
            history=[],
            tool_specs=[{"name": "get_vendor_spend"}],
            run_tool=run_tool,
        )
    )

    # Thinking text is never forwarded to the user as answer text.
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Done."
    echoed = rec.requests[1]["messages"][-2]
    assert echoed["role"] == "assistant"
    assert echoed["content"][0] == {
        "type": "thinking",
        "thinking": "Need vendor spend.",
        "signature": "EqQBCkgsig==",
    }
    assert echoed["content"][1]["type"] == "tool_use"
    assert echoed["content"][1]["input"] == {"period": "ytd"}


def test_request_body_leaves_room_for_thinking_and_sends_no_rejected_fields():
    body = _adapter()._request_body("claude-sonnet-5-5", [], [])
    assert body["max_tokens"] >= 16000
    assert body["thinking"] == {"type": "adaptive"}
    for field in ("temperature", "top_p", "top_k", "tool_choice"):
        assert field not in body


# ===========================================================================
# Fail-soft: mid-stream transport error → error TextDelta + StreamDone, no raise
# ===========================================================================


async def test_stream_transport_error_is_failsoft(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(claude_mod.httpx, "AsyncClient", _factory)

    events = await _collect(
        _adapter().respond_streaming(
            message="anything",
            history=[],
            tool_specs=[],
            run_tool=_noop_run_tool,
        )
    )

    # Generator must not raise; it yields a fail-soft TextDelta then StreamDone.
    assert isinstance(events[-1], StreamDone)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "unavailable" in text.lower()
    reply = events[-1].reply
    # Nothing streamed before the failure → no tokens charged (no double-count).
    assert reply.input_tokens == 0
    assert reply.output_tokens == 0
    assert reply.tool_invocations == []


async def test_stream_non_200_is_failsoft(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream boom")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(claude_mod.httpx, "AsyncClient", _factory)

    events = await _collect(
        _adapter().respond_streaming(
            message="anything",
            history=[],
            tool_specs=[],
            run_tool=_noop_run_tool,
        )
    )
    assert isinstance(events[-1], StreamDone)
    assert events[-1].reply.input_tokens == 0
    assert events[-1].reply.output_tokens == 0


# ===========================================================================
# Local-first: mock adapter default streaming stays deterministic, no network
# ===========================================================================


async def test_mock_adapter_default_streaming_is_deterministic():
    """The mock adapter inherits the base `respond_streaming` — coarse chunks of
    its deterministic answer, ToolDeltas first, then a StreamDone. No network."""

    async def run_tool(tool_name: str, raw_args: dict) -> ToolInvocation:
        return ToolInvocation(
            tool=tool_name,
            args={},
            result={"total": 0, "items": []},
            error=None,
        )

    adapter = MockAssistantAdapter({"provider": "mock"})
    events = await _collect(
        adapter.respond_streaming(
            message="list invoices",
            history=[],
            tool_specs=[],
            run_tool=run_tool,
        )
    )

    tool_deltas = [e for e in events if isinstance(e, ToolDelta)]
    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    done = [e for e in events if isinstance(e, StreamDone)][0]

    assert len(tool_deltas) == 1  # mock routes to exactly one tool
    # ToolDelta(s) precede TextDelta(s).
    assert next(i for i, e in enumerate(events) if isinstance(e, ToolDelta)) < next(
        i for i, e in enumerate(events) if isinstance(e, TextDelta)
    )
    # Lossless: concatenated chunks == the assembled answer.
    assert "".join(d.text for d in text_deltas) == done.reply.answer
    # Deterministic token estimate carried through unchanged.
    assert done.reply.input_tokens > 0
    assert done.reply.output_tokens > 0
    assert events[-1] is done


def test_dispatcher_downgrades_claude_to_mock_without_key():
    """Local-first rail: `claude` with no key falls back to `mock` — so a fresh
    clone (and the test suite) never needs a real Anthropic key."""
    adapter = get_assistant_adapter(
        {"provider": "claude", "api_key": "", "model": "claude-opus-4-8"}
    )
    assert isinstance(adapter, MockAssistantAdapter)


def test_dispatcher_keeps_claude_with_key():
    adapter = get_assistant_adapter(
        {"provider": "claude", "api_key": "sk-test", "model": "claude-opus-4-8"}
    )
    assert isinstance(adapter, ClaudeAssistantAdapter)


# ===========================================================================
# Base AssistantReply is a plain dataclass — sanity that StreamDone carries it
# ===========================================================================


def test_stream_done_carries_assistant_reply():
    reply = AssistantReply(answer="hi", input_tokens=1, output_tokens=2, provider="claude")
    done = StreamDone(reply=reply)
    assert done.reply.answer == "hi"
    assert done.reply.input_tokens == 1
