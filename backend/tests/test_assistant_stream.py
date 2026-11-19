"""Conversational AP Assistant — SSE streaming (`POST /api/assistant/chat/stream`).

Three layers:

  - **Pure unit** (`sse` helpers): framing + lossless answer chunking, no DB.
  - **Real-Postgres** (`realdb`): the full streaming HTTP surface against a live
    tenant DB, proving the wire contract (tool → delta → done frames), the
    transactional invariant (conversation + messages + tool-audit row AND the
    usage debit all land together), and the 429-before-stream behaviour.

The deterministic mock adapter (no key) keeps every assertion stable.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal

from app.services.assistant.base import ToolInvocation
from app.services.assistant.sse import (
    iter_answer_chunks,
    sse_done,
    sse_error,
    sse_text_delta,
    sse_tool_event,
)

# ===========================================================================
# Layer 1 — SSE framing + chunking (pure, no DB)
# ===========================================================================


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE text stream into ``[(event_name, data_dict), ...]``."""
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event_name = None
        data_line = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_line = line[len("data: ") :]
        assert event_name is not None, f"frame missing event line: {block!r}"
        assert data_line is not None, f"frame missing data line: {block!r}"
        events.append((event_name, json.loads(data_line)))
    return events


def test_chunks_reassemble_to_answer_exactly():
    answer = "You have 3 invoice(s) awaiting your approval.\n• A-1 — Acme — 100 USD"
    chunks = iter_answer_chunks(answer)
    assert chunks, "a non-empty answer must produce at least one chunk"
    assert "".join(chunks) == answer


def test_empty_answer_yields_no_delta_frames():
    assert iter_answer_chunks("") == []


def test_delta_frames_are_single_data_lines_with_valid_json():
    frames = [sse_text_delta(chunk) for chunk in iter_answer_chunks("hello world")]
    assert frames
    for frame in frames:
        assert frame.startswith("event: delta\n")
        assert frame.endswith("\n\n")
        # Exactly one data line, parseable as JSON with a "text" key, no embedded newline.
        data_lines = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
        assert len(data_lines) == 1
        payload = json.loads(data_lines[0][len("data: ") :])
        assert "text" in payload


def test_tool_frame_shape():
    inv = ToolInvocation(
        tool="get_vendor_spend",
        args={"period": "ytd", "top_n": 10},
        result={"vendors": [], "total_spend": "0"},
        error=None,
    )
    name, data = _parse_sse(sse_tool_event(inv))[0]
    assert name == "tool"
    assert data == {
        "tool": "get_vendor_spend",
        "args": {"period": "ytd", "top_n": 10},
        "result": {"vendors": [], "total_spend": "0"},
        "error": None,
    }


def test_done_frame_shape():
    conv_id = uuid.uuid4()
    inv = ToolInvocation(tool="list_invoices", args={}, result={"items": []}, error=None)
    name, data = _parse_sse(
        sse_done(
            conversation_id=conv_id,
            answer="hi",
            tool_invocations=[inv],
            usage_in=5,
            usage_out=3,
        )
    )[0]
    assert name == "done"
    assert data["conversation_id"] == str(conv_id)
    assert data["answer"] == "hi"
    assert data["usage"] == {"input_tokens": 5, "output_tokens": 3}
    assert data["tool_invocations"][0]["tool"] == "list_invoices"


def test_error_frame_shape():
    name, data = _parse_sse(sse_error(code="stream_failed", detail="ValueError"))[0]
    assert name == "error"
    assert data == {"code": "stream_failed", "detail": "ValueError"}


# ===========================================================================
# Layer 2 — realdb helpers (mirrors test_assistant.py)
# ===========================================================================


async def _seed_invoice(
    session, org_id, entity_id, *, number, vendor_name, amount, status="approved"
):
    from app.models.invoice import Invoice

    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        entity_id=entity_id,
        invoice_number=number,
        vendor_name=vendor_name,
        amount=Decimal(str(amount)),
        currency="USD",
        status=status,
        invoice_date=date.today(),
    )
    session.add(inv)
    return inv


async def _default_entity_id(session, org_id):
    from sqlalchemy import text

    row = (
        await session.execute(
            text("SELECT id FROM entities WHERE organization_id = :o AND is_default"),
            {"o": org_id},
        )
    ).first()
    return row[0]


async def _set_org_budget(realdb, key, budget):
    from sqlalchemy import update

    from app.models.organization import Organization

    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        settings["assistant"] = {"monthly_token_budget": budget}
        await s.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await s.commit()


async def _clear_org_budget(realdb, key):
    from sqlalchemy import update

    from app.models.organization import Organization

    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        settings.pop("assistant", None)
        await s.execute(
            update(Organization).where(Organization.id == info.org_id).values(settings=settings)
        )
        await s.commit()


async def _clear_usage(realdb, key):
    from sqlalchemy import delete

    from app.models.assistant import AssistantUsage

    info = realdb.info(key)
    async with realdb.control_sessionmaker()() as s:
        await s.execute(delete(AssistantUsage).where(AssistantUsage.organization_id == info.org_id))
        await s.commit()


async def _read_stream(client, *, message: str, conversation_id: str | None = None) -> str:
    body: dict = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    chunks: list[str] = []
    async with client.stream("POST", "/api/assistant/chat/stream", json=body) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"
        async for piece in resp.aiter_text():
            chunks.append(piece)
    return "".join(chunks)


# ===========================================================================
# Layer 2 — wire contract: tool + delta + done; deltas reassemble to done.answer
# ===========================================================================


async def test_stream_emits_tool_delta_and_done(realdb):
    """A streamed turn returns text/event-stream with at least one `tool` event,
    >=1 `delta` whose concatenated text == done.answer, and a terminal `done`
    carrying a valid conversation_id + usage. Uses a chartable vendor-spend
    query so the tool event has structured output to render."""
    await _clear_usage(realdb, "a")
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent_a, number="V-1", vendor_name="BigCo", amount="800.00")
        await _seed_invoice(
            sa, a.org_id, ent_a, number="V-2", vendor_name="SmallCo", amount="200.00"
        )
        await sa.commit()

    try:
        async with realdb.client(key="a", role="admin") as c:
            raw = await _read_stream(
                c, message="which vendors are we paying the most this quarter?"
            )
    finally:
        await _clear_usage(realdb, "a")

    events = _parse_sse(raw)
    names = [n for n, _ in events]

    tool_events = [d for n, d in events if n == "tool"]
    delta_events = [d for n, d in events if n == "delta"]
    done_events = [d for n, d in events if n == "done"]

    assert len(tool_events) >= 1, names
    assert tool_events[0]["tool"] == "get_vendor_spend"
    # The structured tool result is present (chartable) — money as string Decimal.
    assert tool_events[0]["result"]["total_spend"] == "1000.00"

    assert len(delta_events) >= 1, names
    assert len(done_events) == 1, names
    done = done_events[0]

    # tool event(s) precede the first delta (UI can chart while text streams).
    assert names.index("tool") < names.index("delta")
    # `done` is the terminal frame.
    assert names[-1] == "done"

    # Concatenated delta text reconstructs the authoritative answer exactly.
    streamed_text = "".join(d["text"] for d in delta_events)
    assert streamed_text == done["answer"]

    # Valid conversation_id + usage on the done payload.
    uuid.UUID(done["conversation_id"])  # raises if malformed
    assert done["usage"]["input_tokens"] > 0
    assert done["usage"]["output_tokens"] > 0


# ===========================================================================
# Layer 2 — 429 BEFORE the stream starts; nothing persisted
# ===========================================================================


async def test_stream_over_budget_returns_429_not_in_stream_error(realdb):
    """An over-budget org gets a real HTTP 429 (same body shape as /chat) before
    the stream opens — never a 200 with an in-stream error. Nothing persists."""
    from sqlalchemy import func, select

    from app.models.assistant import Conversation

    await _clear_usage(realdb, "a")
    # Budget of 1: the first turn spends >1 token, so the SECOND turn is refused.
    await _set_org_budget(realdb, "a", 1)
    try:
        async with realdb.client(key="a", role="admin") as c:
            first = await _read_stream(c, message="list invoices")
            assert _parse_sse(first)[-1][0] == "done"

            second = await c.post("/api/assistant/chat/stream", json={"message": "list invoices"})
        assert second.status_code == 429, second.text
        detail = second.json()["detail"]
        assert detail["code"] == "assistant_budget_exceeded"
        assert detail["budget"] == 1
        assert detail["used"] >= 1

        # The refused turn left no conversation behind beyond the first.
        mk_a = realdb.sessionmaker("a")
        async with mk_a() as sa:
            conv_count = (
                await sa.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.organization_id == realdb.info("a").org_id)
                )
            ).scalar_one()
        assert conv_count == 1, "the 429'd turn must not create a conversation"
    finally:
        await _clear_org_budget(realdb, "a")
        await _clear_usage(realdb, "a")


# ===========================================================================
# Layer 2 — transactional invariant: rows + usage debit land together
# ===========================================================================


async def test_stream_commits_conversation_audit_and_usage_together(realdb):
    """After a successful stream, the conversation + both messages + the
    `assistant.tool_invoked` audit row exist in the tenant DB AND the
    `assistant_usage` meter reflects the token debit in the control DB —
    proving the explicit in-generator commit lands all of them together
    (the streaming-teardown invariant)."""
    from sqlalchemy import select

    from app.models.assistant import AssistantUsage, Conversation, ConversationMessage
    from app.models.workflow import AuditLog

    await _clear_usage(realdb, "a")
    a = realdb.info("a")
    mk_a = realdb.sessionmaker("a")
    async with mk_a() as sa:
        ent_a = await _default_entity_id(sa, a.org_id)
        await _seed_invoice(sa, a.org_id, ent_a, number="T-1", vendor_name="TxnCo", amount="50.00")
        await sa.commit()

    try:
        async with realdb.client(key="a", role="admin") as c:
            raw = await _read_stream(c, message="list invoices")
        done = [d for n, d in _parse_sse(raw) if n == "done"][0]
        conv_id = uuid.UUID(done["conversation_id"])

        # Tenant DB: conversation + user + assistant messages + tool-audit row.
        async with mk_a() as sa:
            conv = await sa.get(Conversation, conv_id)
            assert conv is not None
            assert conv.organization_id == a.org_id

            msg_roles = (
                (
                    await sa.execute(
                        select(ConversationMessage.role)
                        .where(ConversationMessage.conversation_id == conv_id)
                        .order_by(ConversationMessage.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert msg_roles == ["user", "assistant"]

            audit_rows = (
                (
                    await sa.execute(
                        select(AuditLog).where(AuditLog.action == "assistant.tool_invoked")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audit_rows) == 1
            assert audit_rows[0].organization_id == a.org_id
            # PII-safe shape only — no raw values.
            assert "tool" in audit_rows[0].details

        # Control DB: the usage debit matches the done payload's reported tokens.
        async with realdb.control_sessionmaker()() as cs:
            usage_row = (
                await cs.execute(
                    select(AssistantUsage).where(AssistantUsage.organization_id == a.org_id)
                )
            ).scalar_one()
        assert usage_row.request_count == 1
        assert usage_row.input_tokens == done["usage"]["input_tokens"]
        assert usage_row.output_tokens == done["usage"]["output_tokens"]
        assert usage_row.input_tokens + usage_row.output_tokens > 0
    finally:
        await _clear_usage(realdb, "a")


async def test_stream_failed_persist_charges_no_usage(realdb):
    """If `_persist_turn` raises mid-generator, BOTH sessions roll back: no
    conversation messages AND no usage debit. Proves usage is never charged for
    a turn whose rows didn't land (the coupling the streaming path must keep)."""
    from sqlalchemy import func, select

    from app.models.assistant import AssistantUsage
    from app.services.assistant import orchestrator

    await _clear_usage(realdb, "a")
    a = realdb.info("a")

    async def _boom(*args, **kwargs):
        raise RuntimeError("induced persist failure")

    original = orchestrator._persist_turn
    orchestrator._persist_turn = _boom
    try:
        async with realdb.client(key="a", role="admin") as c:
            raw = await _read_stream(c, message="list invoices")
        events = _parse_sse(raw)
        names = [n for n, _ in events]
        # The stream had already started (tool + delta) then surfaced an error.
        assert "error" in names, names
        err = [d for n, d in events if n == "error"][0]
        assert err["code"] == "stream_failed"
        assert names[-1] == "error", "error is terminal; no done after a failure"

        # No usage debited — the control rollback undid record().
        async with realdb.control_sessionmaker()() as cs:
            usage_count = (
                await cs.execute(
                    select(func.count())
                    .select_from(AssistantUsage)
                    .where(AssistantUsage.organization_id == a.org_id)
                )
            ).scalar_one()
        assert usage_count == 0, "usage must not be charged when the turn's rows didn't land"
    finally:
        orchestrator._persist_turn = original
        await _clear_usage(realdb, "a")


# ===========================================================================
# Layer 2 — claude per-token passthrough lands on the SSE wire (orchestrator)
# ===========================================================================


async def test_stream_claude_per_token_passthrough_through_orchestrator(realdb, monkeypatch):
    """With the `claude` adapter active (its Anthropic streaming client MOCKED),
    `run_turn_streaming` forwards the model's real per-token `text_delta`s as SSE
    `delta` frames, and commits the streamed usage to the control-plane meter.

    Drives `run_turn_streaming` directly with the realdb harness's sessions (the
    SSE *endpoint* is exercised by the mock-adapter tests above; here we prove
    the claude per-token deltas reach the wire and the token accounting under
    streaming stays accurate, end to end through the real commit path). No
    Anthropic key, no network.

    The unit-level adapter behaviour is in `test_assistant_claude_stream.py`."""
    import httpx

    from app.models.organization import Organization
    from app.models.user import User
    from app.services.assistant import claude_adapter as claude_mod
    from app.services.assistant import orchestrator
    from app.services.assistant.claude_adapter import ClaudeAssistantAdapter
    from app.services.assistant.orchestrator import run_turn_streaming

    await _clear_usage(realdb, "a")
    a = realdb.info("a")

    # A streaming SSE body: 4 text_delta tokens + input/output usage on the wire.
    tokens = ["Here ", "are ", "your ", "invoices."]
    frames = [
        'event: message_start\ndata: {"type":"message_start","message":'
        '{"usage":{"input_tokens":37,"output_tokens":0}}}\n\n',
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n',
    ]
    for tok in tokens:
        frames.append(
            'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            f'"delta":{{"type":"text_delta","text":{json.dumps(tok)}}}}}\n\n'
        )
    frames.append('event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n')
    frames.append(
        'event: message_delta\ndata: {"type":"message_delta",'
        '"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n'
    )
    frames.append('event: message_stop\ndata: {"type":"message_stop"}\n\n')
    sse_body = "".join(frames)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body.encode(), headers={"content-type": "text/event-stream"}
        )

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, timeout=5.0)

    monkeypatch.setattr(claude_mod.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(
        orchestrator,
        "get_assistant_adapter",
        lambda _cfg: ClaudeAssistantAdapter({"api_key": "sk-test", "model": "claude-opus-4-8"}),
    )

    from sqlalchemy import select

    from app.models.assistant import AssistantUsage

    mk_a = realdb.sessionmaker("a")
    try:
        async with realdb.control_sessionmaker()() as control_db, mk_a() as tenant_db:
            org = await control_db.get(Organization, a.org_id)
            user = (
                (await control_db.execute(select(User).where(User.organization_id == a.org_id)))
                .scalars()
                .first()
            )
            ent = await _default_entity_id(tenant_db, a.org_id)

            raw = "".join(
                [
                    frame
                    async for frame in run_turn_streaming(
                        control_db=control_db,
                        tenant_db=tenant_db,
                        org=org,
                        user=user,
                        entity_id=ent,
                        conversation_id=None,
                        message="show my invoices",
                    )
                ]
            )

        events = _parse_sse(raw)
        delta_events = [d for n, d in events if n == "delta"]
        done = [d for n, d in events if n == "done"][0]

        # Per-token passthrough: one delta per streamed token, in order, lossless.
        assert [d["text"] for d in delta_events] == tokens
        assert "".join(d["text"] for d in delta_events) == done["answer"]
        assert done["answer"] == "Here are your invoices."
        # Usage on the done payload is the streamed usage (input 37, output 9).
        assert done["usage"] == {"input_tokens": 37, "output_tokens": 9}

        # Committed to the control-plane meter — accounting under streaming holds.
        async with realdb.control_sessionmaker()() as cs:
            usage_row = (
                await cs.execute(
                    select(AssistantUsage).where(AssistantUsage.organization_id == a.org_id)
                )
            ).scalar_one()
        assert usage_row.input_tokens == 37
        assert usage_row.output_tokens == 9
    finally:
        await _clear_usage(realdb, "a")


# ===========================================================================
# Layer 2 — auth on the stream endpoint
# ===========================================================================


async def test_stream_endpoint_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post("/api/assistant/chat/stream", json={"message": "hi"})
    assert resp.status_code in (401, 403), resp.status_code
