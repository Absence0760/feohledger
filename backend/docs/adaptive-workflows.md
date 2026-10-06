# Adaptive AI Workflows

> **Mostly advisory.** Most of this feature is **read models** over approval
> history plus an **advisory** suggestion store. There are **two** explicit,
> opt-in act surfaces, each of which routes through an existing audited service
> rather than mutating state directly:
>
>   1. The smart-routing **apply** path (`POST /routing-suggestion/apply`)
>      assigns the top-ranked recommended approver **through the existing audited
>      `review.assign_reviewer` service** (audit row + notification + OOO
>      delegation — never a raw `assigned_to_id` write).
>   2. The auto-approve-threshold **apply** path
>      (`POST /threshold-recommendation/apply`) raises the org-wide
>      `auto_approve_below` dollar threshold **through the existing audited
>      workflow-definition PATCH path** — it reuses
>      `workflow_definitions._snapshot_version` + the `workflow.version_snapshot`
>      audit dispatch, so a `WorkflowVersion` snapshot + audit row land exactly as
>      a manual `PATCH /api/workflows/{id}` edit would. Never a raw `steps_config`
>      mutation.
>
> Everything else stays advisory: the GET surfaces only **recommend**; the
> remaining "act" surfaces (A/B testing, retraining) are tracked follow-ups, not
> built here.

> **Local-first / deterministic.** All learning and anomaly detection is plain
> statistics (`Decimal`) over the tenant's own data. There is **no LLM call** and
> nothing requires a cloud key — the whole feature runs on a laptop with `pnpm
> dev`. If a future slice adds LLM phrasing for a suggestion, it MUST fail soft to
> the template with no key, mirroring `services/audit_summary.py`.

## Overview

Five surfaces, all under `/api/adaptive`:

1. **Approval-pattern learning** (`GET /approval-patterns`) — per-approver and
   per-vendor aggregates over the tenant's approval/rejection history.
2. **Anomaly detection** (`GET /anomalies`) — flag an invoice (or batch of
   in-review invoices) against the learned per-vendor baseline; returns the
   baseline it compared against.
3. **Workflow-change suggestions** (`GET /suggestions`,
   `POST /suggestions/{id}/dismiss`) — derived "consider auto-approve under $X"
   suggestions, persisted with `open / dismissed / applied / stale` status.
4. **Smart routing** (`GET /routing-suggestion`,
   `POST /routing-suggestion/apply`) — for one invoice, **rank** the org's
   eligible approvers by routing fit (fastest + most-consistent + most
   vendor-familiar), purely from their approval history. GET is advisory; the
   apply path assigns the top pick through the audited `review.assign_reviewer`.
5. **Auto-approve threshold** (`GET /threshold-recommendation`,
   `POST /threshold-recommendation/apply`) — recommend a **conservative raise**
   to the org-wide `auto_approve_below` dollar threshold from the same
   clean-history vendor evidence the suggestions use, and (admin-only) apply it
   through the audited workflow-definition PATCH path.

The statistics live in `app/services/adaptive_workflows.py` (pure, sync, no IO —
unit-testable without a DB). The SQL, the control-plane name join, response
shaping, and suggestion persistence live in `app/api/adaptive_workflows.py`.

## Relationship to the existing fraud / anomaly code

This feature does **not** duplicate `services/invoice_warnings.py`. They are
complementary:

| | `invoice_warnings.fraud_stat_anomaly` | `/api/adaptive/anomalies` |
|---|---|---|
| Trigger | Automatic, on every extraction / mutation (`refresh_warnings`) | On-demand read (GET) |
| Scope | Single vendor, amount only (`amount > mean + N·σ`) | Per-vendor baseline, **three** axes (amount / approver / timing) |
| Side effects | **Writes** warnings + Exception rows | **None** — read-only; returns the baseline for explainability |
| Audience | The invoice processing pipeline | A manager/CFO investigation surface |

If you're reading one and wondering whether the other is dead code: it isn't.
The adaptive endpoint is the on-demand, explainable view; the warning is the
inline pipeline flag. The adaptive code never calls `refresh_warnings` and never
creates Exceptions.

Similarly, `services/analytics.py` has `compute_processing_time_metrics` /
`compute_approval_bottleneck` (a per-stage operational cut). The adaptive
learning service is a per-approver / per-vendor **behaviour** cut and reuses
`analytics._avg` / `_quantile` / `_decimal_days` by importing them.

## Data sources

All reads are tenant-DB (via `get_tenant_db`). Optional entity scoping: when
`X-Entity-ID` resolves to a non-None UUID (`get_entity_id`), invoices are
filtered to that entity; `None` (or absent / `all`) is the consolidated view.

- **Approval / rejection events** come from `audit_log` joined to `invoices`
  (`audit_log.entity_id = invoices.id`):
  - `action = "invoice.approved"` → an approval. `details->'changes'` present ⇒
    approved-**with-corrections**; absent ⇒ approved **unmodified**.
  - `action = "invoice.rejected"` → a rejection.
  - `actor_id` is the approver; `created_at` the decision time.
- **Time-to-approve clock start** = the invoice's transition **into**
  `ready_for_review` — the earliest `audit_log` row for that invoice whose
  `details->>'new_status' = 'ready_for_review'`. Fallback when none exists
  (e.g. auto-approved straight from `new`): `invoices.created_at`. The
  clock-start lookup is bounded to the same `since` window as the decision
  rows, so an out-of-window `ready_for_review` transition can't be paired with
  an in-window approval. Elapsed days via `analytics._decimal_days` (simple
  elapsed days, not business-day weighted — a deliberate follow-up), **clamped
  at ≥ 0** so an out-of-order / backfilled audit row can't feed a negative
  day-count into the median or baseline.
- **Vendor baseline** (for anomaly detection) is built from that vendor's
  **historically-approved** invoices only, with every amount converted into the
  reporting currency (see below) — `status IN (approved, sending_to_erp,
  sent_to_erp, posted_in_erp, payment_scheduled, paid, done)`. Pending / rejected
  invoices are not part of the "accepted norm". The approver + timing per
  baseline invoice come from its `invoice.approved` audit row.

Approver display names are joined from the **control-plane** `User.full_name` in
a separate query (tenant and control are different databases — no cross-DB join).

### Every amount is expressed in the org's REPORTING currency

Neither `_decision_rows` (the threshold evidence) nor `_vendor_approved_rows`
(the anomaly baseline) hands the pure-stat layer raw `Invoice.amount`. Both
convert each invoice into the org's reporting currency
(`currency_conversion.resolve_reporting_currency`) at the rate already **locked
on that invoice row** by `invoice_warnings._refresh_reporting_amount` — via
`currency_conversion.reporting_amount_at_locked_rate`, the same helper the
payment CFO-approval gate uses. No FX call is made on the read, so a market
move can never retroactively rewrite what a control decided.

This is a money-correctness requirement, not a display nicety. The org-wide
`auto_approve_below` these statistics recommend is a **bare number with no
currency of its own**; it is denominated in the reporting currency, matching
`payments.cfo_approval_above` (see `services/payment_controls.cfo_approval_decision`)
and `settings.expense_approval.cfo_threshold`. Reading raw billed amounts let
three spotless JPY 100,000 vendors — worth about 650 of a USD tenant's own
currency — argue for a five-figure USD auto-approve limit.

A row whose currency cannot be bridged (no lock, or a lock that provably
describes a different currency pair) is emitted with `amount_unconverted=True`.
It is then **excluded**, never converted at a guessed rate and never added at
face value:

- `compute_vendor_patterns` keeps its amount out of `avg/median/min/max` and
  counts it on `VendorApprovalPattern.unconverted_count`;
- `recommend_auto_approve_threshold` and `derive_suggestions` disqualify any
  vendor with `unconverted_count > 0` — a max computed off a partial history
  would understate the vendor's real ceiling while the copy claims a complete
  record.

The **anomaly baseline** is the second consumer, and it is the same defect the
`stat_anomaly` fraud rule already had fixed: `compute_vendor_baseline` takes a
mean and a population stdev over the amounts it is given, and a mean over mixed
currencies describes nothing — a handful of JPY invoices either make an ordinary
USD one read as a wild outlier, or inflate the stdev enough to hide a genuine
one. `VendorBaseline` now carries the `currency` its money fields are in and an
`unconverted_count`; an unpriceable row contributes no amount at all (not a
guessed conversion, not its face value) but still contributes its approver and
its timing, because neither depends on a currency — dropping it would make a
legitimate approver look unusual. `min_history` is measured in rows the baseline
could actually price.

Converting only the baseline would have moved the mismatch rather than fixed
it, because `detect_invoice_anomaly` read the subject's amount straight off the
Invoice object. Its **input contract changed** instead: `amount` is a required
keyword-only argument — the subject expressed in the baseline's currency — and
`None` means it could not be. There is no default to forget. On `None` the two
amount rules abstain and an `amount_comparison_unavailable` info flag says why;
this surface is advisory and writes no warning and no Exception row, so no
verdict beats a fabricated one, and the approver / timing rules still run.

The enforcement side moves with it — and not just for `auto_approve_below`.
Every approval money threshold (`require_cfo_above`, `max_invoice_amount` and
the approval chain's per-level bands too) is denominated in the same currency
and compared through `approval_chain.GateAmount` /
`approval_chain.reporting_gate_amount`; see `workflow-design.md` § Approval
Thresholds for that side and its fail-closed table. `decide_auto_approve`'s
amount floor **does not fire at all** on an invoice that can't be expressed —
fail closed, the invoice goes to a human — because a threshold gate that
silently passes an unconvertible invoice is an auto-approval nobody authorised.
Both call sites (`run_extraction` and `POST /api/invoices/{id}/complete`) run
`refresh_warnings` immediately beforehand, which is what locks the rate, so the
conversion is a read of the row.

#### The wire says what the reader is looking at

Excluding a row makes the aggregate honest; it does not make the *presentation*
honest, so both read models carry the fact out to the client rather than leaving
it to be inferred:

- `VendorPatternResponse.unconverted_count` — the money fields exclude those
  approvals while `sample_size` still counts them, so an average over
  `approved_count - unconverted_count` rows is otherwise presented beside a
  sample count that disagrees with it. `/adaptive` renders a `role="alert"`
  line above the vendor table when any row is non-zero, the same disclosure
  treatment `/cfo` gives its unconverted outflows (`docs/decisions.md` §79,
  reinforced by §82). **The denominator is never quietly moved to make the two
  agree** — the exclusion is reported.
- `InvoiceAnomalyResponse.amount_currency` — `amount` is only in the reporting
  currency when the subject could be expressed there; otherwise
  `detect_invoice_anomaly` falls back to the **billed** figure for display, and
  a client with no currency on the row has no choice but to stamp the reporting
  currency onto a number that is not in it. `_anomaly_amount_currency` resolves
  it (reporting currency when convertible, `Invoice.currency` when not) and
  `_anomaly_dict` takes it as a required keyword argument — no default to
  forget, the same reason `amount` is required on `detect_invoice_anomaly`.

`AnomalyFlag.severity` is `info` or `warning` — four of the five flags are
`info`, including `amount_comparison_unavailable`, which reports the *absence*
of a verdict. A renderer that folds `info` into a warning tint makes the page
claim something the backend did not.

**Single-currency tenants are unaffected** — same currency converts at rate 1
with no lock required. For a tenant whose reporting currency is *not* USD, the
figures the threshold surfaces show are now that currency, and they say so: the
recommendation's `rationale` and the per-vendor suggestion copy name the
currency code instead of prefixing `$`, `ThresholdRecommendation.currency`
carries it, `DerivedSuggestion.payload.threshold_currency` records it, and the
`POST /api/invoices/{id}/complete` auto-approve message reads
"below the 5,000.00 EUR threshold".

## Statistics

All amount math is `Decimal`; amounts quantize to `0.01`, rates/days to `0.1`.

- `approval_rate_pct = approved / (approved + rejected) * 100` (0 when no
  decisions).
- `consistency_pct = unmodified / approved * 100` (0 when no approvals) —
  share of approvals made with no field corrections.
- `avg / median / min / max approved_amount` over the approved-amount list.
- `median / avg time_to_approve_days` over the non-None per-invoice legs.
- `_stdev` — population standard deviation, `Decimal` (`Decimal.sqrt`); `<2`
  samples → 0. (analytics doesn't expose one, so it's defined here.)

### Anomaly rules

Given a per-vendor `VendorBaseline` (built only when `>= min_history` approved
invoices; otherwise `insufficient_history=True`, no flags):

1. **`amount_high`** (warning) — `amount > mean + sigma·stdev` **AND**
   `amount > median · median_multiple`. The AND-guard means a tight-variance
   vendor (small σ) doesn't trip on a modest absolute jump, while a wide-variance
   vendor still flags genuine outliers.
2. **`amount_low`** (info) — `amount < mean − sigma·stdev`, `amount > 0`, `σ > 0`
   (unusually tiny — possible test transaction / split invoice).
3. **`unusual_approver`** (info) — only when an approver is supplied (the GET
   endpoint passes the invoice's `assigned_to_id` if set) and that approver is
   not among the vendor's historical `typical_approver_ids`.
4. **`off_pattern_timing`** (info) — an in-flight invoice's time-in-review
   exceeds `median_time_to_approve_days · timing_multiple`.

The result always carries the `baseline` it compared against (explainability).

### Suggestion derivation

`auto_approve_threshold` is the only kind this slice. A vendor qualifies when:
`approved_count >= suggestion_min_history` **AND** `rejected_count == 0` **AND**
**zero modifications** (`unmodified_count == approved_count`). The
zero-modifications gate is **absolute** — it deliberately mirrors the absolute
`rejected_count == 0` gate rather than the older percentage floor, because the
suggestion's own copy asserts a spotless `{n}/{n} approved unmodified` record:
a single corrected approval would make that claim false, so a single corrected
approval disqualifies the vendor. `suggestion_min_consistency_pct` is retained
as an additional (weaker) floor for org-config symmetry, but the absolute gate
is what governs. The suggested threshold is the vendor's max approved amount
rounded **up** to the nearest $500. `confidence_pct = min(consistency_pct, 99)`
(never claims 100%). Messages are deterministic template strings — no LLM.

### Smart routing

`recommend_approvers(eligible, approver_patterns, ...)` (pure, in
`services/adaptive_workflows.py`) ranks the org's **eligible approvers** for one
invoice. Eligible = active control-plane `User`s in the org holding an
approval-capable role (`admin` / `ap_manager` / `cfo`; `ap_clerk` enters
invoices but doesn't approve, so it's excluded). Each candidate gets a
deterministic 0–100 score = weighted sum of four sub-scores (each normalised to
0..1):

| Sub-score | Weight | Formula |
|---|---|---|
| **speed** | 45 | `1 − median_time_to_approve_days / horizon` (clamped 0..1); a 0-day median for a real approver = 1 (fastest); no approval history = 0 |
| **consistency** | 25 | `approval_rate_pct / 100` (fewer rejections / rework) |
| **vendor familiarity** | 20 | `min(vendor_approved_count, 5) / 5` — how many of *this vendor's* invoices the approver has approved before |
| **experience** | 10 | `min(sample_size, 20) / 20` — total decisions made (more signal) |

The weighted sum of those four is the candidate's **`base_score`** (0..100).
A fifth term — the **outcome down-weight** — is then *subtracted* (see below) to
give the final `score`. `horizon` defaults to 14 days. Vendor familiarity is
computed in the API layer by counting this approver's approvals of the invoice's
vendor over the same decision-row window the patterns are built from. The
per-approver speed / consistency / experience come from
`compute_approver_patterns` (all-vendor history).

Ties break on more vendor familiarity → larger sample → `approver_id` (stable +
deterministic). An eligible approver with **no** decision history still appears
(so a new-but-valid approver is routable) but scores only on familiarity.
`insufficient_history` is True only when **no** eligible approver has any history
to rank on — the caller should then fall back to its normal assignment policy.
Each candidate carries a `reasons` list (deterministic human-readable strings)
explaining its score. **The GET result never assigns anyone** — it's a ranked
read model. The separate **apply** path below is what actually assigns.

#### Smart routing — outcome down-weighting (the feedback loop, in the router)

The four sub-scores above are **forward-looking** — they rank on the *approval*
history alone. The router also folds in the realised human **outcomes** of each
approver's past decisions, so it stops recommending an approver whose approvals
don't hold up. This is the routing-side mirror of the *threshold* feedback loop
(below): the same overturn signal, bucketed **per approver** instead of over the
auto-approved population.

- **Per-approver overturn rate** — `compute_approver_outcomes` (pure, in
  `services/adaptive_workflows.py`) reads, for each approver, the share of their
  `invoice.approved` decisions in the window that a human later **voided**
  (`invoice.voided_return_to_approved`), **rejected** (`invoice.rejected`), or
  **corrected** (a later `invoice.approved` carrying `details.changes` **by a
  different actor** — an approver correcting their *own* call on the way in is not
  an overturn of themselves). The same `is_overturned(voided, corrected,
  rejected)` primitive backs both this and the threshold-side
  `compute_outcome_stats`, so the two surfaces agree on what "overturned" means
  and the audit parsing isn't re-derived twice. The SQL that builds these rows is
  `_approver_outcome_rows` (a single LEFT-joined aggregate, entity-scoped via the
  invoice join), the per-invoice mirror of `_auto_approval_outcome_rows`.
- **The penalty** — `recommend_approvers(..., outcomes=…)` subtracts
  `min(overturn_rate, 25 %) / 25 % × 30` points from `base_score` (floored at 0).
  So an approver overturned **at or above 25 %** loses the full **30-point**
  penalty; one below it loses it linearly; a clean approver loses nothing. It is
  **banded + bounded** (≤ 30 points, never hard-zeros a candidate): a high
  overturn rate de-prioritises an approver without making them unroutable.
- **Thin evidence never penalises.** Below a **min-sample of 5 decided invoices**
  the API passes no `ApproverOutcome` for that approver (`insufficient_data`), so
  **no penalty** applies — one bad call can't sink a new approver. (The rate is
  still surfaced for explainability; it just doesn't bite.)
- **Explainable.** Each `RoutingCandidate` returns `base_score`,
  `outcome_penalty`, and the per-approver `overturn_rate_pct` /
  `overturned_count` / `outcome_sample_size`, plus a `reasons` entry when the
  penalty bit (`down-weighted N pts — k/m decisions later overturned (r %)`) —
  mirroring how `/anomalies` returns the baseline and `/feedback` returns its
  basis. The `score` = `base_score − outcome_penalty`, so a held-back approver is
  fully traceable on the wire.

Because the `score` is what the candidates sort on, a heavily-overturned approver
can drop below a clean peer even when the overturned one is *more* vendor-familiar
(the −30 penalty outweighs the +20 familiarity edge) — the router stops
recommending decisions that don't hold up. The **apply** path is unchanged: it
still assigns whichever candidate now ranks first.

#### Smart routing — apply (`POST /routing-suggestion/apply`)

The explicit, opt-in act path: take the recommendation and **assign the
top-ranked eligible approver** to the invoice. Body `{ "invoice_id": <uuid> }`
(`?days=` reuses the GET lookback). It re-runs the *identical* ranking
(`_rank_for_invoice`, shared with the GET) and routes the chosen approver
**through `services/review.assign_reviewer`** — the same audited service the
manual `POST /api/invoices/{id}/assign` endpoint calls. That guarantees, with no
new audit code here:

- an immutable **`invoice.assigned_for_review` audit row** is written (the
  append-only-audit invariant), recording the routed reviewer id;
- the **assignee notification** fires (`EVENT_INVOICE_ASSIGNED`), best-effort;
- **OOO delegation** is honoured (`approval_chain.resolve_assignee`) — if the
  routed approver is out, the assignment redirects to their delegate and the
  audit row records `delegated_from`. The response's `assigned_to_id` /
  `assigned_to_name` reflect the *effective* (post-delegation) assignee.

Behaviour / guards:

- **RBAC** `admin` / `ap_manager` — a **write** surface, so it matches who can
  already assign reviewers (`POST /api/invoices/{id}/assign`), **not** the
  manager/CFO *read* roles. **CFO can read the recommendation but cannot apply
  it** (403), exactly like the manual assign endpoint excludes CFO.
- **Status precondition** — the invoice must be `ready_for_review`; otherwise
  **409** (same precondition + message as the manual assign). The invoice is
  fetched `WITH FOR UPDATE` (entity-scoped) to row-lock against a concurrent
  assign/transition.
- **No defensible pick → 422** — when `insufficient_history` (no eligible
  approver has any history) or there are no candidates, the path refuses with
  **422** rather than assigning arbitrarily; the caller falls back to manual
  assignment.
- **Idempotent** — if the invoice is **already** assigned to the chosen top
  approver, it is a no-op: `assigned: false`, **no second audit row**, no
  re-notify. (Re-assigning to a *different* current assignee does route through
  `assign_reviewer` again, which re-resolves OOO — the manual endpoint allows
  free re-assignment too.)
- **404** when the invoice isn't in this tenant/entity.

Response (`ApplyRoutingResponse`): `invoice_id`, `assigned` (bool — False on the
no-op), `assigned_to_id`, `assigned_to_name`, `rank` (always 1 — the top pick),
`score` (string-Decimal). The commit follows the manual-assign convention — the
`get_tenant_db` dependency commits on a clean return.

### Auto-approve threshold — recommend + apply

The "act" surface for adaptive *thresholds* (the routing apply above acts on
*assignment*). It answers the roadmap ask "raise the auto-approve limit as
accuracy improves" — **safely, auditably, and only when an admin explicitly
applies it**.

`recommend_auto_approve_threshold(vendor_patterns, current_threshold, …)` (pure,
in `services/adaptive_workflows.py`) computes a recommended new org-wide
`auto_approve_below` dollar threshold. The evidence base is the **clean-history**
vendors — the *exact* gate `derive_suggestions` uses: `>= min_history` approvals,
**zero** rejections, **zero** modifications. The rule is deliberately
conservative and explainable:

- **Requires breadth of evidence.** At least `min_qualifying_vendors` (default 3)
  independent vendors must clear the clean-history gate, so a single chatty
  vendor can't move the org-wide limit. Fewer → `should_raise=False`,
  `reason_code="insufficient_evidence"`.
- **Candidate = the highest clean-approved amount** seen across qualifying
  vendors, rounded **up** to the nearest 500 (reporting currency) — every unit
  below it would have sailed through with a spotless record.
- **Refuses to price across currencies.** Every figure — the evidence, the
  candidate, the caps and `auto_approve_below` itself — is in the org's
  reporting currency, and a vendor with any approval that could not be expressed
  there is excluded from the evidence base. See *Every amount is expressed in
  the org's REPORTING currency* above.
- **Never lowers.** `recommended_threshold = max(current, capped_candidate)`; if
  the evidence supports nothing above the current limit it's a no-op
  (`reason_code="no_increase"`).
- **Caps the raise.** The candidate is clamped by the lower of a **relative** cap
  (`current × max_raise_multiple`, default 2×) and an **absolute** cap (default
  $25,000). The first raise off `0` (no threshold yet) skips the relative cap
  (0×anything is 0) and uses the absolute cap only. A capped-but-still-raising
  result carries `reason_code="at_cap"`.

The recommendation carries the full evidence list (`vendor_name`, `n`,
`max_approved_amount`, `median`) and a deterministic `rationale` — no LLM, like
the advisory suggestions. `GET /threshold-recommendation` reads the current
threshold off the active (or `?workflow_id=`-specified) workflow definition's
approval step and returns the recommendation; it never mutates anything.

#### Apply (`POST /threshold-recommendation/apply`)

The explicit, opt-in write. It writes the new `auto_approve_below` onto the
workflow definition's **approval** step (`steps_config.steps[].config`) — but
**through the audited workflow-definition PATCH path**, never as a raw row
mutation. Concretely it reuses the same two helpers the manual
`PATCH /api/workflows/{id}` uses when `steps` change:

- `workflow_definitions._snapshot_version(...)` writes a `WorkflowVersion` row
  snapshotting the **prior** `steps_config` (so the change is reversible /
  diffable in the no-code builder exactly like a manual edit);
- a `workflow.version_snapshot` audit row (`reason: adaptive_threshold_raise`)
  records that an edit happened, **plus** a second
  `workflow.auto_approve_threshold_raised` audit row records *what* changed
  (`previous_threshold` → `new_threshold`, the qualifying-vendor / clean-invoice
  counts, `source: adaptive_recommendation`) for the SOX trail.

Guards / behaviour:

- **RBAC `admin` only** — matches who can edit workflow definitions
  (`PATCH /api/workflows/{id}` is `require_roles(ROLE_ADMIN)`). CFO / ap_manager
  can **read** the recommendation (`GET`, manager/CFO read roles) but **cannot
  apply** it (403) — even though ap_manager can assign reviewers, editing the
  workflow definition is an admin act.
- **Recomputed server-side.** The threshold is re-derived from live stats inside
  the apply, so an admin can never apply a number the deterministic evidence no
  longer supports. The optional `expected_recommended_threshold` body field adds
  an optimistic-concurrency guard (409 if it no longer matches the fresh
  recommendation — guards against applying a stale UI value).
- **Idempotent / no-op safe.** When the recommendation doesn't raise the
  threshold (`insufficient_evidence` / `no_increase`) the apply is a no-op:
  `applied=false`, **no version snapshot, no audit row**. A second apply after a
  raise is likewise a no-op (`no_increase`).
- **409** when there's no workflow definition to update.
- **Affects only NEW invoices.** Per the project's workflow-snapshot invariant,
  `WorkflowInstance.steps_config_snapshot` is frozen at invoice creation, so a
  raised threshold changes nothing for in-flight invoices — only invoices created
  after the apply read the new limit. The `rationale` says so explicitly.

Response (`ApplyThresholdResponse`): `applied`, `workflow_id`,
`previous_threshold`, `new_threshold` (string-Decimal), `reason_code`,
`rationale`, `version_number` (the snapshot written, or `null` on a no-op).

## A/B testing of workflow rules

A controlled experiment comparing **two** workflow-rule configurations — an
**A** control and a **B** variant — running over the *same* workflow definition,
measured on objective, deterministic metrics so an org can answer "does the
variant actually approve faster / touchless-r / with fewer exceptions?" before
adopting it org-wide. Like the rest of this feature it is **local-first +
deterministic** (no LLM, no cloud key): both the A/B assignment and the metrics
are pure functions in `services/workflow_experiments.py` (unit-testable without a
DB), with the DB-touching assignment hook in
`services/workflow_experiments_runtime.py` and the SQL / lifecycle / results
shaping in `api/workflow_experiments.py`.

> **Routes / measures — never moves money.** An experiment only decides *which
> config an invoice runs under* and reads metrics back. It funds nothing; the
> CFO-gated payment run is unchanged.

### Model — `WorkflowExperiment` (tenant-scoped, migration 0064)

| Field | Meaning |
|---|---|
| `workflow_definition_id` | The definition under test — only invoices whose resolved definition is this one get assigned. |
| `config_a` / `config_b` | The two variant `steps_config` JSONBs (same shape the definition stores — a `{"steps": [ {step}, ... ]}` object; **validated at create / PATCH**, 422 on a config without a `steps` list or with a step entry lacking `type`). The chosen variant is frozen verbatim onto the invoice's `steps_config_snapshot` and read back via `get_step_config`, so an unreadable config would silently disable auto-approve / the approval thresholds / segregation for assigned invoices — hence the boundary check. `config_a` is the control. |
| `split_a_pct` | Percent of invoices routed to A (0–100; 50 = even). |
| `primary_metric` | The metric the winner is called on — `time_to_approval_days` \| `touchless_rate_pct` \| `exception_rate_pct` \| `rejection_rate_pct`. |
| `min_sample_per_variant` | Minimum *completed* invoices per arm before a winner is called. |
| `status` | `draft` → `running` → `concluded` (stop returns a running experiment to `draft`). |
| `started_at` / `ended_at` | Lifecycle timestamps. |
| `assignments` | `{invoice_id: "A"|"B"}` — the recorded, stable assignment per in-flight invoice, so the split is auditable and reproducible. |

**Once `assignments` is non-empty, the arms are frozen and the row is not
disposable.** `PATCH` refuses `config_a` / `config_b` / `split_a_pct` (409) and
`DELETE` refuses outright (409). Both guards key on the assignment record, not
on `status`, because `stop` — the documented way to leave `running` — returns
the row to `draft` **with its assignments intact**, which re-opened both
draft-only checks. Editing an arm then pooled invoices that ran the old config
with invoices that ran the new one under one label, so `GET /results` could
crown a variant on a config half the sample never saw — the per-invoice frozen
snapshot invariant (`../../docs/decisions.md` §13) broken one level up. And
`DELETE` destroyed the only record of which invoice ran which arm: `/results`
derives everything from `assignments`, the invoices' snapshots outlive the row,
and the `invoice.experiment_assigned` audit rows would be left naming an
experiment id that no longer resolves — exactly what the delete guard's own
message says it exists to prevent. The readout knobs (`name`, `description`,
`primary_metric`, `min_sample_per_variant`) stay editable: they change how the
same data is presented, not what the data is. A draft that never assigned
anything is still deletable.

Fans out to every tenant via `scripts/migrate_all_tenants.py`; fresh tenants get
it from `create_all` via the registered model (it is NOT a control table).

### Assignment at invoice creation (the frozen-snapshot invariant)

`workflow_engine.create_workflow_instance` — the single chokepoint where the
per-invoice `steps_config_snapshot` is frozen — calls
`maybe_assign_experiment_variant`. When a `running` experiment targets the
invoice's resolved definition (and its entity scope is compatible — an org-wide
NULL-entity experiment matches any invoice, an entity-scoped one only its own
entity), the deterministic `assign_variant(invoice_id, experiment_id,
split_a_pct)` picks A or B (stable SHA-256 hash → `[0,100)` bucket; **no
randomness, no clock**, so the same invoice always lands in the same variant and
two experiments split independently). The **chosen variant's config is frozen
onto the snapshot** — so in-flight invoices keep their variant for life, exactly
like any other workflow snapshot. The assignment is recorded on the experiment's
`assignments` map and a PII-free `invoice.experiment_assigned` audit row is
written. Candidate experiments are fetched `WITH FOR UPDATE` before the
`assignments` dict is read-modified-written — two invoices created concurrently
under the same running experiment would otherwise race a lost update (both read
the same dict, both add their own entry, the second full-dict write clobbers the
first); the row lock serializes them so the second caller's write always
includes the first's entry (issue #149). The whole hook is **best-effort**: its
caller swallows exceptions, so a routing failure falls back to the live
definition's config and never breaks invoice creation. At most one experiment is
honoured per invoice (the most-recently-started match) — running two over one
definition is a config mistake, not a compounded split.

### Results / readout — `GET /api/experiments/{id}/results`

`compute_experiment_results(rows_a, rows_b, primary_metric,
min_sample_per_variant)` aggregates per-variant metrics over the **recorded
assignments**. The API resolves each assigned invoice's review decision and
touchless flag with `services/touchless.touchless_classification_select` — the
**same predicates the dashboard's `touchless_rate` counts with** — and its
time-to-approval leg and exception presence from the audit_log + Exception
rows:

- **median / avg time-to-approval (days)** — over approved invoices, clock-start
  = the invoice's `ready_for_review` transition (fallback `created_at`), clamped
  ≥ 0 (reuses the adaptive `_decimal_days` leg).
- **touchless rate** — the one platform-wide definition
  (`backend/docs/analytics.md` § Touchless rate): approved automatically
  (`invoice.auto_approved`, and no `invoice.approved` / `approval_step` /
  `rejected` row), no field or line-item correction (`invoice.edited` /
  `line_items_edited` / `gl_recoded`), and no exception a person resolved,
  dismissed or escalated — over completed invoices. Until 2026-10 this readout
  used a narrower rule under the same label (auto-approved with no
  `details.changes` on the approval row) while the dashboard used a much wider
  one; the two can no longer disagree.
- **exception rate** — invoices that raised ≥ 1 exception, over **all assigned**
  (not just completed) — exposure is over all work routed to the arm.
- **rejection rate** — rejected over completed.

"Completed" = the invoice reached a review decision — the touchless rate's
shared denominator: cleared review (approved or later, evidence-gated for
`done` / `paid` / `failed`) or currently `rejected`. A rejection resubmitted
back into review is in flight again, and CSV-imported rows are never completed. A clear **"not enough data yet"** state guards the readout: until
*both* arms have `≥ min_sample_per_variant` completed invoices, `enough_data` is
False and `winner` is `null` (with per-arm `notes` saying how many more are
needed). Past the threshold a **winner** is called by a plain, explainable
direction check on the primary metric (lower-is-better for
time/exception/rejection, higher-is-better for touchless); an exact tie is
`"tie"`. **No statistical-significance test is claimed** — the rationale says so,
because that would over-promise on the small samples a single tenant produces.

`audit_log.details` is JSONB with no object-shape constraint, so
`_experiment_metric_rows` reads every `details` through `_details_obj`, which
returns `{}` for anything that isn't an object. A list / string / number there
can only come from a direct-DB write — precisely the tampering a control readout
should survive — and `.get(...)` on it used to raise `AttributeError` out of the
endpoint as a 500, losing the whole experiment's evidence over one row. It is
read as carrying nothing, matching `approval_signature.check_approval_row`,
which absorbs the same shape by counting the row rather than failing the period.
The decision and touchless flag no longer read `details` in Python at all — they
come from the shared SQL classification. Guarded by
`tests/test_experiment_results_malformed_details.py`; the dashboard/experiments
agreement by `tests/test_dashboard_aggregates.py`
(`test_experiments_readout_and_dashboard_agree_on_the_same_invoices`).

### Endpoints

| Route | Method | RBAC | Notes |
|---|---|---|---|
| `/api/experiments` | GET | admin / ap_manager / cfo | `?status=` filter; list |
| `/api/experiments` | POST | **admin only** | Create (draft). 404 if the workflow definition isn't this org's; 422 on a bad `primary_metric` |
| `/api/experiments/{id}` | PATCH | **admin only** | Edit — **draft only** (409 otherwise) |
| `/api/experiments/{id}/start` | POST | **admin only** | draft → running; idempotent (already-running is a no-op); 409 if concluded |
| `/api/experiments/{id}/stop` | POST | **admin only** | running → draft (stops *new* assignments; in-flight keep their frozen variant); 409 if not running |
| `/api/experiments/{id}/conclude` | POST | **admin only** | → concluded (terminal; results stay readable); idempotent |
| `/api/experiments/{id}` | DELETE | **admin only** | **draft only** (409 — stop/conclude a running one to preserve its measurement history) |
| `/api/experiments/{id}/results` | GET | admin / ap_manager / cfo | Per-variant metrics + winner / not-enough-data over the recorded assignments |

Mutate is **admin-only** (editing workflow rules is an admin act, like editing a
workflow definition); managers/CFO can read the list + results. Every mutation
writes a PII-free `workflow_experiment.{created,updated,started,stopped,concluded,deleted}`
audit row. Every route is behind `get_current_user` + `require_roles`; all are
tenant + org scoped (404 on cross-tenant fetch).

### Frontend — `/experiments`

A `/experiments` route under the **Settings** nav group (read managers/CFO,
mutate admin): status `FilterChips` (all / draft / running / concluded), an
experiments `DataTable` with clickable rows opening a **results readout** modal
(winner / not-enough-data banner + the per-variant metric table, primary metric
row highlighted), a create `Modal` (pick a definition — seeds both configs from
its live `steps_config` — set split %, primary metric, min sample, edit the two
JSON configs), and per-row start / stop / conclude / delete actions gated by
status + role. Over `#lib/api/experiments.ts` (types in
`#lib/types/experiments.ts`).

### Frontend — `/adaptive`

An `/adaptive` route under the **Settings** nav group, over
`#lib/api/adaptive.ts` (types live in that module — it is the only consumer,
and every money field on the wire is an exact decimal string). Read is
admin / ap_manager / cfo, matching `_READ_ROLES`; a clerk is redirected, since
every route here would 403 them.

Six `Tabs` panels, each with its own loading / error / empty state and its own
lazy load, so a reader pays for the panel they opened:

| Tab | Endpoint(s) | Act |
|---|---|---|
| Suggestions | `GET /suggestions` | `POST /suggestions/{id}/dismiss`, armed two-click, ap_manager+ |
| Auto-approve threshold | `GET /threshold-recommendation` | `POST .../apply`, **admin only** |
| Smart routing | `GET /routing-suggestion` (+ `GET /api/invoices?status=ready_for_review` for the picker) | `POST /routing-suggestion/apply`, ap_manager+ |
| Approval patterns | `GET /approval-patterns` | — |
| Anomalies | `GET /anomalies` | — |
| Feedback loop | `GET /feedback` | — |

Seven contracts the page encodes, each of which is easy to get wrong:

- **Advisory is stated, not implied.** A standing line under the page title says
  nothing here has changed a workflow, and each of the three acts is a separate,
  explicitly role-gated control. The two read-only panels (anomalies, patterns)
  say in their own intro that flagging raises nothing and blocks nothing.
- **The threshold apply sends `expected_recommended_threshold`** — the exact
  figure that was rendered. The 409 it can provoke is *the guard working*, not a
  failure, so it renders as its own persistent amber
  `[data-testid="adaptive-threshold-stale"]` region naming **both** figures (what
  was on screen, what it became), with the recommendation re-read underneath so
  the button now offers the current number. It is deliberately not routed into
  the panel's error state: "apply failed" would misdescribe both what happened
  (nothing was applied, and nothing is broken) and what to do next.
- **`GET /feedback` writes an `adaptive_feedback.viewed` access-audit row**, so
  the page never polls it and never fetches it speculatively — it loads on the
  explicit act of opening the Feedback tab (once) and on an explicit Refresh.
  `tests-e2e/adaptive/adaptive.spec.ts` asserts the request count is zero until
  the tab is opened.
- **The insufficient-data state renders as itself.** Below `_FEEDBACK_MIN_SAMPLE`
  the backend returns `value_pct: null`, and the UI renders "Not yet measurable"
  plus the backend's own explanatory `label` — never a `0%` that would read as
  "the automation is never overruled" on a two-invoice sample. The overturn-rate
  KPI shows an em dash in the same state.

- **An anomaly row's invoice id is a link, not a stub.** It renders through the
  shared `RowLink` as `/invoices?id=<uuid>` — the same deep link the exceptions
  queue uses — with the full uuid on the cell's `title` and in the accessible
  name. The truncated id it used to show could be neither opened nor pasted, so
  a flagged invoice was a dead end.
- **The amount is labelled with `amount_currency`, never the org's reporting
  currency.** An invoice the backend could not express in the reporting currency
  reaches the page as its *billed* figure; stamping the reporting currency on it
  is a wrong number, not a missing one. The `amount_comparison_unavailable` flag
  beside it says the amount rules abstained.
- **Flag severity maps through the shared `Badge` tone vocabulary** —
  `info → accent`, `warning → warning`, `error → danger`, anything else to the
  untinted `neutral` chip. A tinted badge reads as a signal, so folding `info`
  into `warning` (as `severity === 'error' ? 'danger' : 'warning'` did) made the
  page claim four flags were problems, including the one that reports the
  absence of a verdict.
- **The vendor average discloses what it excluded.** When any row carries
  `unconverted_count > 0` a `role="alert"` line sits above the table saying the
  figures are a floor — the §79/§82 treatment. The sample count is left alone:
  the two are reconciled by telling the reader, not by moving a denominator.

### Mobile — `AdaptiveScreen`

`mobile/lib/screens/adaptive_screen.dart` over `AdaptiveApi` +
`AdaptiveStore`, reached from **Settings → Administration** and gated on
`AuthStore.canViewAdaptive` (the same `_READ_ROLES`, so a clerk never sees the
entry point rather than tapping into a guaranteed 403). Three tabs — Suggestions,
Approval patterns, Anomalies — each with its own loading / error / empty state
and its own request sequence, so a failing anomaly scan cannot blank the
patterns a reader is looking at.

**Read-first, and the omissions are the design.** The only write is
`POST /suggestions/{id}/dismiss` (behind a confirm dialog, gated on
`canDismissSuggestion` = `_WRITE_ROLES`, so a CFO reads and is not offered it).
Neither apply path is exposed: `routing-suggestion/apply` reassigns a live
approval, `threshold-recommendation/apply` raises the org-wide threshold that
decides which invoices skip human review, and the second one carries the
stale-value 409 whose *whole point* is a surface that names both figures and
re-reads the recommendation. That is a desk decision with a desk-sized
affordance; a phone-sized one would be the same control with the explanation cut
off (`docs/decisions.md` §156). The Feedback tab is absent for a different
reason — `GET /feedback` writes an `adaptive_feedback.viewed` access-audit row,
so a second surface that could fetch it needs its own deliberate
"only when asked" treatment rather than riding along with two other tabs.

Two presentation calls mirror the web's, for the same reasons:

- **Per-vendor amounts render with no currency symbol**, under one section note
  saying they are in the org's reporting currency. The payload does not name that
  currency, and mobile has no org-currency store — stamping `$` on a ZAR org's
  averages would be a wrong number, not a missing one. Anomaly rows DO carry
  `amount_currency` and are labelled with it.
- **The unconverted count is disclosed** whenever it is non-zero: the excluded
  approvals are still in `sample_size`, so an average beside a count is an
  average over fewer rows than the count claims unless the reader is told.

`title` / `rationale` are rendered verbatim — they are sentences the backend
composes with the numbers and currency code in them, so they are English on both
surfaces; the screen's own chrome is localized in all six locales.

Tests: `mobile/test/stores/adaptive_store_test.dart`,
`mobile/test/screens/adaptive_screen_test.dart`, plus the `AdaptiveScreen` groups
in `mobile/test/a11y/accessibility_test.dart`.

Smart routing ranks *people*, so each candidate carries its `base_score`, the
`outcome_penalty` with the `overturn_rate_pct` and `outcome_sample_size` it was
read over, and the backend's own `reasons` list — an unexplained ranking of
colleagues is worse than none, and the "no down-weight (N decisions on record)"
line is rendered explicitly so a thin record can't be mistaken for a clean one.

E2E: `frontend/tests-e2e/adaptive/adaptive.spec.ts` (role gate both ways,
section-bar reachability, dismiss, apply, the stale-guard state, the audited-read
timing, and both feedback metric states) and
`frontend/tests-e2e/adaptive/presentation.spec.ts` (the openable invoice link,
the billed-vs-reporting currency label, the `info` badge tone, and the
unconverted-approvals disclosure). Backend-side, the two serialisers are pure
functions covered by `backend/tests/test_adaptive_response_currency.py`, which
includes a drift guard that `_vendor_pattern_dict` emits every
`VendorPatternResponse` field — without it `unconverted_count` could be dropped
again and the response would fall back to its schema default of `0`, a
disclosure that says "nothing excluded" when rows were.

## Tunables — `Organization.settings.adaptive`

Partial override (omit a key to inherit; unknown keys dropped — mirrors
`fraud_rules`):

| Key | Default | Used by |
|---|---|---|
| `sigma` | `2.0` | amount_high / amount_low |
| `median_multiple` | `3.0` | amount_high guard |
| `timing_multiple` | `3.0` | off_pattern_timing |
| `min_history` | `5` | baseline minimum (anomaly) |
| `suggestion_min_history` | `12` | suggestion minimum approvals |
| `suggestion_min_consistency_pct` | `95` | suggestion consistency gate |

## Feedback loop — outcomes fold back into the recommendations

The recommendations above are **forward-looking**: they read the *approval*
history and project a routing/threshold suggestion. The feedback loop
(`GET /api/adaptive/feedback`) closes the circle by reading what HAPPENED to the
invoices that already sailed through — the human **overturns** the system never
sees on the way in. It answers the roadmap's "corrections feed back into the
model" ask **deterministically**, with no trainable model and no LLM: the
realised outcomes self-correct the deterministic threshold recommendation, and a
real accuracy figure replaces the old "Not yet measured" placeholder.

> **Reframe.** There is no trainable model in this feature (it is all
> statistics). "Feedback loop" here means: feed human OUTCOMES
> (voids / re-rejections / corrections of *auto-approved* invoices) back into the
> deterministic threshold recommendation so it self-corrects, and surface an
> honest effectiveness signal — never a fabricated metric.

### The outcome signal (read straight from `audit_log`, no new instrumentation)

The population is the invoices the system **auto-approved** in the lookback
window (`invoice.auto_approved` audit rows). For each, an *overturn* is read from
that same invoice's later audit rows — all of which the app already writes:

| Overturn | Audit signal |
|---|---|
| **voided** | `invoice.voided_return_to_approved` — a payment on it was voided, sending it back to `approved` (the strongest "should not have been paid as-is" signal) |
| **rejected** | `invoice.rejected` after the auto-approval |
| **corrected** | `invoice.approved` carrying `details.changes` after the auto-approval (the post-void re-review walked the extraction back) |

An invoice is **overturned** if *any* of those fired (counted once regardless of
how many). `overturn_rate_pct = overturned / auto_approved × 100`. A single
LEFT-joined aggregate keeps it one round-trip; entity-scoped via the invoice
join. Only overturn rows **at or after** the auto-approval count (a rejection
that predates the auto-approval isn't an overturn *of* it).

### Outcome-adjusted threshold (`outcome_adjusted_threshold`, pure)

The forward `recommend_auto_approve_threshold` result is folded with the
auto-approval overturn rate, in three bands (defaults shown):

- **< 5 % overturn** — the auto-approved cohort is holding up; the forward
  recommendation **passes through unchanged**.
- **5 % – 15 %** (`outcome_pullback`) — overturns are climbing. The loop
  **refuses to raise** — the recommendation is pulled back to a no-raise
  (`should_raise=False`) with a rationale that cites the rate.
- **≥ 15 %** (`outcome_freeze`) — the auto-approved population is being walked
  back too often to trust *any* raise; identical no-raise outcome, stronger
  rationale.

Invariants: it **never lowers** the existing threshold (only declines to raise,
consistent with the forward rule); and when the auto-approved sample is below the
minimum (default 5), `insufficient_data` is True and the loop **leaves the
forward recommendation untouched** — it never reacts to one-off noise. Pure /
deterministic; `/feedback` returns BOTH the base (history-only) and the adjusted
recommendation so a held-back raise is **explainable** (mirroring how the anomaly
surface returns the baseline it compared against).

#### The adjustment governs the WRITE, not just the dashboard

`GET /threshold-recommendation`, `POST /threshold-recommendation/apply` and
`GET /feedback` all resolve the recommendation through the single
`_resolve_threshold_recommendation`, and **all three act on the adjusted one**.

This is load-bearing, not tidiness. The fold used to live only inside
`/feedback`, so the apply POST — the one endpoint that actually widens
auto-approve — recomputed the *forward* recommendation and widened it anyway. An
admin could read "holding at $0: 2 of 10 auto-approved invoices were later voided
or rejected (20 % overturn)" and, in the same request cycle, apply a raise to
$5,000 reported as `reason_code: "ok"`. The brake was wired to the dashboard and
not to the control, and the two read surfaces contradicted each other.

A held-back apply is a clean no-op: `applied=false`, `reason_code`
`outcome_pullback` / `outcome_freeze`, the measured rate in the `rationale`, and
**no** `WorkflowVersion` snapshot or `workflow.auto_approve_threshold_raised`
audit row (nothing changed, so nothing is recorded as having changed). Clean
outcomes still let the raise through unchanged.

The routing sibling never had this gap: its per-approver penalty is folded into
the candidate `score` itself, so `POST /routing-suggestion/apply` — which assigns
whichever candidate ranks first — inherits the down-weight for free.

### A real effectiveness signal (`compute_effectiveness`, pure)

Two metrics replace the "Not yet measured" placeholder — each carries an explicit
**insufficient-data** state (a `null` value + a "not yet measurable" label)
rather than a fabricated number:

1. **`auto_approval_overturn_rate`** — of the invoices the system auto-approved,
   the share a human later voided/corrected/rejected. The honest accuracy signal
   for the auto-approve automation (lower is better). Insufficient when the
   auto-approved sample is below the minimum (default 5).
2. **`recommendation_acceptance_rate`** — of all advisory `workflow_suggestions`
   surfaced, the share an admin actually applied (`status='applied'`). Measures
   whether the recommendations are trusted; insufficient (no divide-by-zero) when
   no suggestions have ever been surfaced.

### Tunables / boundaries

The bands (`pullback_overturn_pct` 5 %, `freeze_overturn_pct` 15 %) and the
minimum sample (5) are function arguments with conservative defaults; no new
`FEOH_` env var or migration. **Read-only** — the feedback endpoint never mutates
workflow state. It is a sensitive read (it exposes the org's approval-control
posture), so it writes a PII-free `adaptive_feedback.viewed` access-audit row
(field-names / counts only — no amounts, vendor, or PII), mirroring the other
SOX-instrumented reads. **Compute-on-read** over `audit_log` + the existing
`workflow_suggestions` table — no new column or migration.

## Endpoints

| Route | Method | RBAC | Notes |
|---|---|---|---|
| `/api/adaptive/approval-patterns` | GET | admin / ap_manager / cfo | `?days=180` (max 730) lookback |
| `/api/adaptive/anomalies` | GET | admin / ap_manager / cfo | `?invoice_id=<uuid>` (single) or batch (in-review queue, cap 200); 404 if the invoice isn't in this tenant/entity |
| `/api/adaptive/suggestions` | GET | admin / ap_manager / cfo | `?status=open|all` (default `open`), `?days=365`; **upserts** then returns |
| `/api/adaptive/suggestions/{id}/dismiss` | POST | admin / ap_manager | idempotent; 404 outside tenant |
| `/api/adaptive/routing-suggestion` | GET | admin / ap_manager / cfo | `?invoice_id=<uuid>` (required), `?days=180` lookback; 404 if the invoice isn't in this tenant/entity. Ranked, advisory — assigns nobody |
| `/api/adaptive/routing-suggestion/apply` | POST | admin / ap_manager | Body `{invoice_id}`, `?days=180`. Assigns the top recommendation via the audited `review.assign_reviewer`. 409 if not `ready_for_review`; 422 if no eligible approver; 404 outside tenant/entity; idempotent no-op when already assigned to the chosen approver. **Write surface — CFO excluded (read-only on routing).** |
| `/api/adaptive/threshold-recommendation` | GET | admin / ap_manager / cfo | `?days=365` lookback, `?workflow_id=<uuid>` (defaults to active definition). Conservative recommended raise to `auto_approve_below` + evidence + rationale, **outcome-adjusted** — the same decision the apply POST acts on. Read-only — never mutates the definition |
| `/api/adaptive/threshold-recommendation/apply` | POST | **admin only** | Body `{workflow_id?, expected_recommended_threshold?}`, `?days=365`. Raises `auto_approve_below` through the audited workflow-definition PATCH path (WorkflowVersion snapshot + audit rows). Acts on the **outcome-adjusted** recommendation, so it holds back (`applied=false`, `reason_code` `outcome_pullback`/`outcome_freeze`, no snapshot/audit row) while the auto-approved population is being walked back. 409 if no definition / stale `expected_recommended_threshold`; idempotent no-op when the recommendation doesn't raise. **Write surface — matches who can edit workflow definitions; CFO/ap_manager excluded.** Affects only NEW invoices (frozen workflow snapshots) |
| `/api/adaptive/feedback` | GET | admin / ap_manager / cfo | `?days=365` lookback, `?workflow_id=<uuid>` (defaults to active definition). The feedback loop — reads the realised OUTCOMES (voids/re-rejections/corrections) of auto-approved invoices from `audit_log`, returns the outcome tallies + two effectiveness metrics (each with an insufficient-data state) + BOTH the base and the outcome-adjusted threshold recommendation. **Read-only** — never mutates; writes a PII-free `adaptive_feedback.viewed` access-audit row |

Read routes exclude `ap_clerk` — this is a manager/CFO surface, matching the
analytics precedent. The **apply** POST is a *write*, gated to `admin` /
`ap_manager` (who can already assign reviewers), so CFO can read the
recommendation but not apply it. Every route is behind `get_current_user` +
`require_roles`.

## Persistence — `workflow_suggestions`

Tenant-scoped table (migration `0031_workflow_suggestions`, fans out to every
tenant DB; fresh tenants get it from `create_all` via the registered model).
Money in `payload` is **string-Decimal**; `confidence_pct` is `Numeric(5,2)` —
never a float.

The table is a **materialized cache**, not the source of truth for the
statistics:

- `GET /suggestions` recomputes `derive_suggestions` every call and **upserts**
  by `dedupe_key` (`INSERT ... ON CONFLICT (dedupe_key) DO UPDATE`): a new
  qualifying vendor inserts as `open`; an existing row's `payload / title /
  rationale / confidence` are refreshed to the latest stats but its **status is
  never overridden** (a `stale` row that holds again re-opens; `dismissed` /
  `applied` are left alone).
- An `open` suggestion whose condition **no longer holds** flips to `stale`
  (not deleted) so the history of what was once suggested survives.

**Why persist** (vs compute-on-read): a dismissal must be **durable** across
requests even as the underlying stats shift, and `status='applied'` is the hook
for the future apply-path slice. Compute-on-read can't remember a dismissal.

**Write-on-GET.** `GET /suggestions` performs the upsert. This is an idempotent
materialization — no money moves and it is not an auditable status change — so
it's acceptable for the single-round-trip ergonomics the UI wants. The noted
alternative, if a reviewer objects, is a `POST /suggestions/refresh` that does
the upsert plus a pure read-only `GET`.

**No audit row on dismiss.** Dismissing a suggestion mutates only the advisory
row's status — not an invoice, payment, approval, or vendor — so the
append-only-audit invariant (which is about those regulated status changes) does
not apply.

## Deferred follow-ups

- **Smart routing — apply path.** ✅ **Shipped** — `POST /routing-suggestion/apply`
  assigns the top recommendation through the audited `review.assign_reviewer`
  (audit row + notification + OOO delegation; never a raw `assigned_to_id`
  write). See § Smart routing — apply above.
- **Auto-adjusting thresholds.** ✅ **Shipped** —
  `GET /threshold-recommendation` recommends a conservative raise to the
  org-wide `auto_approve_below` and `POST /threshold-recommendation/apply`
  (admin-only) writes it **through the audited workflow-definition PATCH path**
  (WorkflowVersion snapshot + `workflow.version_snapshot` +
  `workflow.auto_approve_threshold_raised` audit rows). See § Auto-approve
  threshold — recommend + apply above. Remaining sub-follow-up: surface
  `status='applied'` on the per-vendor `workflow_suggestions` rows whose vendor
  evidence fed an applied raise (the org-wide apply doesn't currently flip the
  per-vendor advisory suggestion's status — the two stores stay independent so a
  dismissal/stale flip on a vendor suggestion can't be confused with the
  org-wide threshold history; revisit if a UI needs to thread them).
- **A/B testing** of workflow rules. ✅ **Shipped** — see § A/B testing below.
  Run a controlled experiment comparing two workflow-rule configs on objective,
  deterministic metrics; assignment freezes a variant config onto the invoice's
  snapshot at creation; the results endpoint calls a winner past a minimum
  sample. `/api/experiments` + `services/workflow_experiments*.py` + migration
  0064 + the `/experiments` frontend surface.
- **Feedback loop — outcomes adjust the recommendations.** ✅ **Shipped** —
  `GET /api/adaptive/feedback` reads the human OUTCOMES of auto-approved invoices
  (voids / re-rejections / corrections) straight from `audit_log` and folds the
  overturn rate back into the threshold recommendation (it pulls back to a
  no-raise when overturns climb), plus a real `auto_approval_overturn_rate` +
  `recommendation_acceptance_rate` effectiveness signal (each with an honest
  insufficient-data state). Deterministic, no LLM, no migration. See § Feedback
  loop above. Sub-follow-up — **per-approver outcome down-weighting in the
  *routing* recommendation**: ✅ **Shipped** — `recommend_approvers` now folds
  each approver's overturn rate (their decisions later voided / corrected-by-
  someone-else / rejected, from `compute_approver_outcomes`) into the routing
  score as a banded, bounded subtractive penalty (≤ 30 pts, no penalty below a
  5-decision min-sample, never hard-zeros), surfaced + explained on each
  `RoutingCandidate` (`base_score` / `outcome_penalty` / `overturn_rate_pct`).
  Reuses the shared `is_overturned` primitive so the threshold- and routing-side
  overturn parsing isn't duplicated. See § Smart routing — outcome down-weighting.
- **Model-retraining feedback loop** (a *trainable* model). The deterministic
  feedback loop above closes the "corrections feed back" ask without one; a real
  trainable model is a far larger, separately-scoped effort and is not built here.
- **LLM phrasing** of suggestion text — if added, must fail soft to the template
  with no key (mirror `services/audit_summary.py`).
- Business-day-weighted time-to-approve (currently simple elapsed days).
- **Web UI** — ✅ **Shipped** as `/adaptive`; see § Frontend — `/adaptive` above.
- **Mobile UI** — ✅ **Shipped** as `AdaptiveScreen`; see § Mobile —
  `AdaptiveScreen` above. Read-first: the two apply paths stay on the web.
