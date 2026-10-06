# AI Cash-Flow Copilot

**Status: Phases 1–3 SHIPPED (read-only cash Q&A + proposed payment plans +
draft-only enactment).** Phase 1 — the four read-only, finance-leader-gated
planning tools plus the `/api/cash-flow/copilot(+/stream)` façade and the
`/cash-flow` chat & cash-position chart — is built and live. Phase 2 — the
`propose_payment_plan` tool (`app/services/assistant/tools/cashflow.py`) + the
pure plan assembler (`app/services/cash_flow_plan.py`) + the plan-card UI
(`PlanCard.svelte`) — is also built and live. Phase 3 (draft-only enactment) —
the two enact routes (`POST /api/cash-flow/plans/{plan_id}/draft-run` +
`.../capture-discounts`), the `services/payment_runs.py` shared creation
service, the `payment_runs.plan_id` idempotency anchor (migration 0079), and
the plan card's "Create draft run" / "Capture N discounts" buttons — is now
built and live too. The Phase 3 **deferred bucket is now closed as well**:
**saved plans + plan-vs-actual** (the tenant-scoped `CashPlan` model, migration
`0087_cash_plans`, and the five routes over it) and **consolidated cross-entity
mode**. See §5/§6/§11/§15/§16 below.

**Author:** platform · **Target:** beyond-parity differentiator · **Est. size:** M (3 phases)

---

## 1. Summary

A natural-language, **forward-looking, advisory** copilot that answers the two
questions a finance leader actually asks about accounts payable:

> "When am I going to run low on cash?"
> "Given my cash, what should I pay early, on time, or defer — and what does it cost me?"

It combines three primitives we already have — the **cash-flow forecast**, the
**payment-timing what-if**, and the **cash-constrained discount optimizer** —
behind the existing **conversational assistant** (so it inherits tenant
isolation, audit, budgeting, and the local-first mock/ollama/claude adapter
stack), and adds a dedicated `/cash-flow` copilot surface that can *propose* a
concrete payment plan.

**Hard boundary: the copilot never moves money.** Its most privileged write is
staging a **draft** payment run (the existing idempotent, CFO-gated path). All
funding stays behind the current human-in-the-loop payment-run execute gate.
The LLM narrates and disambiguates; it never computes a number — every dollar
figure comes from a deterministic pure function, so answers are exact and
reproducible regardless of which model (or no model) is configured.

### What it is / is not

| Is | Is not |
|----|--------|
| Advisory: forecasts, scenarios, ranked recommendations | An autonomous treasurer that pays bills |
| Deterministic money math (pure functions) | An LLM doing arithmetic on your cash |
| A new set of **read-only** planning tools + one **draft-only** staging action | A new payment rail or a bypass of the CFO gate |
| Built on the existing assistant orchestrator | A parallel chat stack |

---

## 2. Why this is a competitive edge

No mid-market competitor pairs a conversational interface with a cash-constrained
early-pay optimizer:

- **Coupa / Basware** have cash-flow analytics but no NL copilot and no
  self-serve discount optimization surfaced conversationally.
- **Tipalti / Bill / Stampli / Airbase** have dashboards and (Stampli) a
  correction-learning bot, but nothing that answers *"what should I pay this
  week given a $2M cash ceiling and which discounts are worth taking?"* in one turn.
- We already ship the hard parts — the forecast, the what-if, the ROI-ranked
  optimizer, and a tool-using assistant with true tenant isolation. This wires
  them into a single high-value experience competitors would have to build from
  scratch.

It also reinforces two existing moats: the **transparent, deterministic** design
(we show the exact math, the LLM never fabricates a figure) and **self-hostable
AI** (the copilot works fully local via the `ollama`/`mock` adapters — a
data-sovereignty story no competitor has).

---

## 3. Architecture

Build **on top of** the conversational assistant
(`backend/docs/conversational-assistant.md`), not beside it. The orchestrator
(`app/services/assistant/orchestrator.py`) already gives us, for free:

- tenant isolation via the `get_tenant` chokepoint (JWT `org`-claim cross-check),
- an append-only, PII-safe audit row per tool call (`assistant.tool_invoked`),
- a row-locked control-plane token budget + 429 refusal,
- the mock / ollama / claude adapter stack with local-first fail-soft,
- SSE streaming with the identical `tool`/`delta`/`done`/`error` contract.

```
POST /api/assistant/chat            (existing route; new tools registered)
  or POST /api/cash-flow/copilot    (thin façade — see §6)
        │  auth + tenant + entity resolved by deps
        ▼
orchestrator.run_turn / run_turn_streaming
  1. budget gate (control plane, row-locked)
  2. load/create conversation (tenant DB)
  3. adapter (mock | ollama | claude)
  4. run_tool closure ── audit row (PII-safe) ── tool fn (tenant DB, savepoint)
        │
        ├─ get_cashflow_forecast   ─┐
        ├─ run_payment_whatif       ├─ NEW read-only planning tools (§4)
        ├─ optimize_discount_capture┘   (deterministic; LLM never computes money)
        └─ propose_payment_plan     ─── NEW: assembles a plan artifact (read-only;
                                          no money moves; §5)
  5. persist messages (tenant DB)
  6. record usage (control plane)
```

### Determinism invariant (the core design decision)

The LLM's only jobs are (a) turn NL into a typed tool call and (b) narrate the
returned structured result. **Every monetary figure originates from a pure
function** we already own and unit-test:

| Copilot capability | Reuses (existing, pure) |
|---|---|
| Outflow forecast by period | `services/analytics.bucket_outflows` + `_commitment_rows` (see `app/api/analytics.py::get_cashflow_forecast`) |
| Running cash position + shortfall detection | `services/analytics.compute_cash_position` (`get_cash_position`) |
| Pay-early / on-time / late scenarios | `services/analytics.apply_payment_timing_scenario` (`get_cashflow_whatif`) |
| Which discounts to capture within a cash ceiling | `services/discount_optimizer.optimize` + `services/discount_roi.compute_roi` |

Because these are deterministic, a copilot answer is byte-reproducible under the
`mock` adapter — which is exactly what makes it testable and safe to ship
local-first.

---

## 4. New assistant tools (read-only)

Registered in `app/services/assistant/tools/` alongside the existing five, added
to `TOOLS` / `TOOL_SPECS` (`tools/__init__.py`). Each is
`async def(db, *, org_id, entity_id, current_user_id, params) -> PydanticModel`,
entity-scoped, money as **exact Decimal → JSON string** (never `float`).

> Note: the *analytics HTTP endpoints* used to coerce money to `float()` for
> chart transport (`app/api/analytics.py`). They no longer do — every money
> field there is an exact decimal string too (`backend/docs/analytics.md`
> § Money serialisation), so the copilot's discipline and the dashboard's now
> agree. The tools' own contract is unchanged: exact decimal strings via
> `model_dump(mode="json")`, never float in the tool return path.

| Tool | Params (clamped) | Returns |
|------|------------------|---------|
| `get_cashflow_forecast` | `granularity∈{day,week,month}`, `horizon_days∈[7,730]`, `include_pending` | periods `[{period, scheduled, committed, pending, discount_eligible}]` + totals |
| `get_cash_position` | + `opening_balance?`, `min_balance_threshold?` | running-balance periods + `first_shortfall_period` + flagged periods |
| `run_payment_whatif` | + `grace_days∈[0,90]` | `{early, on_time, late}` each `{total_outflow, discount_captured, weighted_avg_days_to_pay}` |
| `optimize_discount_capture` | `cash_budget?`, `cost_of_capital_pct?` | ranked recommendations `[{offer_id, vendor, apr, savings, pay_by, selected}]` + `total_savings_selected` |

**Role gating per tool.** The five cash tools (the four §4 planning tools plus
`propose_payment_plan`, §5) are finance-leader reads —
`admin` / `ap_manager` / `cfo` (`api/cash_flow.COPILOT_ROLES` — wider than
analytics' own `_CFO_ROLES`, which is `admin` / `cfo`), **not** `ap_clerk`. This is stricter than the assistant's blanket four-role access, so
the `run_tool` closure enforces a per-tool `allowed_roles` check and returns a
clean "not permitted" tool result (never a 500, never leaking data) when a clerk
asks a cash question. The general assistant's `get_payment_forecast` serves the
same outflow figures and carries the same gate; the other general tools stay
open to all four roles (`backend/docs/conversational-assistant.md` § The ten
tools has the per-tool table and its drift guard).

**Audit shape (PII-safe).** Same as today: log the tool name + arg *shape*, never
values — e.g. `optimize_discount_capture` logs `{"has_budget": bool,
"cost_of_capital_pct": N}`, never vendor names or amounts. Cash figures live in
the answer (to an already-authenticated finance leader), never in the audit row.

---

## 5. The `propose_payment_plan` action (advisory, draft-only)

**All four points below are shipped.** Point 1 is Phase 2
(`propose_payment_plan` tool + `services/cash_flow_plan.py::assemble_plan`,
both read-only). Points 2–4 — the enact tiers (draft run / discount capture)
— are Phase 3, now built: `POST /api/cash-flow/plans/{plan_id}/draft-run` +
`.../capture-discounts` (`app/api/cash_flow.py`).

Given a cash ceiling and a horizon, `propose_payment_plan` assembles a **plan
artifact**: which open commitments to pay in which period, which discount
offers to capture, the resulting cash-position curve, and the captured-savings
total. It is built entirely from the pure functions above (`bucket_outflows`,
`compute_cash_position`, and the discount optimizer's own selection via
`run_discount_optimization` — the plan reuses `optimize_discount_capture`'s
exact selection rather than re-deriving it, so the two can never diverge). A
selected discount is re-timed onto its `pay_by` period at its discounted
outlay when it can be matched back to a single commitment row (a
vendor-scoped offer, or an invoice outside the forecast horizon, cannot be —
its offer id is surfaced in `unretimed_offer_ids` instead of silently
overclaiming precision on the curve).

**Safety model — this is the load-bearing part:**

1. **It never moves money and never mutates an invoice/payment.** It returns a
   proposal object. Full stop. **Shipped** — see above.
2. Enacting the plan is a **separate, explicit** user action with two tiers,
   both reusing existing gated paths — nothing new on the money path.
   **Shipped:**
   - *Capture discounts* (`POST /api/cash-flow/plans/{plan_id}/capture-discounts`):
     re-runs the SAME discount-optimizer pass (`run_discount_optimization`) to
     get the current `selected` offer ids, then flips each still-`offered` one
     `offered → accepted` via the existing `services.discount_offers.accept_offer`
     mutator (the same one `POST /api/discounts/offers/{id}/accept` uses).
     **Status-only and never moves money** (the CFO-gated payment run still
     funds). An offer no longer `offered` (already handled by a prior call, or
     a manual accept in the meantime) is skipped, not re-raised.
   - *Stage payments* (`POST /api/cash-flow/plans/{plan_id}/draft-run`):
     re-derives the SAME commitment rows the plan used (`_commitment_rows`),
     narrows them to invoices that are ACTUALLY payable right now
     (`PAYABLE_INVOICE_STATUSES` — a plan's horizon also includes
     pre-approval pipeline invoices, which can't be staged), and creates a
     **draft** `PaymentRun` via `services.payment_runs.create_payment_run_for_invoices`
     — the exact function `POST /api/payments/runs` uses (same payable-status
     gate, financial-integrity exception block, credit-memo netting, and
     CFO-threshold computation). Never `execute`s. Execution stays behind the
     current human review + `requires_cfo_approval` gate + segregation-of-duties
     (`POST /api/payments/runs/{id}/approve` then `/execute`), completely
     unchanged.
3. **Idempotency:** plan enactment reuses the existing idempotent create paths;
   re-submitting the same proposal must not create a second draft run. Dedupe
   is on the deterministic `plan_id` (§6) persisted on the draft run
   (`payment_runs.plan_id`, a partial-unique-indexed column — migration
   `0079_payment_run_plan_id`): retrying `.../draft-run` for the same
   `plan_id` returns the existing run (`created:false`, HTTP 200) instead of
   staging a second one. `capture-discounts` is naturally idempotent too — an
   offer no longer `offered` is simply excluded from the next optimizer pass.
4. **Confirmation UX:** the copilot presents the plan; the human clicks
   "Create draft run" / "Capture N discounts" on the plan card
   (`PlanCard.svelte`). The LLM cannot trigger either — the tool only
   *returns* the plan; the enact endpoints are ordinary RBAC-gated, audited,
   non-LLM routes, gated to the same finance-leader roles as the rest of the
   copilot.

So the LLM's influence ends at "here is a proposal"; every irreversible step is a
deterministic, human-initiated, already-audited action.

### Persistence

Reuse the tenant-scoped `assistant_conversations` / `assistant_messages` for the
chat. The **plan artifact is still stateless on the path that acts on it** — it
is a computed object returned in the tool result and re-derivable from its own
inputs, and every enact endpoint re-derives its commitment rows rather than
reading a stored row. `plan_id` remains the idempotency / replay key (§6), and
`payment_runs.plan_id` remains the draft-run anchor.

**Saving a plan is a separate, additive act (SHIPPED — §15).** The tenant-scoped
`CashPlan` model (`app/models/cash_plan.py`, migration `0087_cash_plans`) is
*keyed by* that same deterministic id, never a replacement for it, and nothing
in the enact path reads the table. What it adds is the one thing re-derivation
cannot give back: **what the projection said at the time**. Yesterday's plan is
not recomputable today — its horizon started from a different day and the
invoices inside it have moved on — so without a stored row "did our forecast
hold?" is unanswerable.

A snapshot is therefore **frozen**: re-saving the same `plan_id` returns the
existing row untouched rather than restating it against newer data, which would
rewrite the very baseline a variance is measured against. `entity_id` NULL on
this table means **consolidated** (a whole-group plan), not the "unstamped
legacy row" NULL migration 0029 backfilled elsewhere — the table is new, so
nothing needs backfilling and no row can be unstamped.

---

## 6. API surface

Minimal — the copilot rides the existing assistant routes; a thin façade gives
it a first-class URL and lets us set copilot-specific defaults (finance-leader
RBAC, a system-prompt hint, streaming on by default).

| Method | Path | Status | Notes |
|--------|------|--------|-------|
| POST | `/api/cash-flow/copilot` | Phase 1 · shipped | Façade over `orchestrator.run_turn`; body `{message, conversation_id?}`; RBAC `admin/ap_manager/cfo`; entity-scoped |
| POST | `/api/cash-flow/copilot/stream` | Phase 1 · shipped | SSE variant (reuses `run_turn_streaming`, identical event contract) |
| POST | `/api/cash-flow/plans/{plan_id}/draft-run` | Phase 3 · shipped | Enact: create a **draft** payment run from a proposal (idempotent on `plan_id`, audited, CFO gate at execute-time unchanged) |
| POST | `/api/cash-flow/plans/{plan_id}/capture-discounts` | Phase 3 · shipped | Enact: accept the plan's discount offers (status-only, reuses discount accept) |
| POST | `/api/cash-flow/plans/{plan_id}/save` | shipped | Freeze the proposal as a `CashPlan` snapshot. **201** on first save, **200** returning the ORIGINAL snapshot on a repeat. Read-only over the money path |
| GET | `/api/cash-flow/plans` | shipped | Saved snapshots, newest first (`?limit=`, `?consolidated=`). Entity-scoped by default |
| GET | `/api/cash-flow/plans/{plan_id}` | shipped | One snapshot + its frozen curve + `has_draft_run` |
| GET | `/api/cash-flow/plans/{plan_id}/variance` | shipped | Plan vs. actual (§15) — compute-on-read, nothing stored |
| DELETE | `/api/cash-flow/plans/{plan_id}` | shipped | Discard the baseline only; the draft run, payments and offers are untouched |

Both `/copilot` routes also take `?consolidated=true` (§16). Every route above
is on the same finance-leader gate (`admin`/`ap_manager`/`cfo`, never
`ap_clerk`) and the same `FEOH_CASHFLOW_COPILOT_ENABLED` kill switch; every
mutation is audited PII-free (`cash_plan.saved` / `cash_plan.deleted`).

### The `plan_id` scheme

`plan_id` is a **deterministic idempotency correlation key**, not a stored
primary key: `services/cash_flow_plan.py::compute_plan_id` hashes (UUID5, not a
random `uuid4`) the plan's own RESOLVED defining inputs — `org_id`,
`entity_id`, `granularity`, `horizon_days`, `min_balance_threshold`,
`cash_budget`, `cost_of_capital_pct` — plus the calendar **date** (not a
timestamp: "today" determines which commitments are in-horizon, so a plan
computed yesterday and an identical-params plan computed today are, correctly,
two different plans).

`propose_payment_plan` computes this once and returns it as `plan_id` on the
tool result (`PaymentPlanResult`, which also echoes the resolved
`granularity` / `horizon_days` / `min_balance_threshold` / `cash_budget` /
`cost_of_capital_pct` fields verbatim). The frontend replays those same fields
in the body of both enact calls (`CashFlowPlanReplay` — see
`app/schemas/cash_flow.py`); the enact endpoints independently resolve the
identical inputs and recompute `plan_id` server-side. If it doesn't match the
`plan_id` in the URL — a tampered field, or the underlying org settings
changed since the plan was proposed, or simply a new day — the request is
refused with a clean `409` rather than silently acting on a different plan
than the one the human is looking at. **The client is never trusted for WHAT
to act on** — only its replayed parameters decide which plan_id the server
expects, and the server re-derives its own commitment rows / discount-offer
selection from scratch either way. `/save` runs the identical guard, then
re-runs `propose_payment_plan` itself and stores ITS result — the client's
replay body never becomes the snapshot.

**The entity scope is discovered from the id, not asserted by the client.**
`plan_id` already hashes the `entity_id` a plan was built under, so exactly two
ids can be legitimate for a given caller: the entity they have selected, and
the consolidated whole-group scope. `_resolve_and_verify_plan` computes both
(most specific first) and accepts whichever matches. That is what lets a
consolidated plan be enacted or saved without the client telling us which mode
produced it — the plan card is rendered from a tool result that carries no
entity, and a self-declared `consolidated` flag in the body would be a claim we
would have to trust. It widens nothing: entity scoping is a *view* scope
(`tenant.get_entity_id` validates the header against the tenant's own
`entities` table and grants nothing), so the consolidated id is equally
reachable by simply not sending `X-Entity-ID` — and a tampered parameter still
matches neither candidate.

The plain `/api/assistant/chat` also gains the five read-only cash tools, so a
clerk-free finance user can ask cash questions in the general assistant too
(subject to the per-tool role gate). The façade exists mainly for the dedicated
UI and defaults.

---

## 7. Frontend

A `/cash-flow` route (Insights nav group), Svelte 5 runes, static — no SSR,
fetches via `#lib/api.ts`:

- **Copilot panel** — chat over `/api/cash-flow/copilot/stream`, reusing the
  assistant's streaming client + the chartable structured `result` (the forecast
  / position / optimizer results render as the existing chart components; the
  prose streams alongside).
- **Cash-position chart** — running-balance curve with shortfall periods flagged
  red (from `get_cash_position`).
- **Proposed-plan card** (`PlanCard.svelte`) — the period-by-period pay
  schedule, captured-savings figure, and two explicit buttons ("Create draft
  run", "Capture N discounts") that call `#lib/api/cashFlow.ts`
  (`createDraftRunFromPlan` / `captureDiscountsFromPlan`, both over
  `#lib/api.ts`, never raw `fetch`). The buttons are gated by role
  (`auth.isManager || auth.isCfo` — mirroring the backend's `_COPILOT_ROLES`,
  the same check `/discounts`' accept button uses) and rendered only when the
  plan has anything to act on. "Create draft run" fires on a single click
  (mirrors the manual payments-queue "Create draft run" button — a draft
  isn't destructive); "Capture N discounts" uses an armed two-click confirm
  (mutates several `DiscountOffer` rows). A `409` from either call — the
  stale-plan guard (§6) — surfaces a friendly "ask the copilot for a fresh
  plan" notice instead of a raw error (`ApiError`, `#lib/api.ts`, carries the
  HTTP status so the frontend can branch on it without parsing message text).
- **Save plan** — a third `PlanCard` action, over `saveCashFlowPlan`.
  Deliberately **no** confirm step: saving a snapshot moves no money, cannot be
  wrong, and a repeat save returns the original rather than overwriting it — so
  arming it would teach the wrong reflex about the two buttons beside it, which
  DO act.
- **Saved plans panel** (`SavedPlansPanel.svelte`, side rail) — the saved
  snapshots, each expanding into its plan-vs-actual comparison (§15). A period
  that has not closed yet is shown but visibly *not* scored, and a plan saved
  today reads "no period has closed yet, so there is nothing to score" rather
  than a fabricated zero variance. Payments the backend could not place or
  express are stated, not hidden. Armed two-click delete.
- **Consolidated toggle** (§16) — answers for the whole group without making
  the user clear the sidebar's entity selector. It rides the URL on BOTH the
  streaming and the non-streaming fallback path, so a stream failure cannot
  silently switch a group view to one subsidiary's.
- Money via the shared `<Money>` component (exact-string aware). Loading / empty
  / error states throughout. Build from the shared component library. No
  client-side money arithmetic anywhere.

E2E: `frontend/tests-e2e/cash-flow/copilot.spec.ts` (chat, charts, the plan card
and its enact buttons) + `saved-plans.spec.ts` (the rail, the save round-trip,
opening a comparison). Both assert structure, not amounts — money correctness is
proven to the cent in `backend/tests/test_cash_flow_saved_plans.py`, where it
can be.

---

## 8. Security & invariant mapping

| Invariant | How this design satisfies it |
|-----------|------------------------------|
| Money is exact | All figures from `Decimal` pure functions; tools serialize to string, never `float`. The analytics HTTP endpoints have since been migrated to the same exact-string contract, so there is no longer a float coercion to avoid copying |
| Idempotency on money-moving writes | Copilot moves no money; the only write is a draft run via the existing idempotent create path, deduped on `plan_id` |
| Audit trail append-only | Every tool call audited (existing orchestrator); every enact action audited via the existing payment-run / discount-accept audit rows |
| Tenant isolation at the data layer | Inherited from `get_tenant` chokepoint + entity scoping; tools bound to one tenant session |
| Auth before everything + RBAC | Façade + tools gated `admin/ap_manager/cfo`; per-tool `allowed_roles` enforced in `run_tool`; clerk gets a clean refusal |
| PII out of logs | Audit logs arg *shape* only; cash figures never enter the trail |
| Secrets via sops | No new secret — reuses `FEOH_ANTHROPIC_API_KEY`; local-first via mock/ollama |
| Local-first | Fully functional under `mock` (deterministic) and `ollama` (default); no cloud account required |

The critical new security surface is the **per-tool role gate** (clerks must not
read org cash) and the **draft-only boundary** on plan enactment. Both get
explicit tests (§10).

---

## 9. Configuration (`FEOH_` prefix)

Reuses the assistant's config; a couple of additive knobs:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_CASHFLOW_COPILOT_ENABLED` | `true` | Master switch for the copilot tools + façade routes |
| `FEOH_CASHFLOW_COPILOT_DEFAULT_HORIZON_DAYS` | `90` | Default forecast horizon when the user doesn't specify |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED` | `false` | Master switch for the projected-shortfall alert sweep (§14). OFF by default so local dev / tests never email a CFO |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_INTERVAL_SECONDS` | `86400` | Sweep tick. Daily — a cash forecast doesn't move hour to hour |
| `FEOH_CASHFLOW_SHORTFALL_ALERTS_HORIZON_DAYS` | `90` | How far ahead the alerting forecast looks. Separate from the copilot's interactive default so an operator can alert on a shorter, more actionable window |
| (reused) `FEOH_ASSISTANT_PROVIDER` / `FEOH_ASSISTANT_MONTHLY_TOKEN_BUDGET` / `FEOH_ANTHROPIC_API_KEY` | — | Adapter, budget, key — **no new secret** |
| (reused) `FEOH_DISCOUNT_COST_OF_CAPITAL_PCT` | `8.0` | Optimizer cost-of-capital when the user doesn't override |

---

## 10. Testing

- **Determinism / money-exactness** — under the `mock` adapter, a fixed seeded
  tenant produces a byte-stable forecast/plan; assert money is exact-string, no
  float anywhere in the tool return path.
- **Optimizer correctness** — reuse/extend the existing `optimize` unit tests;
  assert the copilot tool returns the same selection the `/api/discounts/optimize`
  endpoint does for the same inputs (single source of truth).
- **Draft-only boundary (critical)** — **Phase 2, shipped:** `propose_payment_plan`
  mutates nothing at all — assert no `Payment`/`PaymentRun` is created and no
  invoice status changes (`test_propose_payment_plan_never_mutates_anything`).
  **Phase 3, shipped:** the enact routes create a `draft` run and **execute
  nothing** — no `Payment` leaves `draft`, no invoice transitions to `paid`, and
  the CFO gate still guards execute. **Saving, shipped:** `/save` creates no
  `Payment`/`PaymentRun` and leaves every invoice status untouched
  (`test_saving_never_moves_money`).
- **A saved plan is a frozen baseline** — seeding a new commitment after a save
  and re-saving must return the ORIGINAL snapshot byte-for-byte
  (`test_save_does_not_restate_a_snapshot_when_the_data_moves`); a restatement
  would rewrite what the variance is measured against.
- **Only elapsed periods are scored** — the pure `compare_plan_to_actual` must
  exclude in-progress and future periods from every total, and surface real cash
  in a period the plan never projected rather than absorbing it.
- **Scope discovery is not a wildcard** — a plan id built under entity A must be
  accepted for A and for the consolidated scope, and refused (409) under entity
  B.
- **Per-tool RBAC** — an `ap_clerk` asking a cash question gets a clean refusal
  tool result (not data, not a 500); `test_rbac.py` coverage gate stays green for
  the new routes.
- **Tenant isolation** — tenant B's copilot never sees tenant A's commitments
  (inherited, but assert it).
- **Idempotency** — enacting the same `plan_id` twice yields one draft run.
- **Streaming parity** — the SSE `tool`/`delta`/`done` contract is unchanged;
  the plan artifact rides a `tool` frame before the prose.

---

## 11. Phasing

1. **Phase 1 — Read-only cash Q&A. ✅ SHIPPED.** The four planning tools + per-tool
   RBAC + the `/api/cash-flow/copilot(+/stream)` façade + the `/cash-flow` chat &
   cash-position chart. Ships value with zero write surface.
2. **Phase 2 — Proposed plans. ✅ SHIPPED.** `propose_payment_plan` tool + the
   plan card UI (display only, no enact). The optimizer + cash-position math
   drive a concrete, re-timed pay schedule; nothing moves money.
3. **Phase 3 — Draft-only enactment. ✅ SHIPPED.** The two enact endpoints (draft
   run + discount capture), idempotent + audited, human-confirmed, CFO gate
   unchanged — plus the originally-deferred sub-bucket: **saved plans +
   plan-vs-actual** (§15) and **consolidated cross-entity mode** (§16).

Each phase is independently shippable and independently valuable. Everything on
this page is now shipped.

---

## 12. Deferred / open questions

1. ~~**Saved plans + plan-vs-actual.**~~ **SHIPPED** — see §15. The tenant-scoped
   `CashPlan` model + migration `0087_cash_plans` persist a proposal as a frozen
   snapshot keyed by the deterministic `plan_id`, and `GET .../variance` scores
   it against what actually got paid. **Consolidated cross-entity mode**, the
   other half of this bucket, is §16.
2. ~~**Opening-balance source.**~~ **SHIPPED.** The resolution chain lives in
   `services/cashflow.py::resolve_opening_balance` — **one owner, and all three
   consumers go through it**: the copilot tools, the §14 alert sweep, and
   `GET /api/analytics/cash_position` (the `/cfo` dashboard's chart, which had
   its own duplicate inline copy). A copilot answer, an alert email, and the
   dashboard therefore cannot start from a different number. It returns an
   `OpeningBalance` carrying its own provenance: `source`
   (`explicit` | `provider` | `settings` | `none`), plus the `provider` name and
   the adapter's opaque `account_ref` when a bank sync supplied it — so the
   copilot can say "from your Modern Treasury operating account" rather than
   quoting an unattributable number. `CashPositionResult` and
   `PaymentPlanResult` surface all of it (`opening_balance_source` /
   `_provider` / `_account_ref`), and `CashPositionChart` / `PlanCard` already
   render the source label in every locale.

   The same change closed a real defect it exposed: the provider link used to
   take the adapter's amount and **drop its currency**. Every outflow subtracted
   from the opening balance is denominated in the org's reporting currency, so
   an org reporting in USD with a EUR operating account got a running balance
   that was silently a mixture of two currencies — and the plan proposals and
   shortfall figures priced off it were wrong by the exchange rate, with no
   signal. A provider balance in any other currency is now **refused**: the
   chain falls through to the persisted/zero link and flags
   `opening_balance_provider_skipped: "currency_mismatch"`, so the fallback
   can't be mistaken for "no bank is connected". Converting was rejected —
   an FX rate fetched on a read makes the curve non-deterministic and
   unreproducible (§3, `decisions.md` §18), and the fix an operator needs is to
   set a reporting currency or a BYO opening balance, not for us to guess. A
   blank / unknown provider currency fails closed the same way.

   **The outflow side had the mirror-image defect and is now fixed too.**
   `analytics._commitment_rows` selected `Invoice.amount` — each invoice's OWN
   currency — with no conversion, so the premise the paragraph above rests on
   ("every outflow subtracted from the opening balance is denominated in the
   org's reporting currency") was true of the balance and false of the
   outflows. A ¥10,000,000 invoice subtracted from a $250,000 opening balance
   projected a −$9.75M shortfall that did not exist: the alert sweep emails
   every finance leader about it, `propose_payment_plan` re-times payments
   around it, and `POST /plans/{id}/draft-run` stages a payment run off that
   plan. A GBP invoice under-counts in the same way and can hide a REAL
   shortfall. Every row now goes through `currency_conversion.reporting_amount_for_row`
   — the same helper every sibling rollup in `api/analytics.py` uses, reading
   the rate locked on the invoice, never fetching one on a read — and carries
   an `unconverted` flag so a row we could not convert is visible rather than
   silently mixed in.

   **That flag is now actually read.** For a while it was computed on every
   row and consumed by nobody, so a foreign invoice with no usable lock still
   entered the curve at face value with nothing on any surface to contradict
   it — the same −$9.75M, just arrived at one rung later.
   `analytics.bucket_outflows` counts it per period, `compute_cash_position`
   carries it through the running balance, and it surfaces as
   `unconverted_count` on `/api/analytics/{cashflow_forecast,cashflow_whatif,
   cash_position}` and on the `get_cashflow_forecast` / `get_cash_position` /
   `run_payment_whatif` tool results. It is the OUTFLOW-side twin of
   `opening_balance_provider_skipped` above: non-zero means the totals mix
   currencies, and — because the closing balance carries forward — one such row
   poisons every later period.

   `GET /api/analytics/cash_position` now returns the same provenance
   (`opening_balance_source` / `_currency` / `_provider` /
   `_provider_skipped`). Its explicit-override source value changed from
   `"query"` to `"explicit"` — the two names always meant the same thing, and
   the `cashFlow.chart.source.explicit` i18n key already existed in every
   locale while `…source.query` never did.

   **The `/cfo` cash-position card renders the refusal.** It used to show only
   the `source === 'none'` prompt, so "we have your bank balance and declined to
   use it" and "no bank is connected" looked identical on the surface where the
   number is actually read — the copilot's chat narration was the only place a
   human saw the difference. The card now renders a distinct amber notice
   whenever `opening_balance_provider_skipped` is set (independent of `source`,
   since the refusal can fall through to a persisted figure as easily as to
   zero), naming the reporting currency and what to do about it. The reason
   code → message mapping is the pure `routes/cfo/openingBalanceNotice.ts`; an
   **unrecognised** code deliberately falls back to a generic line rather than
   to silence, because the backend can add a reason before the frontend learns
   its wording and silence is the failure mode this closes. Display only — no
   backend change; the API already carried the field.
3. **Multi-entity consolidation.** Phase 1 honors `X-Entity-ID` (per-entity
   view). A "show me consolidated cash across all subsidiaries" mode would ignore
   the header like `GET /analytics/by-entity` does — a small follow-up. (The
   §14 alert sweep already runs org-wide for exactly this reason; the
   interactive tools still honour the header.)
4. ~~**Analytics endpoints' float coercion.**~~ **SHIPPED** — every money field
   on `/api/analytics/*` is now an exact decimal string, with the frontend and
   Flutter consumers moved across in the same change. Day counts, percentages
   and counts stay JSON numbers. See `backend/docs/analytics.md`
   § Money serialisation.
5. ~~**Proactive alerts.**~~ **SHIPPED** — see §14.

---

## 14. Proactive projected-shortfall alerts (shipped)

`app/services/cash_flow_alerts.py` — a background sweep that pushes the cash
forecast instead of waiting for someone to pull it. The cash-position curve and
its pure breach detector (`services/analytics.detect_threshold_breaches`) both
predate this; until now their only consumer was a dashboard read, so a finance
leader learned about a projected shortfall by going looking for it.

Mirrors `contract_renewal` / `discount_auto_trigger` exactly: a long-lived
asyncio task started in `main.lifespan`, one fresh engine per tenant, one org's
failure logged (exception *class* only — PII-out-of-logs) and skipped rather
than halting the sweep, **off by default** behind
`FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED`.

Per org, per tick:

1. **Opt-in check.** Skip unless the org has a persisted
   `settings.cashflow.min_balance_threshold` (managed by
   `GET/PUT /api/analytics/cash-position-settings`). The threshold *is* the
   opt-in — with no line there is nothing to breach and nothing to say.
2. Build the org-wide commitment rows via the same `_commitment_rows` the CFO
   dashboard and the copilot use, bucket them weekly over
   `FEOH_CASHFLOW_SHORTFALL_ALERTS_HORIZON_DAYS`, resolve the opening balance
   through the shared `resolve_opening_balance` (§12.2), and run
   `compute_cash_position` → `detect_threshold_breaches`.
3. `project_shortfall` (pure) reduces the breach list to the **earliest**
   breaching period — the deadline the finance leader is actually working
   against — carrying the count of all breaching periods so the message can say
   how widespread it is.
4. If that period differs from the one the org was last alerted about, notify
   its finance leaders once (`notification_dispatch.notify_event`, in-app +
   email, per-user-preference gated) and record the period on
   `Organization.settings.cashflow.shortfall_alert`.

| Property | How |
|---|---|
| **Never moves money** | No `Payment` / `PaymentRun`, no discount accepted, no invoice touched. The only write beyond the notification rows is the alerted-period marker in the org's settings JSON (no migration) |
| **Idempotent** | The marker is the dedupe — the role `renewal_alert_sent_at` plays in `contract_renewal`. A standing shortfall is announced once per projected period; the marker clears when the projection clears, so a recurrence is announced again |
| **Fails in the safe direction** | The notification is sent *before* the marker is written, so a crash between the two re-alerts next tick rather than swallowing the warning. For an alert, a duplicate is recoverable and a miss is not. That ordering only covers a *crash*, though: `notify_event` never raises, so an off `FEOH_NOTIFICATIONS_ENABLED`, a failed recipient load, or every leader having the event opted out all completed normally and the marker was written for an alert nobody received — permanently, since the marker is the dedupe. `notify_event` now returns **how many recipients it actioned**, and the marker is written only when that is non-zero (a PII-free WARNING names the org otherwise) |
| **One answer on audience** | `ALERT_ROLES` is pinned by a drift-guard test to `api/cash_flow.py::COPILOT_ROLES` (`admin` / `ap_manager` / `cfo` — not `ap_clerk`), so the push surface and the pull surface can't disagree about who may see org cash. An org nobody was actually told about — no finance leaders yet, or a dispatch that reached none of them — leaves the marker unwritten so a later sweep still fires |
| **Org-wide by design** | `entity_id=None` — a treasury shortfall is a question about the whole legal group's cash, the same consolidated posture `GET /analytics/by-entity` takes by ignoring `X-Entity-ID` |
| **PII-free** | The message carries org-level aggregates only (projected closing balance, threshold, shortfall), formatted straight off the `Decimal`; log lines carry counts and exception classes, never a cash figure |

New notification event: `cash_shortfall_projected` (`entity_type:
"cash_position"`, `entity_id: NULL` — the alert is about the org's whole
projected position, not any one record). Rendered by
`notification_templates.render_cash_shortfall`, pre-rendered and handed to
`notify_event(rendered=…)` like `contract_renewal_due`.

Tests: `backend/tests/test_cash_flow_alerts.py` (pure reduction, mocked
multi-org fan-out + failure isolation + dedupe + re-arm + recipient-less retry,
and a real-Postgres pass proving the breach comes from real invoices and that
nothing on the money path moves).

---

## 13. Files this will touch (when built)

Backend: `app/services/assistant/tools/{cashflow,optimizer}.py` (new tools) +
`tools/__init__.py` (register), `app/api/cash_flow.py` (façade + enact routes),
`app/services/cash_flow_plan.py` (pure plan assembler over the existing
analytics/optimizer functions), `app/services/cashflow.py` (opening-balance
resolution + provenance, §12.2), `app/services/cash_flow_alerts.py` (the §14
shortfall sweep, wired into `app/main.py`'s lifespan),
`app/schemas/cash_flow.py`, wired into `app/main.py`; tests
`backend/tests/test_cash_flow_copilot.py`,
`backend/tests/test_cash_flow_opening_balance.py`,
`backend/tests/test_cash_flow_alerts.py`; docs — flip this banner + add a
router row to the root `CLAUDE.md` + a roadmap entry.
Frontend: `src/routes/cash-flow/+page.svelte`, `src/lib/components/cash-flow/`,
`src/lib/api/cashFlow.ts`, nav entry in `src/lib/nav.ts`, e2e
`tests-e2e/cash-flow/copilot.spec.ts`.

## Currencies the curve could not convert

`api/analytics._commitment_rows` includes a foreign invoice with no usable
rate lock at **face value** — dropping it would understate the outflow — and
flags it `unconverted`. `services/analytics.bucket_outflows` counts those into
`unconverted_count`, and the count now rides every surface built on that curve:

| Surface | Field |
|---|---|
| `get_cashflow_forecast` / `get_cash_position` / `run_payment_whatif` | `unconverted_count` |
| `propose_payment_plan` (`PaymentPlanResult`, and per `PaymentPlanPeriod`) | `unconverted_count` |
| the projected-shortfall alert email | a sentence naming the count |

The plan and the alert were the two that still dropped it, and they are the two
that matter most: the plan is the only artifact a user can **enact**
(draft-run / capture-discounts), and the alert is an email telling finance
leaders their cash runs out. A single unconverted ¥10,000,000 invoice drags a
$250,000 opening balance to a projected −$9.75M, so the shortfall the email
announces can be entirely manufactured by the conversion gap. Counts only —
never a vendor, an invoice number or an amount breakdown, so the message stays
PII-free.

Nothing converts at read time. A rate fetched on a read makes a historical
curve move under the reader (`docs/decisions.md` §18).

## A malformed threshold does not silently un-subscribe an org

`settings.cashflow.min_balance_threshold` **is** the per-org opt-in to shortfall
alerting: `cash_flow_alerts._project_tenant` returns early when it resolves to
`None`. So `cashflow._coerce_decimal` returning `None` for a corrupt stored
value un-subscribed the org from the alert it configured, forever, with nothing
logged. It now logs a warning naming the FIELD (never the value — a settings
blob is operator data) on every rejection.

`Decimal("NaN")` was the same failure wearing a valid-parse costume:
`Decimal(str("nan"))` succeeds, and every `closing < threshold` comparison
against it is False, so the org looked configured while being just as opted
out. Non-finite values (NaN and both infinities) are refused for that reason —
only a finite number is a threshold.

## Draft-run stages one currency, and says what it left

A `PaymentRun` is single-currency by construction: `PaymentRun.total_amount` is
a bare `Numeric` with no currency of its own and the CFO threshold is compared
against it as a bare number (`payment_runs.create_payment_run_for_invoices`
refuses a mixed batch with a 422). `POST /plans/{plan_id}/draft-run` used to
hand that builder **every** payable invoice in the plan's horizon, so for a
multi-currency tenant it could only ever 422 — the plan card's button could
never succeed, and the error was not actionable from a plan the user cannot
edit.

It now narrows to the org's **reporting currency** — the currency the plan's own
cash curve, budget and threshold are already expressed in, so the staged run is
the slice of the plan the plan was reasoning about — and reports
`run_currency` + `excluded_currency_count` rather than dropping the rest
silently. Those invoices remain payable from the normal queue. When the horizon
holds nothing in the reporting currency the 409 names the currencies that ARE
there: the actionable version of the old 422. `excluded_currency_count` is 0
for every single-currency tenant, so nothing changes for them.

## Capture-discounts row-locks each offer

`POST /plans/{plan_id}/capture-discounts` re-reads each selected offer
`FOR UPDATE` before mutating it, mirroring the payment dispatcher's claim
pattern. `run_discount_optimization` loads the rows with a plain SELECT, so two
concurrent calls both saw `offered` and both accepted. It is status-only — no
money moves — so the worst case was a duplicate audit row and a second
`accepted_at`; a money-adjacent mutator should still not be the one place in
the file that takes the state it read on trust.

**Tests:** `backend/tests/test_cashflow_currency_visibility.py`.

---

## 15. Saved plans + plan-vs-actual (shipped)

**What it answers:** *did our cash forecast hold?* Every other surface in this
document is forward-looking; this is the only one that looks back.

### Why a stored snapshot at all

`compute_plan_id` deliberately makes a plan re-derivable, so the obvious
question is why anything needs storing. The answer is that a plan is only
re-derivable **today**: its id hashes the calendar date because "today"
determines which commitments are in-horizon, and the invoices inside it have
since been approved, paid, voided or re-dated. Yesterday's projection is
therefore not recomputable — and a variance with no baseline is just a
restatement of the present.

So the snapshot is **frozen at save time and never restated**. A second `POST
.../save` for the same `plan_id` returns the existing row untouched (`created:
false`, HTTP 200) rather than re-running the proposal against newer data;
restating would silently rewrite the very number the comparison is measured
against. The unique index `uq_cash_plans_org_plan_id` makes that hold under a
concurrent retry — the loser rolls back and returns the winner's row.

The saved curve is JSONB with **money as exact decimal strings**: `jsonb` would
store a bare number as `numeric`, but every JSON codec in the path round-trips
one through `float`, which is the money invariant this stack exists to hold.
`services/cash_flow_plan.py::freeze_periods` / `thaw_periods` own that shape.

### What is compared

`GET /plans/{plan_id}/variance` is compute-on-read — nothing is stored, so
re-running it later simply scores more elapsed periods.

- **Only fully-elapsed periods are scored.** A period whose window has not
  closed has no variance to report: its actual is a partial number, and
  subtracting a whole projection from a partial actual manufactures a "we
  underspent" reading that reverses by the end of the week. In-progress and
  future periods are still returned (a reader wants the shape of what is coming)
  but are labelled `in_progress` / `future` and excluded from every total.
- **Actual = `completed` payments, dated by `completed_at`.** A payment still in
  flight is not cash that left. A `completed` payment with no `completed_at`
  cannot be placed in any period, so it is counted on `undated_payment_count`
  rather than dropped silently or forced into a bucket it may not belong to
  (its `created_at` bounds that count to the plan's own window — it is never
  used AS a settlement date, which is precisely why the row cannot be scored).
- **Amounts resolve through `currency_conversion.payment_reporting_amount_sql`**,
  the same resolver `/api/payments/summary` and the 1099 report use.
  `Payment.amount` is denominated in the INVOICE's currency, so summing it raw
  across a multi-currency book is a two-currency mixture; a row neither rung can
  express is excluded and counted on `unconvertible_payment_count`, never added
  at face value.
- **Both sides are bucketed by the same function.** Actual payments go through
  `analytics.bucket_outflows` at the plan's own granularity, so the labels join
  by construction rather than by a second, drifting date rule. Cash landing in a
  period the plan never projected is reported on `unmatched_actual_periods`
  instead of being absorbed into a total.
- **The comparison runs under the SAVED plan's entity scope**, not the caller's
  `X-Entity-ID` — scoring one scope's projection against another's actuals is
  not a variance, it is two unrelated numbers subtracted.
- **Discount follow-through** rides along: how many of the plan's own selected
  offers are now `accepted`/`captured`.

`period_bounds_for_label` does **not** reimplement the bucketing rule — every
label `bucket_outflows` emits is itself a date inside its own period, so it
parses the label and feeds it back through `analytics._period_bounds`. A label
that does not belong to the stated granularity raises rather than guessing a
window.

**Files:** `app/models/cash_plan.py`, `alembic/versions/0087_cash_plans.py`,
the bottom half of `app/services/cash_flow_plan.py`, the saved-plan routes in
`app/api/cash_flow.py`, `app/schemas/cash_flow.py`,
`frontend/src/lib/components/cash-flow/SavedPlansPanel.svelte`.
**Tests:** `backend/tests/test_cash_flow_saved_plans.py`,
`frontend/tests-e2e/cash-flow/saved-plans.spec.ts`.

---

## 16. Consolidated cross-entity mode (shipped)

A treasury shortfall is a question about the whole legal group's cash, not one
subsidiary's slice. The §14 alert sweep already took that posture (it builds its
commitment rows with `entity_id=None`) and `GET /api/analytics/by-entity` takes
it by ignoring `X-Entity-ID` outright. The interactive surface now matches.

- **On the chat routes** it is an explicit flag: `POST /api/cash-flow/copilot`
  and `/copilot/stream` accept `?consolidated=true`, which runs the turn with
  `entity_id=None` regardless of the header. A plan proposed in that turn
  therefore carries the consolidated `plan_id`.
- **On the plan routes** it is *discovered*, not asserted — see §6. The client
  is never asked which mode produced a plan, because it would have to be
  trusted about it.
- **Nothing widens.** Entity scoping in this codebase is a view scope, not a
  privilege boundary: `tenant.get_entity_id` validates the header against the
  tenant's own `entities` table and grants nothing, and the consolidated answer
  is equally reachable by not sending the header at all. The tenant boundary —
  the thing that IS a privilege boundary — is untouched.
- **A consolidated draft run** stages across every entity and the run row lands
  on the tenant's **default** entity, the documented home for un-scoped rows,
  exactly as an entity-less `POST /api/payments/runs` already behaves. The scope
  comes from the plan, not from `X-Entity-ID`, so the staged set is always the
  set the plan reasoned about.
- **Saved plans**: `entity_id IS NULL` marks a consolidated snapshot. `GET
  /plans` is entity-scoped by default (a consolidated plan is nobody's single
  entity's plan); `?consolidated=true` lists the whole tenant. Detail, variance
  and delete look a plan up by id alone within the tenant, so a consolidated
  snapshot stays readable from the view that created it.

**Frontend:** a rail toggle on `/cash-flow` that rides the URL on both the
streaming and the non-streaming fallback path — a stream failure must not
silently switch a group view to one subsidiary's.
