"""Claude assistant adapter — Anthropic Messages API tool-use.

Selected only when an API key is configured. House style: a raw ``httpx`` POST
to ``https://api.anthropic.com/v1/messages`` (matches
``extraction_adapters/claude_vision.py``), not the SDK. The model id resolves
from config (``FEOH_ASSISTANT_MODEL`` → falls back to ``FEOH_EXTRACTION_MODEL``,
``claude-sonnet-5-5``) — never hardcoded. Adaptive thinking, which current
models run by default; thinking blocks are echoed back UNCHANGED (signature
included) on every tool-use hop, as the Messages API requires.

The manual tool-use loop is capped at ``FEOH_ASSISTANT_MAX_TOOL_HOPS`` to bound
cost. Each ``tool_use`` block is executed via the orchestrator's tenant-bound,
audited ``run_tool`` closure — the adapter never touches the DB.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.services.assistant.base import (
    AssistantAdapter,
    AssistantReply,
    RunTool,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolDelta,
    ToolInvocation,
)
from app.services.assistant.dispatcher import register_assistant_adapter

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_UNAVAILABLE = "Sorry — the assistant is unavailable right now."


async def _iter_sse_events(resp: httpx.Response) -> AsyncIterator[dict]:
    """Parse the Anthropic Messages SSE stream into decoded ``data:`` JSON dicts.

    The Messages streaming API frames each event as an ``event:`` line followed
    by a ``data:`` line (single-line JSON) and a blank separator. We only need
    the ``data`` payloads (each already carries its own ``type``), so we yield
    one decoded dict per ``data:`` line. Malformed/keepalive lines are skipped.
    """
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


_SYSTEM_PROMPT = (
    "You are the AP assistant for an accounts-payable application. You answer "
    "questions about the CURRENT tenant's accounts-payable data only. You may "
    "ONLY use the provided tools to read data — you cannot run arbitrary "
    "queries, you cannot access another tenant's data, and you must refuse any "
    "request that falls outside what the tools expose (e.g. modifying data, "
    "sending payments, or reading another organization's records). When a tool "
    "returns results, answer the user's question concisely and cite concrete "
    "figures from the tool output. Money values are exact — never round them "
    "away."
)


@register_assistant_adapter("claude")
class ClaudeAssistantAdapter(AssistantAdapter):
    """Anthropic Messages API tool-use adapter."""

    provider_name = "claude"

    def _model(self) -> str:
        return self.config.get("model") or settings.extraction_model

    def _headers(self) -> dict:
        return {
            "x-api-key": self.config.get("api_key", ""),
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _request_body(self, model: str, messages: list[dict], tool_specs: list[dict]) -> dict:
        """The Messages API body for one hop. ``stream`` is added by the caller."""
        return {
            "model": model,
            # Thinking counts toward this ceiling; 4096 could be spent on
            # thinking before any answer text on a thinking-on model. A cap,
            # not a charge — the per-org token budget meters actual usage.
            "max_tokens": 16000,
            "system": _SYSTEM_PROMPT,
            "thinking": {"type": "adaptive"},
            "tools": tool_specs,
            "messages": messages,
        }

    async def respond(
        self,
        *,
        message: str,
        history: list[dict],
        tool_specs: list[dict],
        run_tool: RunTool,
    ) -> AssistantReply:
        model = self._model()
        messages: list[dict] = list(history) + [{"role": "user", "content": message}]
        max_hops = max(1, settings.assistant_max_tool_hops)

        invocations: list[ToolInvocation] = []
        input_tokens = 0
        output_tokens = 0
        answer_parts: list[str] = []

        async with httpx.AsyncClient(timeout=60) as client:
            for _hop in range(max_hops):
                body = self._request_body(model, messages, tool_specs)
                try:
                    resp = await client.post(_API_URL, json=body, headers=self._headers())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Assistant claude API call failed: %s", exc.__class__.__name__)
                    answer_parts.append(_UNAVAILABLE)
                    break

                if resp.status_code != 200:
                    logger.warning("Assistant claude API error %s", resp.status_code)
                    answer_parts.append(_UNAVAILABLE)
                    break

                data = resp.json()
                usage = data.get("usage", {}) or {}
                input_tokens += int(usage.get("input_tokens", 0) or 0)
                output_tokens += int(usage.get("output_tokens", 0) or 0)

                content_blocks = data.get("content", []) or []
                # Collect any text the model emitted this hop.
                for block in content_blocks:
                    if block.get("type") == "text":
                        answer_parts.append(block.get("text", ""))

                stop_reason = data.get("stop_reason")
                if stop_reason != "tool_use":
                    break

                # Echo the assistant turn back, then run every tool_use block.
                messages.append({"role": "assistant", "content": content_blocks})
                tool_results = []
                for block in content_blocks:
                    if block.get("type") != "tool_use":
                        continue
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {}) or {}
                    invocation = await run_tool(tool_name, tool_input)
                    invocations.append(invocation)
                    if invocation.error:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "is_error": True,
                                "content": invocation.error,
                            }
                        )
                    else:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": json.dumps(invocation.result or {}),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        answer = "\n".join(p for p in answer_parts if p).strip()
        if not answer:
            answer = "I wasn't able to produce an answer for that."
        return AssistantReply(
            answer=answer,
            tool_invocations=invocations,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=self.provider_name,
        )

    async def respond_streaming(
        self,
        *,
        message: str,
        history: list[dict],
        tool_specs: list[dict],
        run_tool: RunTool,
    ) -> AsyncIterator[StreamEvent]:
        """True per-token passthrough of the Anthropic Messages SSE stream.

        Runs the same server-orchestrated tool-use loop as ``respond``, but each
        hop is a ``stream: true`` request: the model's ``content_block_delta`` /
        ``text_delta`` events are forwarded as ``TextDelta``s the instant they
        arrive (token-by-token), tool-use blocks drive the audited ``run_tool``
        and surface as ``ToolDelta``s, and the final ``StreamDone`` carries the
        assembled reply.

        Token accounting under streaming: ``input_tokens`` comes from
        ``message_start.message.usage.input_tokens`` and ``output_tokens`` from
        the cumulative ``message_delta.usage.output_tokens`` (the running total
        the API emits per hop) — summed across hops exactly as the non-streaming
        path sums the per-response ``usage``. So the meter is identical whether
        the turn streamed or not.

        Fail-soft: any transport / HTTP error surfaces as a single ``TextDelta``
        carrying the standard unavailable message plus a terminal ``StreamDone``
        — the generator never raises, so the orchestrator's commit/usage path is
        unaffected (and tokens already streamed are still counted).
        """
        model = self._model()
        messages: list[dict] = list(history) + [{"role": "user", "content": message}]
        max_hops = max(1, settings.assistant_max_tool_hops)

        invocations: list[ToolInvocation] = []
        input_tokens = 0
        output_tokens = 0
        answer_parts: list[str] = []

        async with httpx.AsyncClient(timeout=60) as client:
            for _hop in range(max_hops):
                body = self._request_body(model, messages, tool_specs)
                body["stream"] = True

                # Per-hop accumulators rebuilt from the SSE event stream.
                content_blocks: list[dict] = []
                # tool-use arg JSON arrives as input_json_delta fragments per block index.
                tool_json_parts: dict[int, list[str]] = {}
                hop_out_tokens = 0
                hop_failed = False

                try:
                    async with client.stream(
                        "POST", _API_URL, json=body, headers=self._headers()
                    ) as resp:
                        if resp.status_code != 200:
                            await resp.aread()
                            logger.warning("Assistant claude stream error %s", resp.status_code)
                            answer_parts.append(_UNAVAILABLE)
                            yield TextDelta(text=_UNAVAILABLE)
                            hop_failed = True
                        else:
                            async for event in _iter_sse_events(resp):
                                etype = event.get("type")
                                if etype == "message_start":
                                    usage = (event.get("message") or {}).get("usage") or {}
                                    input_tokens += int(usage.get("input_tokens", 0) or 0)
                                elif etype == "content_block_start":
                                    idx = event.get("index", 0)
                                    block = dict(event.get("content_block") or {})
                                    while len(content_blocks) <= idx:
                                        content_blocks.append({})
                                    content_blocks[idx] = block
                                    if block.get("type") == "tool_use":
                                        tool_json_parts[idx] = []
                                elif etype == "content_block_delta":
                                    idx = event.get("index", 0)
                                    delta = event.get("delta") or {}
                                    dtype = delta.get("type")
                                    if dtype == "text_delta":
                                        # The genuine per-token passthrough.
                                        chunk = delta.get("text", "")
                                        if chunk:
                                            answer_parts.append(chunk)
                                            yield TextDelta(text=chunk)
                                    elif dtype == "input_json_delta":
                                        tool_json_parts.setdefault(idx, []).append(
                                            delta.get("partial_json", "")
                                        )
                                    elif dtype in ("thinking_delta", "signature_delta"):
                                        # Rebuild the thinking block exactly, so
                                        # the echo on a tool-use hop carries its
                                        # text and signature. Without the
                                        # signature the API rejects the replayed
                                        # block and the second hop fails.
                                        if 0 <= idx < len(content_blocks):
                                            blk = content_blocks[idx]
                                            if dtype == "thinking_delta":
                                                blk["thinking"] = blk.get(
                                                    "thinking", ""
                                                ) + delta.get("thinking", "")
                                            else:
                                                blk["signature"] = blk.get(
                                                    "signature", ""
                                                ) + delta.get("signature", "")
                                elif etype == "message_delta":
                                    # message_delta.usage.output_tokens is the running
                                    # total for THIS response; the last seen wins.
                                    usage = event.get("usage") or {}
                                    if "output_tokens" in usage:
                                        hop_out_tokens = int(usage.get("output_tokens", 0) or 0)
                                # message_stop / content_block_stop / ping: no-op
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Assistant claude stream call failed: %s", exc.__class__.__name__
                    )
                    answer_parts.append(_UNAVAILABLE)
                    yield TextDelta(text=_UNAVAILABLE)
                    hop_failed = True

                if hop_failed:
                    break

                # Fold this hop's cumulative output tokens into the running total.
                output_tokens += hop_out_tokens

                # Finalize tool_use blocks: attach the assembled input JSON.
                for idx, parts in tool_json_parts.items():
                    if 0 <= idx < len(content_blocks):
                        raw = "".join(parts)
                        try:
                            content_blocks[idx]["input"] = json.loads(raw) if raw else {}
                        except json.JSONDecodeError:
                            content_blocks[idx]["input"] = {}

                has_tool_use = any(b.get("type") == "tool_use" for b in content_blocks)
                if not has_tool_use:
                    break

                # Echo the assistant turn back, then run every tool_use block.
                messages.append({"role": "assistant", "content": content_blocks})
                tool_results = []
                for block in content_blocks:
                    if block.get("type") != "tool_use":
                        continue
                    invocation = await run_tool(block.get("name", ""), block.get("input", {}) or {})
                    invocations.append(invocation)
                    yield ToolDelta(invocation=invocation)
                    if invocation.error:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "is_error": True,
                                "content": invocation.error,
                            }
                        )
                    else:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id"),
                                "content": json.dumps(invocation.result or {}),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})

        answer = "".join(answer_parts).strip()
        if not answer:
            answer = "I wasn't able to produce an answer for that."
        yield StreamDone(
            reply=AssistantReply(
                answer=answer,
                tool_invocations=invocations,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider=self.provider_name,
            )
        )

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    _API_URL,
                    json={
                        "model": self._model(),
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers=self._headers(),
                )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
