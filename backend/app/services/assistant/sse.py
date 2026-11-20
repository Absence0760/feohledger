"""Server-sent-events framing for the conversational assistant stream.

Pure, DB-free helpers that turn the orchestrator's in-memory turn artifacts
(``ToolInvocation``, the assembled answer, the usage delta) into SSE frames on
the exact wire contract the frontend builds against. Keeping the framing here
(not in the orchestrator or the route) means the event shapes are unit-testable
without a DB and can't drift between the two.

Each frame is standard SSE: a ``event: <name>`` line, a ``data: <single-line
JSON>`` line, then a blank line. The JSON is emitted with ``ensure_ascii`` and
no embedded newlines so a single ``data:`` line always carries one whole event.

Event protocol (names + data shapes):
  - ``tool``  → ``{"tool", "args", "result"|null, "error"|null}`` — one per
    invocation, before the prose, so the UI can chart while text streams.
  - ``delta`` → ``{"text": "<chunk>"}`` — incremental answer text.
  - ``done``  → ``{"conversation_id", "answer", "tool_invocations": [...],
    "usage": {"input_tokens", "output_tokens"}}`` — final authoritative payload.
  - ``error`` → ``{"code", "detail"}`` — only for failures AFTER the stream
    started (a pre-stream budget refusal is a real HTTP 429, not an in-stream
    error).
"""

from __future__ import annotations

import json
import re
import uuid

from app.services.assistant.base import ToolInvocation

# Split the answer into word-ish chunks (a run of non-space followed by its
# trailing whitespace), so concatenating every delta exactly reconstructs the
# answer — no characters added or dropped.
_CHUNK_RE = re.compile(r"\S+\s*|\s+")


def _frame(event: str, data: dict) -> str:
    """Render one SSE frame. ``data`` is serialised to a single JSON line."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _invocation_payload(inv: ToolInvocation) -> dict:
    return {
        "tool": inv.tool,
        "args": inv.args or {},
        "result": inv.result,
        "error": inv.error,
    }


def sse_tool_event(inv: ToolInvocation) -> str:
    """An ``event: tool`` frame (same per-invocation shape as ToolInvocationOut)."""
    return _frame("tool", _invocation_payload(inv))


def iter_answer_chunks(answer: str) -> list[str]:
    """Split ``answer`` into transport chunks whose concatenation == ``answer``.

    Word-plus-trailing-whitespace chunks keep the deltas readable while
    guaranteeing a lossless reassembly. An empty answer yields no chunks.
    """
    if not answer:
        return []
    return _CHUNK_RE.findall(answer)


def sse_text_delta(text: str) -> str:
    """A single ``event: delta`` frame carrying one text span.

    Used by the orchestrator to forward each ``TextDelta`` as it arrives —
    a real Anthropic ``text_delta`` on the claude path, a coarse chunk on the
    mock/ollama path. Concatenating every span reconstructs the answer exactly.
    """
    return _frame("delta", {"text": text})


def sse_done(
    *,
    conversation_id: uuid.UUID,
    answer: str,
    tool_invocations: list[ToolInvocation],
    usage_in: int,
    usage_out: int,
) -> str:
    """The terminal ``event: done`` frame — the frontend's source of truth."""
    return _frame(
        "done",
        {
            "conversation_id": str(conversation_id),
            "answer": answer,
            "tool_invocations": [_invocation_payload(inv) for inv in tool_invocations],
            "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
        },
    )


def sse_error(*, code: str, detail: str) -> str:
    """An ``event: error`` frame — only for post-stream-start failures."""
    return _frame("error", {"code": code, "detail": detail})
