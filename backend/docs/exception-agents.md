# Exception Agents — autonomous exception handling

AI agents that triage the exception queue: for each flagged invoice an agent
either auto-resolves it (mutating the invoice through the same audited path a
human would use) or escalates it to a human. Tenant-scoped, append-only
decision log, local-first (no LLM key required), opt-in per org.

The queue spine both agents and humans run on — how an exception is opened,
routed and closed, and the append-only `exception.*` audit rows every one of
those events writes — is [exception-lifecycle.md](exception-lifecycle.md).

**Status:** two fully-implemented resolvers for `po_mismatch` (small amount
mismatch + missing-PO auto-link) behind one dispatcher, plus a GL-coding resolver
for `missing_data` behind its own dispatcher; the remaining exception types are
escalate-only stubs (see [Deferred](#deferred)). An agent dashboard UI ships the
resolution / escalation rates + decision log on the `/exceptions` **AI Agents**
tab (accuracy is a labelled placeholder pending a human-overturn signal).

## Data model — `AgentDecision`

`app/models/agent_decision.py`, table `agent_decisions` (tenant DB, migration
`0030_agent_decisions`). One **append-only** row per coordinator run.

| Column | Type | Notes |
|--------|------|-------|
| `id` | uuid PK | |
| `exception_id` | uuid FK → `exceptions.id` | indexed |
| `invoice_id` | uuid FK → `invoices.id` | indexed |
| `exception_type` | varchar(50) | copied from the exception |
| `action_taken` | varchar(20) | `auto_resolved` \| `escalated` \| `no_action` |
| `confidence` | numeric(5,4) | exact `Decimal` (never float), 0.0000–1.0000 |
| `rationale` | text | human-readable; LLM-polished when a key is set |
| `changes` | jsonb | `{"field": {"old": "<str>", "new": "<str>"}}`; money is string-Decimal. NULL when nothing changed |
| `autonomy_level` | varchar(20) | the level in force at decision time |
| `agent_type` | varchar(50) | which resolver decided (`amount_mismatch_v1`, a stub, or `none`) |
| `organization_id` | uuid | indexed |
| `entity_id` | uuid FK → `entities.id` | from `EntityMixin`; copied from the invoice |
| `created_at` / `updated_at` | timestamptz | from `TimestampMixin` |

Append-only is enforced by convention (no UPDATE/DELETE path in code). It is the
**decision log**, NOT the audit trail — the DB-immutable `audit_log` row for the
invoice mutation is written separately (see [Two audit rows](#two-audit-rows)).

## Architecture

Mirrors the project's adapter-registry layout (`erp_adapters/` et al.).

```
app/services/exception_agents/
├── base.py            # ExceptionResolver ABC + AgentEvaluation + action constants
├── registry.py        # @register_exception_agent decorator + get_resolver()
├── autonomy.py        # autonomy_level → confidence threshold
├── llm_rationale.py   # optional LLM rationale polish (fail-soft, offline default)
├── coordinator.py     # run_agent(): evaluate → (apply | escalate) → record AgentDecision
├── __init__.py        # re-exports + imports resolvers for decorator side effects
└── resolvers/
    ├── po_mismatch.py      # dispatcher registered for `po_mismatch` — tries each delegate
    ├── amount_mismatch.py  # delegate: po_mismatch amount variance (snap + approve)
    ├── missing_po.py       # delegate: missing/unresolved PO (link by vendor+amount+date)
    ├── multi_po_split.py   # delegate: consolidated invoice spanning a unique PO set (sum match)
    ├── missing_data.py     # dispatcher registered for `missing_data` — tries each delegate
    ├── gl_coding.py        # delegate: GL-coding fix/correct from vendor history (+ approve)
    └── stubs.py            # escalate-only stubs (duplicate, fraud_flag)
```

- A **resolver** handles exactly one `exception_type`. `evaluate(...)` returns an
  `AgentEvaluation` (recommended action + confidence + proposed `changes`) and
  performs **no mutation**. `apply(...)` enacts the resolution and MUST write the
  invoice-mutation audit row(s); the default is a no-op (for escalate-only
  stubs).
- The **coordinator** (`run_agent`) is generic: resolve the org's autonomy
  threshold, dispatch to the resolver, decide whether confidence clears the
  threshold, call `apply` (or escalate), and always persist one `AgentDecision`.

### One exception type, several strategies — the `po_mismatch` dispatcher

The registry is keyed by `exception_type` (one resolver per type), but a single
`po_mismatch` exception covers several distinct invoice↔PO problems, each with
its own fix. `resolvers/po_mismatch.py::PoMismatchDispatcher` is the **single**
resolver registered for `po_mismatch`; it owns an ordered list of delegate
resolvers and, in `evaluate`, tries each until one recommends `auto_resolved`.
That delegate's recommendation **and its `agent_type`** become the dispatcher's
result, so the coordinator records the real resolver (`amount_mismatch_v1` /
`missing_po_v1` / `multi_po_split_v1`) in the `AgentDecision`; `apply` is
forwarded to the selected delegate. The delegates are **disjoint**:

- `matched` live status → `amount_mismatch_v1`;
- `no_po` + exactly **one** PO matching the full amount → `missing_po_v1`;
- `no_po` + **no** single PO matching but a **unique** PO *set* summing to the
  total within tolerance → `multi_po_split_v1`.

`multi_po_split_v1` explicitly defers (recommends nothing) when a single PO
matches the full amount, and the dispatcher tries the single-PO resolver first,
so single-PO always wins that case. At most one delegate ever fires; ordering
only decides the rationale carried on a full escalation. The dispatcher never
mutates state itself.

### Coordinator flow

1. Resolve `autonomy_level` → confidence `threshold`.
2. `get_resolver(exception_type)`; if none → record `no_action` (status
   untouched), commit, return.
3. `resolver.evaluate(...)` → `AgentEvaluation`.
4. `can_resolve = recommended == auto_resolved AND confidence >= threshold`.
   - **Before any mutation**, if `can_resolve`: refuse when the triggering human
     is barred from *clearing* this exception by segregation of duties
     (`exception_lifecycle.segregation_refusal`) → escalate with a recorded
     decision. See § The agent inherits the queue's segregation rule.
   - If yes: `resolver.apply(...)` (writes audit rows), then resolve the
     exception through the **shared** queue chokepoint
     `services/exception_lifecycle.record_decision` — the same one
     `api/exceptions` uses — which stamps `time_to_resolution_seconds` +
     `resolved_by = "AP Agent"` and writes the append-only
     `exception.resolved` row (`via: "agent"`, `actor_id` = the human who
     triggered the run).
   - If no: escalate through that same chokepoint (`exception.escalated`).
     The evaluation rationale lands on the exception row, so the human picking
     it up reads why in the queue, not only in the `AgentDecision` log.
     Escalation deliberately does NOT stamp `resolved_by` / `resolved_at` — see
     [exception-lifecycle.md](exception-lifecycle.md) § Escalation is not a
     resolution.
   - If `apply` raises `NotApprovable` (invoice not in `ready_for_review`):
     downgrade to `escalated`, nothing was committed.
   - If `apply` raises an `HTTPException` below 500 — the **approval path's own
     refusal** — downgrade to `escalated` the same way, with the refusal's
     detail as the rationale a human reads in the queue. A 5xx is a fault, not
     a refusal, and propagates. See § Every refusal ends in a decision row.
5. Persist one `AgentDecision`, commit.

### Every refusal ends in a decision row

`resolver.apply` finishes in `review.approve_invoice`, which enforces controls
no resolver pre-check can reproduce:

- **Segregation of duties** — the user who uploaded the invoice cannot approve
  it, and `actor_id` is the human who triggered the agent run. An AP manager
  resolving an exception on their own upload trips this.
- **The named-approver gate** (`approver_strategy: "specific"`).
- **The max-amount / CFO thresholds measured against the same-vendor rolling
  AGGREGATE** (`services/structuring`) — a resolver only ever sees the single
  invoice, so a split payable can clear its own pre-check and still be refused.

Each is an `HTTPException`, and the coordinator only caught `NotApprovable` —
so those propagated out of `run_agent` to the route. The caller got a bare 403,
the exception stayed `open`, no `AgentDecision` row was written, and nothing in
the queue said why. Every *other* way an apply can fail records a decision and
escalates; these now do too.

The apply also runs inside a **SAVEPOINT** (`db.begin_nested()`). The
`NotApprovable` path could assume "nothing was committed" because resolvers
raise it *before* calling `approve_invoice`; the approval path's own refusals
cannot, because `approve_invoice` applies `corrections` — including `amount` —
*before* it enforces the money thresholds (deliberately, so the gate sees the
post-correction figure). Escalating after a threshold refusal without unwinding
would commit the agent's amount change on an invoice nobody approved.

A savepoint rollback **expires** every object the apply touched, so the handler
re-loads the invoice (`await db.refresh(invoice)`) before anything reads it. A
sync attribute access on an expired instance triggers a lazy refresh, which
under asyncio raises `MissingGreenlet` — turning a handled refusal back into a
500. The realdb half of `tests/test_exception_agent_approval_refusal.py` is what
pins both halves: the refused amount change does not persist, and the escalation
that follows does.

### The agent inherits the queue's segregation rule

Clearing a payment-**blocking** exception (`duplicate` / `fraud_flag` /
`line_total_mismatch` / `payment_reconciliation`) is what lets a payment run pay
the invoice, and it is refused for an actor implicated in that payable or
recorded as having raised the flag — see
[`docs/authentication.md`](../../docs/authentication.md) § Segregation of duties
on the exception queue.

That rule binds on the agent door too, and `via="agent"` is **not** an
exemption. The agent holds no authority of its own: `actor_id` is the human who
pressed the button, the fail-closed branch above refuses to act without that
human's real roles, and `apply` approves on those roles — which is why the
`HTTPException` handler above already exists for the *approval* path's own
segregation refusal. Exempting agents would hand a barred actor a one-click
route to the outcome the HTTP door refuses them.

It is checked **before** `apply` rather than caught after it, because the answer
needs no mutation and because `record_decision` runs *after* the SAVEPOINT
closes — a raise from there would reach the route as a bare 403 with the
exception left `open` and no `AgentDecision` row, the exact regression the
handler above was written to fix. The outcome is therefore the same escalation
every other refusal produces, with the refusal sentence as the rationale a human
reads in the queue. The org opt-out
(`settings.exceptions.require_segregation: false`) is read from the same place,
so an org that disabled the control does not find the agent still refusing.

**Nothing in the shipped registry can reach the gate today**: `duplicate` and
`fraud_flag` are escalate-only stubs (§ Deferred) and `line_total_mismatch` /
`payment_reconciliation` have no resolver at all, so no payment-blocking type
has an auto-resolving agent. That is why
`tests/test_exception_agent_queue_segregation.py` registers a probe resolver to
test it — a gate written *after* the first such resolver lands, lands as a
bypass — and why a companion test fails the moment a real auto-resolving
resolver appears for a blocking type.

## Autonomy → threshold

`Organization.settings.exception_agents.autonomy_level` controls **whether to
act**:

| Level | Confidence threshold | Effect |
|-------|----------------------|--------|
| `conservative` (default) | `1.01` | unreachable — **everything escalates** (safe, opt-in-required default) |
| `balanced` | `0.90` | auto-resolve when the resolver is ≥ 90% confident |
| `aggressive` | `0.75` | auto-resolve when ≥ 75% confident |

Unknown / absent levels fall back to `conservative` (fail-closed). `conservative`
being `1.01` makes "off" a data property, not a special-case branch — the
coordinator's single `confidence >= threshold` comparison naturally escalates
everything.

## The amount-mismatch resolver (`amount_mismatch_v1`)

Handles `exception_type == "po_mismatch"` where the **only** issue is an amount
variance within a tight tolerance: adjust the invoice amount to the PO total and
approve.

- **Data source:** the **live** `PurchaseOrder` row, re-matched via
  `match_invoice_to_po` inside `evaluate` (and again under the invoice row lock
  in `apply`). The resolver does **not** trust the `invoice.po_match` JSONB
  snapshot — that snapshot can be stale (PO re-synced/edited after it was
  written) and it doesn't distinguish a clean amount variance from a `partial`
  3-way receipt. Re-matching closes both gaps in one read.
- **In-scope only when the live match status is exactly `matched`.** `no_po`,
  `mismatch` (variance beyond the matcher's own band), and `partial` (3-way
  underdelivery — goods only partially received) all **escalate** — paying a
  partially-received PO in full would be wrong.
- **Currency:** a `PurchaseOrder` has no currency of its own in the schema; its
  `total` is denominated in the invoice's currency, so there is no
  cross-currency comparison to make. If per-PO currency is ever added, the
  resolver must gate on `invoice.currency == po.currency` before adjusting.
- **Tolerance:** default **2.5%** (deliberately tighter than the matcher's 5%
  warning band so the agent never silently absorbs a variance a human reviewer
  would still consider material). Override via
  `Organization.settings.exception_agents.amount_tolerance_pct`.
- **Decimal math (load-bearing):** `po_total` and the current amount are
  `Decimal(str(...))` then `.quantize(Decimal("0.01"))` — never `float`.
  `variance_pct = abs(current - po_total) / po_total * 100`, quantized to `0.01`.
- **Auto-fix only when** the live status is `matched`, `variance_pct <= tolerance`,
  `current != po_total`, and the reconciled amount does not trip a CFO/maximum
  approval gate. Confidence is `0.95`. `apply` re-runs the match under the row
  lock and bails (→ escalate) if the live PO total moved before it adjusts.
- The new amount is exactly `po_total`, passed as a `Decimal` into
  `review.approve_invoice(corrections={"amount": ...})` — the same audited
  correction path a human approver uses.

### CFO gate (not bypassed)

`approve_invoice` enforces the workflow snapshot's `require_cfo_above` /
`max_invoice_amount`. The agent acts on behalf of the **triggering user** and
approves with **that user's real roles** (`coordinator.run_agent(actor_roles=…)`,
threaded straight into `approve_invoice` — never a fabricated `{"ap_manager"}`
set), so rather than bypassing the gate it reads the snapshot in `evaluate` and
**escalates** when the reconciled amount would require CFO sign-off (or exceeds
the hard max). The money-path invariant stays intact — the agent never grants
itself a role the actor doesn't hold.

Both threshold reads in `evaluate` go through `approval_chain`'s shared
fail-CLOSED body (`_money_gate_applies`), so a **malformed / non-finite**
`require_cfo_above` or `max_invoice_amount` in the snapshot (a settings typo, or
a stringified `Infinity`/`NaN` an insider tampered in to defeat the control)
**fails closed** — it escalates to a human rather than silently skipping the gate
(`amount > Decimal("Infinity")` is always False, which previously let a tampered
threshold auto-approve). The same fail-closed body backs the human-approval and
expense-report CFO gates.

All four resolvers express the gated figure in the org's **reporting currency**
first, via `approval_chain.reporting_gate_amount` — the thresholds are bare
numbers denominated there (see `workflow-design.md` § Approval Thresholds), so a
foreign-currency invoice must not be gated on its billed figure. An invoice with
no locked FX rate comes back `expressible=False`, which the shared gate body
reads as fail-closed: the resolver escalates instead of self-approving past a
control it could not evaluate.

Each cap is read through **its own** named wrapper —
`approval_chain.max_amount_gate_applies` for `max_invoice_amount`,
`approval_chain.cfo_gate_applies` for `require_cfo_above`. The two share that
body, so the verdict is identical either way; what differs is the diagnosis. All
four resolvers evaluated the hard cap through `cfo_gate_applies`, whose
fail-closed log reads *"auto-approval money threshold is unparseable (…);
requiring human (CFO) approval"* — so an operator chasing a stuck agent was
pointed at `require_cfo_above` when the bad value was `max_invoice_amount`, on
the one path where that log is the only evidence of what happened. The cap trip
now logs *"max-invoice-amount money threshold is unparseable (…); refusing the
approval"*. `tests/test_exception_agent_max_amount_gate.py` is the drift guard
(an AST scan over the four resolvers, plus a check that the two helpers emit
non-interchangeable diagnoses).

**Fail-closed authority.** An auto-resolution approves an invoice with the
actor's authority, so `run_agent` refuses to self-approve when it can't name the
acting user's roles: if `actor_roles` is empty/`None` on a would-be auto-resolve,
the coordinator **escalates to a human** instead of fabricating an elevated set.
The only caller today (`POST /api/exceptions/{id}/agent-resolve`) always passes
the JWT user's real roles (`{r.name for r in user.roles}`); a hypothetical
background trigger with no user therefore escalates rather than auto-approving.

### The org's rules, not the platform's

`ExceptionResolver.apply` takes `org_settings: dict | None = None` — the SAME
dict `evaluate` receives, passed by `coordinator.run_agent` (which has held it
all along) and forwarded by every resolver into `review.approve_invoice` and, in
`missing_po`, `invoice_warnings.refresh_warnings`.

It used to be missing from `apply`'s signature entirely, so the four resolvers
that approve ran the invoice through the **platform** defaults:

* a fraud rule the tenant had explicitly **disabled** still opened a
  payment-BLOCKING `fraud_flag` (`duplicate` / `fraud_flag` /
  `line_total_mismatch` are in `api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`),
  so the agent's own "resolution" refused the invoice's next payment run; and
* a stricter-than-default per-vendor / per-commodity PO tolerance
  (`settings.matching.vendor_rules`) was **erased** from `invoice.po_match` by
  the resolver's own `refresh_warnings` recompute — the control silently
  loosened by the thing meant to honour it.

Every HTTP approval door already threaded it (the single-invoice endpoint, the
email link, the Slack button, the Teams card, bulk-status). These are worse than
those were, because they run **unattended** — nobody is watching the screen when
it happens.

`tests/test_approval_org_settings.py` scans the whole of `app/` (it was scoped
to `app/api/`, which is why it never saw the resolvers) and drives
`run_agent` with a stub resolver to assert the coordinator hands `apply` the
same settings it hands `evaluate`.

Note that `gl_coding` and `multi_po_split` still **reuse the thresholds/tolerance
`evaluate` resolved** rather than re-resolving them in `apply`. That is
deliberate and unchanged: pinning the value is what guarantees `apply` enacts
exactly what `evaluate` decided, so the two halves of a resolver can never
disagree about an override.

### Two audit rows

A successful auto-resolve writes **two** rows:

1. **`invoice.approved` in `audit_log`** — `approve_invoice` builds a
   `build_field_diff` (`{"amount": {"old": "...", "new": "..."}}`, string-Decimal)
   and `transition_invoice` writes the row with `details={"changes": ...}`. This
   is the DB-immutable mutation-audit row.
2. **`AgentDecision`** — the decision log (this feature's own product). It is
   **not** a substitute for the audit row.

## The missing-PO resolver (`missing_po_v1`)

Handles `exception_type == "po_mismatch"` where the **live** match status is
`no_po` — the invoice references a `po_number` that resolves to nothing (a
typo'd / mis-extracted number, or a number whose PO sits under a different
vendor). The real PO usually *does* exist; the resolver finds it and links it
rather than escalating a blank.

- **Disjoint from amount-mismatch.** `evaluate` re-runs `match_invoice_to_po`
  and only proceeds when the live status is exactly `no_po`; a `matched` status
  belongs to `amount_mismatch_v1`, anything else to a human. The two
  `po_mismatch` delegates therefore never both fire.
- **Candidate search — vendor ∧ amount ∧ date** (`_candidate_pos`):
  - **Vendor:** the invoice's `vendor_id` (exact). If the invoice has no
    `vendor_id` but does carry a `vendor_name`, `vendor_matching.match_vendor`
    resolves the name → vendor and only a **≥ 0.8** confident name match is used;
    no vendor signal at all → no candidates (it never amount-matches the whole
    tenant blind).
  - **Amount:** the PO `total` within the org's effective PO-match tolerance band
    (`matching_rules.resolve_match_rule` — the same per-vendor / per-commodity
    resolver the matcher itself uses; default **5%**). Money is `Decimal`.
  - **Date:** the PO's `created_at` (a `PurchaseOrder` has no order-date column)
    must fall in an asymmetric window around the invoice's `invoice_date` —
    `[invoice_date − lookback, invoice_date + 5d]` (POs precede invoices;
    `lookback` default 90 days, override
    `Organization.settings.exception_agents.po_match_window_days`). When the
    invoice has **no** `invoice_date`, the date leg is skipped.
- **Confidence:** exactly one candidate clearing all legs →
  - `0.92` when corroborated by date (auto-resolves at `balanced`/`aggressive`);
  - `0.80` for an undated, vendor+amount-only match — deliberately **below** the
    `balanced` 0.90 gate, so it auto-resolves only under `aggressive` autonomy.
  - Zero or **multiple** candidates → `0.0` → escalate (ambiguous → a human picks).
- **`apply` — link + approve via the audited path.** There is no `Invoice.po_id`
  FK; the link is `invoice.po_number` (+ aligning `invoice.vendor_id` to the PO's
  when absent), mirroring how `po_matching` resolves a PO. `apply` re-locks the
  invoice, re-asserts `ready_for_review`, re-verifies the live match is still
  `no_po`, re-fetches the **exact** PO chosen in `evaluate` (bailing if it
  vanished / is no longer `open`), re-points `po_number`, calls
  `invoice_warnings.refresh_warnings` to refresh `po_match`, and requires the
  post-link match to be a clean `matched` before approving through
  `review.approve_invoice` (with the triggering user's real `actor_roles`). It **never adjusts the
  amount** — it only links. The CFO/maximum gate is honoured (escalate, never
  self-approve past a threshold) and — because the link does **not** change the
  invoice amount — the gate is measured against the **invoice's own amount**, not
  the linked PO total (unlike `amount_mismatch_v1`, which snaps the amount to the
  PO total and so legitimately gates on it; this matches `multi_po_split_v1`). The
  gate is also checked **before** any `po_number`/`po_match` mutation, so an
  escalation never leaves a half-applied PO link in the committed state.
  Idempotent: a re-run after the link finds the live match no longer `no_po` and
  bails, and the coordinator's exception row-lock already prevents a second
  decision on a resolved exception. Writes the two audit rows (`invoice.approved`
  + the `AgentDecision`, the latter recording `changes={"po_number": {...}}`).

## The multi-PO split resolver (`multi_po_split_v1`)

Handles `exception_type == "po_mismatch"` where the live match status is `no_po`
**and** no single PO under the vendor matches the invoice total on its own — but
a **set** of the vendor's open POs sums (within the resolved tolerance) to the
invoice total. The classic case is one consolidated invoice raised against two
or three POs. This is the deferred *"Multi-PO split matching"* follow-up to
`missing_po_v1`.

- **Disjoint from `missing_po_v1`.** Both fire on `no_po`, but they never overlap:
  `missing_po_v1` owns the case where exactly **one** PO matches the full amount;
  `multi_po_split_v1` fires only when **no** single PO matches but a **unique** PO
  *set* of size ≥ 2 sums to the total. The resolver explicitly **defers**
  (recommends nothing) when any single candidate PO matches the full amount, and
  the dispatcher tries the single-PO resolver first, so a 1:1 link always wins.
- **Set search — pure + bounded** (`find_po_subset`, no DB / clock / randomness):
  - **Candidate pool:** the vendor's open POs (same vendor leg as `missing_po_v1`
    — exact `vendor_id`, else a ≥ 0.8 `vendor_name` match, else nothing) inside
    the same asymmetric date window (`[invoice_date − lookback, invoice_date + 5d]`,
    `lookback` default 90 days, override `po_match_window_days`).
  - **Tolerance:** the **combined** PO total must fall within the org's effective
    PO-match tolerance band of the invoice total — resolved via
    `matching_rules.resolve_match_rule` (same per-vendor / per-commodity resolver
    the matcher uses; default 5%). Money is `Decimal`.
  - **Combinatorial bound:** the pool is capped at **`_MAX_CANDIDATES = 12`** POs
    and the subset size at **`_MAX_SET_SIZE = 4`** (worst case
    C(12,2)+C(12,3)+C(12,4) = 781 tiny combinations). A pool **larger than 12 is
    NOT truncated** — truncation could hide the real set or invent a false
    "unique" one — so the resolver **escalates with a logged rationale**
    (`SubsetSearchTooLarge`) instead of searching a partial pool. Size-1 subsets
    are excluded by construction (that's `missing_po_v1`'s job).
- **Ambiguity / none → escalate.** `find_po_subset` returns the single matching
  set only when **exactly one** distinct subset sums within tolerance; **more than
  one** (ambiguous) or **zero** → `None` → escalate. It never picks arbitrarily.
- **Confidence.** A split is weaker evidence than a single exact PO, so it sits
  one band below `missing_po_v1`: **`0.90`** dated (auto at `balanced`/`aggressive`),
  **`0.80`** undated (auto only under `aggressive`).
- **`apply` — link the set + approve via the audited path.** Re-locks the invoice,
  re-asserts `ready_for_review` and a still-`no_po` live single-PO match, re-fetches
  the **exact** PO set chosen in `evaluate` (bailing if any vanished / is no longer
  `open`), re-derives the unique sum under the lock (bail on drift), honours the
  CFO / maximum gate exactly as the single-PO resolvers (the gate is on the
  **invoice** amount, which never changes — the combined PO total is informational),
  sets `invoice.po_number` to a combined `"PO-A,PO-B"` reference + aligns
  `vendor_id`, and writes a **multi-PO match snapshot** onto `invoice.po_match`
  (`match_type: "multi-po-split"`, the PO ids/numbers + combined total) for the
  modal. The single-PO matcher can't produce a `matched` for a split, so the
  snapshot is written **directly** — `refresh_warnings` is deliberately **not**
  called (it would re-run the single-PO matcher and re-raise a `no_po` exception).
  It then approves through `review.approve_invoice` (with the triggering user's real `actor_roles`).
  **It NEVER adjusts the invoice amount** — the sum only *selects* the set; the
  amount is left exactly as-is, so the invoice is approved at its own face value.
  Idempotent: a re-run finds the live match no longer `no_po` (or not
  `ready_for_review`) and bails, and the coordinator's exception row-lock prevents
  a second decision. Writes the two audit rows (`invoice.approved` + the
  `AgentDecision`, the latter recording `changes={"po_number": {...}}` with the
  combined reference).

## The GL-coding resolver (`gl_coding_v1`)

Handles `exception_type == "missing_data"` where the actionable gap is a
**missing or inconsistent GL account**: the invoice has no `gl_account` (or one
that disagrees with how this vendor has been coded every other time), and the
vendor's approved history shows a single dominant GL a reviewer would almost
certainly pick. The agent fills / corrects that one field and approves.

Registered behind a **`missing_data` dispatcher** (`resolvers/missing_data.py`),
mirroring the `po_mismatch` dispatcher: the registry is keyed by exception type,
the dispatcher is the single registered `missing_data` resolver, and it delegates
to `gl_coding_v1` (today the sole strategy). The dispatcher surfaces the
delegate's `agent_type` so the `AgentDecision` records the real resolver.

- **Reuses the enrichment stats, doesn't reimplement them.** The dominant-value
  math is the pure `vendor_enrichment.suggest_fields` primitive — the same
  dominance-ratio computation the `/api/enrichment/.../suggestions` advisory
  surface uses. The resolver only adds the agent wiring: pull the vendor's
  approved-or-beyond coding history (newest first, bounded by `HISTORY_LIMIT`),
  ask `suggest_fields` for the dominant `gl_account` (and `cost_center`), map the
  dominance to a confidence, and apply.
- **Corrects, not only fills.** `suggest_fields` is non-destructive (it suppresses
  any field the draft already populates), so the resolver calls it with an empty
  `current` — the dominant value is derived purely from history. It then compares
  to the invoice's *actual* current GL: a present-but-inconsistent GL is corrected
  to the vendor's dominant value, not left alone. An invoice already coded to the
  dominant GL escalates (the missing-data gap is elsewhere; no false "resolution").
- **Confidence bands.** A very dominant value (≥ 80% dominance over ≥ 5 approved
  invoices) → `0.92` (auto at `balanced`/`aggressive`); a merely-majority value →
  `0.80` (auto only under `aggressive`). Below the enrichment suggestion floor
  (`autofill_min_confidence` / `autofill_min_sample`, read from the org's
  `settings.enrichment`) no suggestion is produced → escalate. Ambiguous history
  (no majority) → escalate.
- **Cost center rides along** only when the draft's cost center is empty *and* the
  vendor has its own dominant cost center — never overwrites a populated one, and
  never codes a cost center on its own (the trigger is always the GL fix).
- **Other-required-field gate.** A GL fix only makes an invoice payable when the
  vendor / invoice-number / amount are already present (mirrors the
  `invoice_warnings` missing-field set). A genuinely missing one of those
  escalates — a GL correction alone wouldn't help.
- **`apply` — correct + approve via the audited path.** Re-locks the invoice,
  re-asserts `ready_for_review`, re-derives the dominant GL under the lock (bails
  if it no longer holds, or the invoice was already coded → idempotent), honours
  the CFO / maximum gate exactly as the PO resolvers (escalate, never self-approve
  past a threshold), then calls `review.approve_invoice(corrections={"gl_account":
  …, "cost_center": …})` — the same audited correction path a human approver uses.
  **It never touches `amount`** — GL recode only; it never moves money. Writes the
  two audit rows (`invoice.approved` with the field diff + the `AgentDecision`,
  the latter recording `changes={"gl_account": {...}, "cost_center": {...}}`).
  The re-derivation under the lock reuses the SAME `autofill_min_confidence` /
  `autofill_min_sample` thresholds `evaluate` resolved from the org's real
  `settings.enrichment` (stashed on the resolver instance, mirroring
  `multi_po_split`'s `_tolerance_pct` stash) — `apply` never falls back to the
  platform defaults, so an org's looser/stricter override can't make `evaluate`
  and `apply` disagree and force a spurious escalation.

## No-LLM-key fail-soft

`llm_rationale.build_rationale` mirrors `audit_summary.py`: the deterministic
`template` rationale is always the fallback, an LLM is consulted **only** when a
key is configured (reusing `FEOH_ANTHROPIC_API_KEY` + `FEOH_EXTRACTION_MODEL`, or the
org's BYOK extraction key), and there is **zero network call** in the no-key
local default. The *decision* (action + confidence + amount change) is 100%
rules-derived — the LLM only rewords the rationale string.

## API

All under `/api/exceptions`, all behind `require_roles(admin, ap_manager)`.
Registered **before** `exceptions.router` so the literal collection routes win
over `exceptions.py`'s `/{exception_id}/...` matcher.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/exceptions/{exception_id}/agent-resolve` | Run the agent on one `open`/`escalated` exception. Returns the new exception status + the `AgentDecision`. |
| `GET` | `/api/exceptions/agent-decisions` | Paginated decision log; filter by `exception_type`, `action_taken`. |
| `GET` | `/api/exceptions/agent-stats` | Aggregate counts + resolution/escalation rate. `accuracy` is a placeholder (needs a human-overturn signal). |

Response shapes are typed Pydantic `response_model`s in
`app/schemas/exception_agent.py` (`AgentDecisionResponse`,
`AgentDecisionListResponse`, `AgentStatsResponse`, `AgentResolveResponse`).

`confidence` is serialised as `float` in responses (display only) — stored exact.

### Dashboard UI

The `/exceptions` route carries an **AI Agents** tab (a `ui/Tabs` panel beside the
operational Queue) rendering `lib/components/exceptions/AgentDashboard.svelte` over
`GET /agent-stats` + `GET /agent-decisions` (via `lib/api/exceptionAgents.ts`):

- a KPI row — decisions made, resolution rate, escalation rate, auto-resolved,
  escalated;
- a recent-decision log table (resolver / exception type / action / confidence /
  autonomy / change summary) with an action filter (`all` / `auto_resolved` /
  `escalated` / `no_action`) and Load-More pagination;
- an **accuracy** card that shows "Not yet measured" with an explainer — never a
  fabricated number — until a human-overturn signal exists (see below).

Both endpoints are admin/ap_manager-gated server-side. e2e:
`frontend/tests-e2e/exceptions/agent-dashboard.spec.ts`.

#### Running an agent from the dashboard

`POST /{exception_id}/agent-resolve` had **no caller in `frontend/src`** — the
dashboard reported on agent activity that could only be triggered outside the
product. It now ships as a **Run an agent** panel above the decision log
(`getAgentCandidates` / `runExceptionAgent` in `lib/api/exceptionAgents.ts`):

- The runnable queue is `GET /api/exceptions?status=open,escalated` — the exact
  statuses `agent-resolve` accepts, so the panel never offers a row the endpoint
  would 409. It keeps its own loading / errored / empty states: a failed read
  must never render as "nothing to run an agent on", which is a claim about the
  open exception queue.
- An **invoice-less** exception (a Positive Pay never-issued-cheque `fraud_flag`)
  is listed with a *disabled* Run carrying the reason, not hidden — the backend
  422s it, and a missing button explains nothing.
- Running is **confirm-then-act**: a run can mutate the invoice, so it is never
  a bare click.
- **The outcome is rendered from the response, and `escalated` / `no_action` are
  OUTCOMES, not failures.** The coordinator applies a fix only when confidence
  clears the org's autonomy threshold and hands the rest to a human — that is
  what the threshold is for, and rendering it as an error would teach operators
  that the safe path is the broken one. Each renders its own explanatory note
  beside the action badge, the new exception status, the resolver, the
  confidence, the autonomy level and the rationale. The persistent error region
  is reserved for a genuine HTTP refusal (404 / 409 / 422), whose `detail` stays
  on screen rather than fading in a toast.
- After a run the stats, the decision log and the runnable queue all re-read
  together.

e2e: `frontend/tests-e2e/exceptions/agent-resolve.spec.ts`.

**Concurrency:** `coordinator.run_agent` takes a `FOR UPDATE` lock on the
exception row and re-asserts its status before doing anything. The API's
pre-call status check is a TOCTOU on its own; two concurrent `agent-resolve`
calls would both pass it. The row lock serializes them — the loser re-reads the
now-`resolved` row and the coordinator raises `ExceptionNotActionable`, which
the API maps to **409** (no second `AgentDecision` row, no status clobber).

## Org settings

```json
{
  "exception_agents": {
    "autonomy_level": "conservative",
    "amount_tolerance_pct": 2.5,
    "po_match_window_days": 90
  }
}
```

Default (key absent) → `conservative` → everything escalates. No new env var:
the optional rationale LLM reuses `FEOH_ANTHROPIC_API_KEY` + `FEOH_EXTRACTION_MODEL`.

One setting outside this block also governs an agent run:
`settings.exceptions.require_segregation` (default `true`, disabled only by an
explicit `false`) is read by both the human queue and the coordinator, so the
segregation refusal above is turned off in one place for both doors rather than
two.

## Deferred

Registered as escalate-only stubs (so the coordinator dispatches by type) or
intentionally unregistered (so the coordinator records `no_action`). Each has a
roadmap follow-up.

| Exception type | State | Follow-up |
|----------------|-------|-----------|
| `missing_data` | **`gl_coding_v1`** (GL coding) behind the `missing_data` dispatcher; non-GL missing-field gaps still escalate | other missing-field auto-fill (vendor / amount / terms). The missing-**PO** case is handled by `missing_po_v1` under `po_mismatch`/`no_po`, not here |
| `duplicate` | stub — always escalates | duplicate auto-merge |
| `fraud_flag` | stub — always escalates | (likely stays human-gated) |
| `unverified_vendor`, `review_rejected`, `amount_exceeded`, `extraction_failed` | unregistered → `no_action` | human gates / out of scope for this slice |

Also deferred: a mobile agent surface, adaptive/learned thresholds, and an
**accuracy metric** — it needs a human-overturn signal (was an auto-resolution
later reversed?), which is not tracked yet, so `agent-stats.accuracy` is `null`
and the dashboard shows "Not yet measured" rather than a fabricated figure.
GL-coding follow-ups: per-line GL coding (multi-line invoices coded per item),
and learned per-vendor dominance thresholds instead of the static bands.

Missing-PO follow-ups: **multi-PO split matching** (one invoice spanning several
POs) is now **shipped** as `multi_po_split_v1` (see [The multi-PO split
resolver](#the-multi-po-split-resolver-multi_po_split_v1)). Still deferred:
**line-level** split matching (which PO each invoice *line* belongs to, vs. the
header-amount set match shipped here); matching against `created`/non-`open` POs;
and a learned date window / amount band per vendor instead of the static defaults.
