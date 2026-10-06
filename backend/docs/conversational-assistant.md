# Conversational AP Assistant

A per-user, per-tenant natural-language assistant over a **fixed, typed
toolset** — ten read-only tools (five general, five cash-flow copilot) that
run only against the caller's current tenant. The assistant never exposes raw
SQL and never reads another tenant's data; the model can only emit one of the
ten fixed tool calls with typed, clamped parameters.

**Local-AI default.** `pnpm dev` ships with `FEOH_ASSISTANT_PROVIDER=ollama`
(in `backend/.env.development`): the `ollama` adapter drives the assistant
against a **local, tool-capable** Ollama text model (`FEOH_ASSISTANT_OLLAMA_MODEL`,
default `qwen2.5:7b`) — no key, no cloud. A real `claude` adapter
(Anthropic Messages API tool-use) is selected with `FEOH_ASSISTANT_PROVIDER=claude`
+ a key.

**Local-first is preserved.** The `ollama` adapter **fails soft to the
deterministic `mock` adapter** whenever Ollama is unreachable, the model isn't
pulled, or it returns no usable answer — so a fresh clone with no Ollama still
answers with zero dependencies. (The `mock` adapter routes a query to one tool
via keyword/intent heuristics, no network/key.) The `claude` adapter likewise
auto-downgrades to `mock` when `FEOH_ANTHROPIC_API_KEY` is empty. The code-level
default in `app/config.py` stays `mock`, so tests and a bare-config boot remain
deterministic; only the committed dev env selects `ollama`.

## Architecture

```
POST /api/assistant/chat
        │  (auth + tenant resolved by deps; employee roles only)
        ▼
orchestrator.run_turn
  1. budget gate (control plane, row-locked)  → 429 on exceed
  2. load/create conversation (org,user)       → tenant DB
  3. build adapter (mock | claude)
  4. run_tool closure  ── audit row (PII-safe) ── tool fn (tenant DB, savepoint)
  5. persist user + assistant messages (tenant DB)
  6. record usage (control plane upsert; no standalone commit)
```

Persist (5) runs **before** record (6) and neither commits on its own — both
ride the request's commit boundary (`get_tenant_db` / `get_control_db` exit). So
a turn that unwinds after the tool work rolls back the conversation rows, the
audit rows, **and** the token debit together: usage is never charged for a turn
whose conversation/audit rows never landed.

`run_turn_streaming` (the SSE path, `POST /chat/stream`) reuses steps 1–6 via
the shared `_prepare_turn` + `_build_run_tool` helpers, then yields the answer
as SSE events. Because a streamed body defers the request commit boundary, it
commits steps 5–6 **explicitly inside the generator** to preserve the same
all-or-nothing coupling — see [Streaming (SSE)](#streaming-sse--post-apiassistantchatstream).

Tenant isolation **and** audit logging live in the orchestrator's `run_tool`
closure — not in the adapters. Both adapters call `run_tool`; neither touches
the DB or the audit infra directly. A leaked/spoofed `X-Tenant-Slug` can't widen
access because `get_tenant` already cross-checks the JWT `org` claim, and every
tool is bound to that one tenant session.

### Adapter registry (`app/services/assistant/`)

Mirrors the extraction-adapter registry (`@register_assistant_adapter` +
`get_assistant_adapter`). Config is assembled from settings:

```python
{"provider": settings.assistant_provider,             # "mock" | "claude" | "ollama"
 "api_key":  settings.anthropic_api_key,              # reused — no new secret (claude)
 "model":    settings.assistant_model or settings.extraction_model,  # claude model id
 "ollama_model":    settings.assistant_ollama_model,  # local tool-capable text model
 "ollama_base_url": settings.ollama_base_url}         # shared with the extraction adapter
```

`mock` is the **synchronous-dispatch fallback** for `claude` (no key). `ollama`
can't be probed synchronously, so it falls back to `mock` at **call** time
inside the adapter instead.

- **`mock_adapter.py`** (default) — ordered keyword/intent rules route the
  message to one tool, run it, and format a deterministic templated answer.
  Token counts are a deterministic estimate (`len//4`) so the meter + budget
  path are exercised identically to the claude path.
- **`claude_adapter.py`** — raw `httpx` POST to
  `https://api.anthropic.com/v1/messages` (house style; matches
  `extraction_adapters/claude_vision.py`), `thinking: {"type": "adaptive"}`,
  the ten tools as Anthropic tool schemas, and a manual tool-use loop capped at
  `FEOH_ASSISTANT_MAX_TOOL_HOPS`. The model id resolves from config
  (`FEOH_ASSISTANT_MODEL` → falls back to `FEOH_EXTRACTION_MODEL`) — never
  hardcoded. Real `usage` tokens are summed across hops. **Streaming**: it also
  implements `respond_streaming`, a `stream: true` variant of the same tool-use
  loop that forwards the Anthropic Messages SSE `text_delta`s as they arrive
  (true per-token passthrough) — see [Streaming (SSE)](#streaming-sse--post-apiassistantchatstream).
- **`ollama_adapter.py`** (committed dev default) — raw `httpx` POST to a local
  Ollama `/api/chat` with the ten tools converted to OpenAI-style function
  schemas, the same `FEOH_ASSISTANT_MAX_TOOL_HOPS` loop, and `prompt_eval_count` /
  `eval_count` summed as the usage tokens. Uses a dedicated **tool-capable text
  model** (`FEOH_ASSISTANT_OLLAMA_MODEL`, not the vision model used for
  extraction). Robustness: many local models emit the tool call as JSON *text*
  rather than a structured `tool_calls` field — `_parse_text_tool_calls`
  recovers those so the tool still runs. Any failure (Ollama down, model not
  pulled, non-200, no prose answer) **fails soft to `mock`**. Pull a model once
  with e.g. `ollama pull qwen2.5:7b` (a tool-capable instruct model; coder /
  vision variants either reject `tools` or format answers poorly); see
  [Local AI testing](local-ai-testing.md). Note that not every Ollama model
  supports tool-calling (`deepseek-r1`, vision-only models reject `tools` with a
  400) — pick one that does (`qwen2.5*`, `llama3.1`, `mistral-nemo`, …); the
  fail-soft keeps the assistant answering regardless.

## The ten tools

Each is an `async def(db, *, org_id, entity_id, current_user_id, params)` over
the **current tenant** session, returning a Pydantic model. Money is `Decimal`
end to end; transport uses `model_dump(mode="json")` (Decimal → string, never
float). Params clamp limits so an odd model arg can't request an unbounded scan.

| Tool | Wraps | Returns |
|------|-------|---------|
| `list_invoices` | filtered `select(Invoice)` (entity-scoped) | invoice summaries + total + applied filters |
| `get_vendor_spend` | `services.analytics.compute_supplier_concentration` (statuses from `app/api/analytics._COMMITTED_STATUSES`) | top-N vendors + share %, reporting currency |
| `list_pending_approvals` | `ready_for_review` invoices ⋈ active approval `WorkflowStep` | approval-queue rows (`assignee=me\|anyone`) |
| `get_payment_forecast` | `services.analytics.bucket_outflows` (`_COMMITTED_STATUSES` + `_PENDING_STATUSES`) | due-dated outflow buckets + total |
| `find_invoices_by_text` | `services.rag.retrieve_similar` (pgvector; mock embeddings by default) | similar invoices + non-PII snippet |
| `get_cashflow_forecast` | cash-flow copilot (`tools/cashflow.py`) | projected outflow by period: committed / pending / discount-eligible |
| `get_cash_position` | cash-flow copilot (`tools/cashflow.py`) | running balance per period + first shortfall |
| `run_payment_whatif` | cash-flow copilot (`tools/cashflow.py`) | pay-early / on-time / late scenario comparison |
| `optimize_discount_capture` | discount optimizer (`tools/optimizer.py`) | worthwhile discount offers ranked by annualized return |
| `propose_payment_plan` | discount optimizer (`tools/optimizer.py`) | proposed plan: cash curve + discounts to capture (never moves money) |

**What each caller can reach.** Every tool is org- and entity-scoped (below)
and conversations are private to their author (next section). Beyond that,
only the five cash-flow tools carry a per-tool role gate:
`ToolSpec.allowed_roles = FINANCE_LEADER_ROLES` (`admin` / `ap_manager` /
`cfo`), checked in the orchestrator's `run_tool` before any read, along with
the `FEOH_CASHFLOW_COPILOT_ENABLED` master switch — a clerk, or a disabled
deployment, gets a clean refusal tool result. The five general tools have no
`allowed_roles`, so any of the four employee roles can call them.

> **Note:** that includes `get_payment_forecast`, which returns the same
> due-dated outflow figures the REST cash-flow forecast gates to admin / CFO —
> see `docs/known-issues.md` § "The assistant's payment-forecast tool skips the
> forecast's role gate".

`list_invoices` re-builds the filter SELECT directly rather than importing from
`app/api/invoices.py` (frozen during in-flight multi-entity work) — see
[Deferred](#deferred). All tools narrow reads to one subsidiary when an
`X-Entity-ID` is selected: the four SQL tools via `apply_entity_scope`, and
`find_invoices_by_text` by threading `entity_id` into `retrieve_similar`, which
joins each embedding back to its `Invoice.entity_id` (the embedding row carries
no `entity_id` of its own). With no entity selected (`entity_id=None`) every
tool returns the consolidated all-entities view.

## Conversation history (tenant-scoped)

`assistant_conversations` + `assistant_messages` live in each tenant DB —
conversation content is tenant business data and inherits tenant isolation.
Every conversation read filters `(organization_id == jwt_org, user_id ==
current_user.id)`: a user sees only their own threads, and another user's /
tenant's conversation id **404s** (not 403, so it can't enumerate).

## Token budget + usage meter (control plane)

`assistant_usage` lives in the control plane: billing is a per-org concern and
one upsert-on-`(org, period)` row enforces the cap without fanning a sum across
every tenant DB on each call. (It is in `tenant_provisioning.CONTROL_TABLES` and
is created there — unlike the `extraction_usage` / `card_rebates` meters, which
despite the same per-org framing are tenant-local. This line used to cite
`extraction_usage` as the precedent; it was never a control-plane table.)

- **Enforcement** — `usage.assert_within_budget` at the **top of `run_turn`**,
  before any adapter/model/tool work. It takes a **`SELECT … FOR UPDATE`** on
  the `(org, period)` meter row to make the read atomic, then **commits
  immediately** — the lock is held only for that one quick round-trip, never
  across the model call / SSE stream that follows. An earlier version held the
  lock until the control transaction committed at request end, which
  serialized an org to **one in-flight `/chat` turn at a time** (every other
  concurrent turn blocked on the same row until the first turn's entire
  response finished streaming). Committing right after the check trades
  perfect serialization for a small, bounded race: two turns whose checks land
  within the same short window can both read `used < budget` and both proceed,
  so the cap can be overshot by at most a handful of concurrent turns' worth of
  tokens before the next check catches it — an acceptable trade for a soft
  usage-shaping guardrail (not a money invariant). Budget `0` disables the cap
  (matching the `FEOH_MAX_CONCURRENT_SESSIONS=0` convention). Per-org override in
  `Organization.settings.assistant.monthly_token_budget` beats the platform
  default. (A single turn is still bounded by `max_tokens ×
  FEOH_ASSISTANT_MAX_TOOL_HOPS`, bounding how large that overshoot can get.)
- **Refusal contract** — `AssistantBudgetExceeded` → **HTTP 429** with
  `{"detail": "...", "code": "assistant_budget_exceeded", "used", "budget",
  "period"}`. No tool runs, no model call, nothing persisted.
- **Recording** — `usage.record` upserts the meter (atomic
  `INSERT … ON CONFLICT DO UPDATE` increment) but **does not commit**: it flushes
  onto the request's `control_db` and rides the request commit boundary
  alongside the tenant-side conversation + audit rows, so the three commit or
  roll back together. The meter is the single source of truth for the cap and
  for `GET /api/assistant/usage`.

## Audit trail

Every tool call writes one append-only audit row **before** the tool returns
data, via `dispatch_audit` (action `assistant.tool_invoked`, entity_type
`assistant_conversation`). A multi-hop claude turn writes one row per hop. The
`details` payload carries the tool name + a **PII-safe arg shape** — never
values: `find_invoices_by_text` logs `{"query_len": N, "k": K}` (never the query
text); `list_invoices` logs `{"status": [...], "has_amount_filter": bool, ...}`;
`get_vendor_spend` logs `{"period", "top_n"}`. No amount, bank, or tax value
ever enters the trail.

The `find_invoices_by_text` **answer/snippet** (not the audit row) does include
the matched invoice's `amount` — by design: it's surfaced only to an already
-authenticated same-tenant employee, and it's the same field `list_invoices`
returns to that user. PII (`vendor_tax_id`, `bank_details`, addresses) is held
out by the `_SNIPPET_FIELDS` allowlist; amount/invoice-number/dates are the
non-confidential summary fields. If per-role amount confidentiality is ever
required, gate the snippet's `amount` on role — tracked as a future option, not
a current gap.

The tool's read runs inside a SAVEPOINT (`begin_nested`) so a failing query
(e.g. a schema-drifted tenant) rolls back just the tool — the audit row and the
message persistence survive, and the turn returns a clean error invocation
instead of a 500.

## API

All under `/api/assistant`, behind `get_current_user` (auth-before-everything),
employee roles only (`admin`/`ap_manager`/`ap_clerk`/`cfo`). Vendor-portal JWTs
are rejected by `get_current_user`.

| Method | Path | Body / Query | Response |
|--------|------|--------------|----------|
| POST | `/api/assistant/chat` | `{message, conversation_id?}` | `ChatResponse` |
| POST | `/api/assistant/chat/stream` | `{message, conversation_id?}` | `text/event-stream` (SSE — see below) |
| GET | `/api/assistant/conversations` | `?limit&offset` | `{items, total}` |
| GET | `/api/assistant/conversations/{id}` | — | `{conversation, messages}` |
| GET | `/api/assistant/usage` | — | `UsageResponse` |

`ChatResponse.tool_invocations[*].result` is the verbatim tool ReturnModel dump
— already chartable (e.g. `get_vendor_spend.vendors[]` → bar chart). The chart
UI reads the structured `result`; the top-level `answer` is the prose.

## Streaming (SSE) — `POST /api/assistant/chat/stream`

Streaming counterpart of `/chat`: **identical** request body (`{message,
conversation_id?}`), auth, tenant resolution, RBAC (`_ASSISTANT_ROLES`), and
entity scoping. The response is a `StreamingResponse(media_type=
"text/event-stream")` with `Cache-Control: no-cache`, `X-Accel-Buffering: no`,
and `Connection: keep-alive`. The frontend treats the terminal `done` event as
the source of truth and falls back to the same handling as `/chat` on a 429.

### Event protocol

Standard SSE framing — `event: <name>`, then `data: <single-line JSON>`, then a
blank line. The framing lives in `app/services/assistant/sse.py` (pure, DB-free,
unit-tested) so the event shapes can't drift between the orchestrator and the
route. Event names + data shapes:

| Event | Data | When |
|-------|------|------|
| `tool` | `{"tool", "args", "result"\|null, "error"\|null}` (same shape as `ToolInvocationOut`) | one per tool invocation, emitted **before** the prose so the UI can render a chart while text streams |
| `delta` | `{"text": "<chunk>"}` | incremental answer text; concatenating every `delta.text` reconstructs `done.answer` exactly (lossless). On `claude` this is **real per-token passthrough** (one frame per Anthropic `text_delta`); on `mock`/`ollama` it is word-chunking of the assembled answer |
| `done` | `{"conversation_id", "answer", "tool_invocations": [...], "usage": {"input_tokens", "output_tokens"}}` | terminal, authoritative payload |
| `error` | `{"code", "detail"}` | only for failures **after** the 200/stream has started |

The stream is driven by the adapter's `respond_streaming` generator, which the
orchestrator iterates and forwards onto the wire: a `ToolDelta` becomes a `tool`
frame (emitted as the tool completes, before the prose that cites it), a
`TextDelta` becomes a `delta` frame, and a terminal `StreamDone` carries the
assembled `AssistantReply` used for persistence + the final `done` frame
(`app/services/assistant/base.py` defines the three event types).

- **`claude` — true per-token passthrough.** `respond_streaming` runs the same
  server-orchestrated tool-use loop, but each hop is a `stream: true` request:
  the Anthropic `content_block_delta` / `text_delta` events are forwarded as
  `TextDelta`s the instant they arrive, so the SPA renders the model's natural
  -language text token-by-token. Tool-use blocks (whose args stream as
  `input_json_delta` fragments) still drive the audited `run_tool` and surface
  as `ToolDelta`s, and the multi-hop loop feeds tool results back exactly as the
  non-streaming path does.
- **`mock` / `ollama` — deterministic chunking.** These inherit the base
  `respond_streaming`, which runs the non-streaming `respond` to completion then
  replays it as `ToolDelta`s + coarse word-chunk `TextDelta`s. No network, so
  local-first + tests stay deterministic and never need an Anthropic key. The
  `claude` adapter still **fails soft to `mock`** when unkeyed (dispatcher
  downgrade), so a fresh clone streams the deterministic answer.

The wire contract (`tool`/`delta`/`done`/`error`) is identical across adapters —
only how finely `delta`s are produced differs.

#### Token accounting under streaming

The meter stays exactly as accurate streaming as non-streaming. The `claude`
streaming generator reads usage straight off the Anthropic SSE events:
`input_tokens` from `message_start.message.usage.input_tokens` and
`output_tokens` from the cumulative `message_delta.usage.output_tokens` (the
running total the API emits per response), summed across tool-use hops — the
same sum the non-streaming `respond` computes from each hop's `usage`. The
assembled `AssistantReply` (on `StreamDone`) carries those totals, and the
orchestrator records them via `usage.record` inside the same in-generator commit
as the conversation + audit rows (see [Transactional invariant](#transactional-invariant-under-streamingresponse)).
A mid-stream claude failure fails soft — the generator yields a fallback
`TextDelta` + `StreamDone` and never raises, so it never double-charges tokens
(only what actually streamed before the failure is counted) and the
budget/commit path is unaffected. Covered by
`tests/test_assistant_claude_stream.py` (per-token order, multi-hop usage sum,
fail-soft, mock determinism, no-key downgrade) +
`tests/test_assistant_stream.py` (the claude passthrough lands on the SSE wire
through the real commit path).

### Budget refusal is a real HTTP 429, before the stream

`usage.assert_within_budget` runs in the **endpoint**, before the
`StreamingResponse` is constructed — so an over-budget org gets a real HTTP 429
with the **same body shape** as `/chat` (`{"detail", "code":
"assistant_budget_exceeded", "used", "budget", "period"}`), never an in-stream
`error` event. The `error` event is reserved for failures that happen *after*
the 200 has been sent.

### Transactional invariant under StreamingResponse

With a streamed body the request-scoped session teardown (`get_tenant_db` /
`get_control_db`) fires only after the body is fully drained, so the streaming
generator (`run_turn_streaming`) commits **explicitly inside itself** rather
than relying on that teardown: after `_persist_turn` + `usage.record` it calls
`await tenant_db.commit()` then `await control_db.commit()`. Any failure rolls
**both** back and emits an `error` event — so the conversation + audit rows and
the token debit still land or unwind **together**, and usage is never charged
for a turn whose rows didn't land (the same coupling `/chat` gets from the
shared request commit boundary). The dependency teardown's later `commit()` is a
no-op on the already-committed (or already-rolled-back) sessions. Covered by
`tests/test_assistant_stream.py` (success-path coupling + a forced
`_persist_turn` failure asserting no usage debit).

The streaming and non-streaming paths share their setup (budget gate →
conversation load/create → history → adapter → audited `run_tool` closure) via
`orchestrator._prepare_turn` + `_build_run_tool`, so the two can never diverge
on the security-critical isolation/audit bits.

## Configuration (`FEOH_` prefix)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_ASSISTANT_PROVIDER` | `mock` | `mock` (local-first) \| `claude` |
| `FEOH_ASSISTANT_MODEL` | (empty) | Model id; empty → `FEOH_EXTRACTION_MODEL` (claude-opus-4-8 family) |
| `FEOH_ASSISTANT_MONTHLY_TOKEN_BUDGET` | `200000` | Per-org/month token cap; `0` disables |
| `FEOH_ASSISTANT_MAX_TOOL_HOPS` | `4` | Claude tool-use loop cap (cost bound) |
| `FEOH_ANTHROPIC_API_KEY` | (empty) | Reused from extraction — **no new secret**. Empty → auto-downgrade `claude`→`mock` |

## Migration

`0032_assistant` is branch-aware (gated on the `invoices` table): tenant DBs get
`assistant_conversations` + `assistant_messages`; the control plane gets
`assistant_usage`. Apply with
`FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head` →
`python scripts/migrate_all_tenants.py` → `alembic upgrade head` (control). Fresh
tenants get the conversation tables via `tenant_provisioning._create_tenant_tables`
(`create_all`); `assistant_usage` is in `CONTROL_TABLES` so it's never created on
a tenant DB.

## Deferred (tracked follow-ups)

1. **Chart-rendering UI / example-prompt empty state** — frontend only; the API
   already returns chartable structured `result` (and `tool` SSE events stream
   it before the prose).
2. **BYO-LLM beyond claude** (openai/etc.) — add adapters under the same
   registry; the registry is already open for it.
3. **Shared `invoice_queries.py`** — `list_invoices` re-builds the filtered
   SELECT because `app/api/invoices.py` is frozen this session (in-flight
   multi-entity work). Durable fix: extract the canonical filter/paginate into
   `app/services/` and have both the invoices router and the tool call it.
   Trigger: once the multi-entity invoices work lands and the file is editable.
