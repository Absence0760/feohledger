# Invoice Processing Workflow

This document describes the end-to-end workflow for processing an invoice from upload through ERP submission.

## Overview

```
┌────────┐     ┌─────────┐     ┌──────────┐     ┌───────────┐     ┌──────┐
│ Upload │ ──> │ Extract │ ──> │  Review  │ ──> │  Send to  │ ──> │ Done │
│        │     │  (AI)   │     │ (Human)  │     │    ERP    │     │      │
└────────┘     └─────────┘     └──────────┘     └───────────┘     └──────┘
```

Each invoice carries a **correlation ID** (UUID) that is propagated to every workflow instance, workflow step, audit log entry, and payment record — enabling full lifecycle traceability with a single query.

## Invoice Statuses

| Status              | Meaning                                                            | Workflow Stage     |
|---------------------|--------------------------------------------------------------------|--------------------|
| `new`               | Created, no file attached yet                                      | Pre-workflow       |
| `pending`           | File uploaded, AI extraction in progress                           | Stage 1: Extract   |
| `ready_for_review`  | Extraction complete, awaiting reviewer                             | Stage 2: Review    |
| `approved`          | Reviewer approved, ready for ERP push                              | Between 2 and 3    |
| `rejected`          | Reviewer rejected, needs edits/re-upload                           | Stage 2 (branch)   |
| `sending_to_erp`    | Async ERP submission in flight                                     | Stage 3: ERP Send  |
| `sent_to_erp`       | ERP confirmed receipt                                              | Stage 4 (start)    |
| `posted_in_erp`     | ERP posted/booked the invoice (set via inbound ERP webhook)        | Stage 4            |
| `payment_scheduled` | Added to a payment run, awaiting execution                         | Stage 5 (payments) |
| `paid`              | Payment executed                                                   | Stage 5            |
| `done`              | Workflow complete — terminal state, invoice is now immutable       | Final              |
| `failed`            | Any stage failed                                                   | Error state        |

The state machine in `services/workflow_engine.py` enforces every transition below — payment-path states (`posted_in_erp`, `payment_scheduled`, `paid`) are driven by inbound ERP webhooks + payment-run execution, but they go through `transition_invoice()` like any other transition, so they all produce audit-log rows.

## Status Transitions

```
new ──────────────> pending                  (file uploaded, extraction triggered)
new ──────────────> ready_for_review         (extraction skipped or pre-filled)
new ──────────────> approved                 (auto-approve confidence path)
new ──────────────> done                     (no workflow steps enabled → terminal)
pending ──────────> ready_for_review         (extraction succeeded)
pending ──────────> approved                 (auto-approve over threshold)
pending ──────────> failed                   (extraction failed / reaper)
pending ──────────> new                      (AI reading paused — plan allowance / spend cap used; manual entry, see billing.md)
ready_for_review ─> approved                 (reviewer approves)
ready_for_review ─> rejected                 (reviewer rejects)
rejected ─────────> ready_for_review         (re-submitted after edits)
rejected ─────────> new                      (requires full re-upload)
approved ─────────> sending_to_erp           (ERP submission initiated)
approved ─────────> payment_scheduled        (direct schedule — no ERP step)
approved ─────────> done                     (no further steps → terminal)
sending_to_erp ───> sent_to_erp              (ERP confirmed)
sending_to_erp ───> failed                   (ERP rejected or timed out)
sent_to_erp ──────> posted_in_erp            (ERP webhook confirms post)
sent_to_erp ──────> done                     (terminal — workflow complete)
posted_in_erp ────> payment_scheduled        (payment run picks up the invoice)
posted_in_erp ────> done                     (no payment step in workflow)
payment_scheduled > paid                     (processor settles the payment)
payment_scheduled > approved                 (void payment → back to queue)
paid ─────────────> done                     (terminal)
paid ─────────────> approved                 (void payment → back to queue)
failed ───────────> pending                  (retry extraction)
failed ───────────> sending_to_erp           (retry ERP push, if previously approved)
```

`done` is terminal. The `payment_scheduled → approved` and `paid → approved` back-edges are the **void-payment** path (`POST /api/payments/{id}/void`): the invoice returns to the queue so it can be re-scheduled. Every transition above writes an audit-log row through `transition_invoice()`; nothing in the payment path mutates `invoice.status` directly.

All transitions are enforced by a state machine in `services/workflow_engine.py`. Invalid transitions return `409 Conflict`. Authoritative graph: `VALID_TRANSITIONS` in that file.

## Stage 1: Upload & Extraction

**Endpoint:** `POST /api/invoices/upload` (multipart/form-data)

1. Accept file upload (PDF, PNG, JPEG, TIFF — max 25 MB).
2. Store file in S3/MinIO under `{organization_id}/{invoice_id}/{filename}`.
3. Create the Invoice record with `status=new` and placeholder fields (`invoice_number="PENDING"`, `amount=0`). Populate `file_key` and `file_url`.
4. Create a WorkflowInstance and the first WorkflowStep (`type=extraction`).
   Whether extraction runs at all is read from the snapshot that instance just
   froze (`is_step_enabled(..., invoice_id=invoice.id)`), never by re-resolving
   the live definition — a second resolution can disagree with the frozen one
   (breaking the snapshot invariant, `decisions §13`) and `get_or_create_workflow_definition`
   *inserts* a definition when it finds none, inside the upload transaction.
5. Transition invoice to `pending`.
6. Dispatch async AI extraction task.
7. Return `202 Accepted` with the invoice ID and `correlation_id`.

**Extraction outcomes:**
- **Success:** Extracted fields are written to the invoice, vendor matching links the invoice to a `Vendor`, the per-vendor correction cache overlays cached priors, RAG few-shots inform the prompt, and `services.invoice_warnings.refresh_warnings` runs to populate warnings, exceptions, and the **2/3-way PO match** (persisted on `invoice.po_match`). Before that final call, `run_extraction` appends its own findings straight onto `invoice.warnings` — arithmetic/self-correction violations (`extraction_self_correction`), a hallucinated/deactivated GL code (`gl_account_invalid`), a semantic near-duplicate (`duplicate_similar`). `refresh_warnings` recomputes every category IT owns from scratch on each call, but preserves these three upstream categories (`invoice_warnings.UPSTREAM_WARNING_TYPES`) instead of blind-overwriting `invoice.warnings` — otherwise they'd be silently erased the moment it runs (its own fully-recomputed categories are unaffected: a stale one that no longer applies is correctly dropped, not kept forever). An `InvoiceExtractionResult` row is created with the confidence score and raw output, and the invoice transitions to `ready_for_review`.
- **Failure:** Invoice transitions to `failed`. Error details are stored in `WorkflowInstance.state_data`.
- **Timeout:** If extraction has not completed within 5 minutes, the invoice is transitioned to `failed` with reason `extraction_timeout`.

## Stage 2: Review

A human reviewer examines the extracted data and either approves or rejects the invoice.

### Approver Configuration

The approval step in the workflow definition supports three strategies:

| Strategy | Behavior |
|---|---|
| **Manual** | When submitting for review, the user picks an approver from a dropdown. The selected user is assigned and sees Approve/Reject buttons on the invoice. |
| **Specific** | One or more approvers are pre-configured in the workflow. Invoices are auto-assigned (round-robin). Only the assigned user sees the review buttons. |
| **Auto** | Invoices skip human review entirely and are auto-approved. |
| **Chain** | Multi-level sequential approval. Each level defines a min/max amount range, approver list, and required approval count. All approvals at level N must complete before level N+1 begins. See **Multi-Level Approval Chains** below. |

Multiple approvers can be configured for the "specific" strategy via the workflow editor's search-and-pick interface.

### Who Can Review

- If an invoice has an `assigned_to_id`, only that user sees Approve/Reject buttons in the modal.
- If no one is assigned, any user with a non-clerk role can review.
- AP Clerks never see review buttons regardless of assignment.

### Approval Thresholds

All thresholds are read from `steps_config_snapshot` (frozen per invoice at creation time).

| Config key | Behavior |
|---|---|
| `auto_approve_below` | Invoices with amount below this value skip human review entirely — auto-approved. |
| `require_cfo_above` | Non-CFO users receive 403 when attempting to approve invoices above this amount. |
| `max_invoice_amount` | Invoices above this amount are rejected outright (422). |

**All three are bare numbers denominated in the org's REPORTING currency**, as
are the approval chain's per-level `min_amount` / `max_amount` bands. That is
the convention `payments.cfo_approval_above` follows
(`services/payment_controls.cfo_approval_decision`) and
`settings.expense_approval.cfo_threshold` follows; a second convention would
mean two money controls on the same invoice measured in different units.

Nothing in a JSONB config declares its currency, so the *amount* has to be
brought to it. `approval_chain.reporting_gate_amount(invoice, amount=…,
org_settings=…)` returns a `GateAmount` — the figure in the reporting currency
plus whether it could be established at all — converted at the rate already
**locked on the invoice row** by `invoice_warnings._refresh_reporting_amount`.
No FX call is made at gate time: fetching one would make the same invoice pass
or fail a control depending on the minute, and let a market move retroactively
change a decision already on the audit trail. `structuring.vendor_recent_spend`
is scoped to the invoice's own currency, so one rate prices the whole aggregate.

Every site goes through it — `review._enforce_approval_thresholds` (human
approval), `extraction.decide_auto_approve` (unattended), the four
exception-agent resolvers, and `resolve_applicable_levels` (chain bands).
Before this they each compared a raw `Invoice.amount`, so a GBP 9,000 invoice —
USD 11,400 — read as under a USD 10,000 `require_cfo_above` and was approved by
an `ap_manager` with no CFO signature, and routed to the manager tier of a chain
whose senior level starts at 10,000. `require_cfo_above` is the control that
decides whether a CFO has to sign at all, which makes it the sharpest instance.

`GateAmount.expressible=False` (no locked rate, or a lock that provably
describes a different currency pair) means there is **no comparison to make**,
and every consumer fails CLOSED:

| Site | Fail-closed behaviour |
|---|---|
| `cfo_gate_applies` / `max_amount_gate_applies` | The gate fires — a human (a CFO, for the CFO gate) decides. Same direction as a malformed threshold, via the same `_money_gate_applies` body. |
| `decide_auto_approve` amount floor | The floor does **not** fire, so the invoice goes to human review. |
| `resolve_applicable_levels` | The amount bands are skipped and every routing-rule-matching level applies — for a chain, fail-closed means MORE approvers, since an empty result is no chain requirement at all. Non-amount routing rules still filter. |

`GateAmount` exists as a value rather than a convention because there are five
of these comparisons and a convention is what the sixth one forgets. A plain
`Decimal` is still accepted and means "already in the gate currency" — the
correct reading for a single-currency tenant and for the expense-report path,
which locks its reporting figure at submit. Guarded by
`tests/test_approval_gate_currency.py`, whose AST scan fails if any gate site in
`review.py`, `extraction.py` or the resolvers goes back to a raw amount.

**`require_cfo_above` fails CLOSED on a malformed value.** The threshold is
parsed through the single shared helper `approval_chain.cfo_gate_applies`, which
is reused by the human-approval gate (`review._enforce_approval_thresholds`), the
expense-report gate (`api/expenses`, key `expense_approval.cfo_threshold`), and
the auto-approve revoke check (`extraction.decide_auto_approve`). An explicitly-
configured but **unparseable** threshold — a settings typo like `"5,000"`, an
empty string, a non-numeric/non-finite value (`NaN`/`Infinity`), or a value an
insider tampered to defeat the control — is treated as **"CFO approval
required"**, never silently skipping the gate. It does not raise: one bad
settings write can neither disable the fraud control nor 500 (brick) the whole
approval queue — even a legitimate CFO can still approve past the fail-closed
gate. The malformed value is logged PII-free (a money threshold, not a secret)
for an admin to correct. The payment-run CFO gate (`payments.cfo_approval_above`)
enforces the same fail-closed discipline inline (see `payments.md`).

**So does `max_invoice_amount`, and so do the auto-approve knobs.** The cap has
its own named sibling, `approval_chain.max_amount_gate_applies` (both share one
`_money_gate_applies` body, so the two can't drift), and
`extraction.decide_auto_approve` reads `auto_approve_below` through
`approval_chain.finite_money_threshold` and the confidence bar through its own
`_confidence_threshold`. Every one of those sites previously coerced the raw
config with a bare `Decimal(str(...))` or compared straight against it, so a
non-numeric value **raised**:

- out of `review._enforce_approval_thresholds` as an unhandled `InvalidOperation`
  — a **500 on every approval** under that workflow, legitimate ones included,
  with no path forward at all (unlike the CFO gate, where a CFO can still
  approve past a fail-closed refusal);
- out of the pure `decide_auto_approve`, which on the extraction path lands the
  invoice in `failed` rather than surfacing as a refusal.

Now: an unusable cap **refuses** the approval (422, naming the misconfiguration
rather than formatting a value that would raise); an unusable
`auto_approve_below` is simply not a floor, and an unusable
`auto_approve_threshold` disables the confidence trigger — both directions that
send the invoice to a human. `NaN`/`Infinity` count as unusable everywhere, for
the reasons in `finite_money_threshold`'s docstring.

**These values are also refused at the save boundary.** `steps_config` is JSONB
and `POST /api/workflows/import` accepts it as a free-form dict — the one save
path no Pydantic `Decimal | None` field constrains (create/update go through
`WorkflowStepConfig`). `workflow_builder.validate_builder_steps` therefore now
checks the canonical `approval` step's three money thresholds and each chain
level's `min_amount` / `max_amount`, plus the `extraction` step's
`auto_approve_threshold`, and returns a per-field 422. The runtime fail-closed
behaviour above stays the backstop for definitions that predate this check or
were edited directly in the database. Chain bounds matter here even though
`resolve_applicable_levels` never raises on them: it reads an unparseable bound
as *no bound*, silently widening the level to every amount and routing money
past the tier that should have seen it.

**Structuring guard**: both gates above compare against `invoice.amount` **plus** the same vendor's other invoice amounts over a trailing window (`services/structuring.py`, called from `review._enforce_approval_thresholds` on the human path and from `extraction.resolve_gate_aggregate` on the unattended one) — closing the "split one large payable into several under-threshold invoices with distinct invoice numbers" bypass (the exact-match duplicate check in `invoice_warnings.py` never fires on distinct numbers, and per-invoice thresholds never aggregated). Config lives alongside the other fraud-rule knobs on `Organization.settings.fraud_rules`: `structuring_enabled` (default `true`) and `structuring_window_days` (default `7`). Rejected/failed invoices don't count toward the aggregate; everything else does, including still-pending ones. The rejection/CFO-required message names the aggregate and the vendor's other recent spend when the single invoice alone would have passed.

**The auto-approve path measures the same aggregate.** `extraction.decide_auto_approve`
(the revoke check above) stays PURE and takes the figure as an `aggregate_amount`
argument; both call sites — `extraction.run_extraction` and
`api/workflow.complete_invoice`'s amount-floor path — compute it with the shared
`extraction.resolve_gate_aggregate`, which is `review`'s own computation
(`invoice.amount + vendor_recent_spend(...)`, guarded by
`structuring.get_structuring_config`). Evaluating the gates against this invoice
alone left the split-payable bypass wide open on the path with *no human in it* —
strictly worse than the human hole the guard was added to close. `resolve_gate_aggregate`
never raises: a lookup failure degrades to the single-invoice comparison (still
gated) rather than breaking extraction. `auto_approve_below` deliberately keeps
measuring the **single** invoice — it is a "too small to be worth a human's time"
convenience, not a spend control, and aggregating it would quietly stop it firing
for any frequent vendor.

**Neither unattended path fires for an entry-only caller.** Entry is open to
`ap_clerk` (`api/invoice_entry.py`), and a clerk with no manage role cannot
approve. So their `/complete` skips the
`auto_approve_below` floor (whatever the org's `require_segregation`), and their
`upload` / `extract` dispatch extraction with `suppress_auto_approve=True`
(the flag the supplier-portal resubmit already used): the invoice always lands at
`ready_for_review`. Otherwise a clerk could choose or re-key a document under the
floor — on an intake invoice nobody uploaded, where segregation has no one to
bind — and have it approved with no second person involved. Their `/complete`
also takes only `new` → `ready_for_review`, and only where the snapshot has an
approval step (with none, `/complete` closes a `new` invoice to `done`).

### Multi-Level Approval Chains

Strategy `"chain"` with `approval_chain: list[ApprovalLevelConfig]`.

Each `ApprovalLevelConfig`:

| Field | Purpose |
|---|---|
| `min_amount` | Lower bound for this level to apply (reporting currency — see *Approval Thresholds*) |
| `max_amount` | Upper bound, nullable for open-ended (reporting currency) |
| `approver_ids` | List of eligible approver UUIDs |
| `required_approvals` | Number of approvals needed at this level |
| `name` | Display name (e.g. "Manager", "CFO") |

Chain state is tracked in `WorkflowInstance.state_data["approval_levels"]`. Levels are sequential: all approvals at level N must complete before level N+1 becomes active. The invoice stays in `ready_for_review` until all applicable levels are satisfied.

**`approval_chain` owns that JSONB slot.** Nothing else under `app/` names the
key: reads go through `get_chain_progress(instance)` (off a row) or
`chain_state_of(state_data)` (off a raw mapping — including the deep copy the
in-place mutators work on, which is returned by identity so their nested writes
land), the reject path clears it with `clear_chain_state`, and the writer plus
the escalation sweep's SQL predicate use `CHAIN_STATE_KEY`.
`tests/test_approval_chain_state_owner.py` is the drift guard (an AST scan over
`app/`, so a comment explaining the key is still allowed).

A key present holding JSON `null` means exactly what an absent key means — **no
chain**, and the next approval initialises one. Nothing in the app writes a
`null` (`init_chain_state` writes an object, `clear_chain_state` removes the key
outright), so one can only arrive from a hand-edited row, a restore, or an
importer serialising absence. That is why the owner coerces with `or {}` rather
than `.get(key, {})`: the latter hands `None` to a caller that immediately calls
`.get("levels", …)` on it, which is an `AttributeError` on the approval path
rather than a routing decision.

**Named-approver enforcement**: a non-empty `approver_ids` on the current level is a hard allow-list, enforced by `approval_chain.check_level_approver` before the approval is recorded — the endpoint's role-based RBAC gate (`require_permission(PERM_INVOICE_APPROVE)`, held by any `ap_manager`/`cfo`/`admin`) only confirms the actor holds an approving role, not that they are one of the named approvers, so this is a separate, additional check. An empty `approver_ids` list is unrestricted (any actor who cleared RBAC may approve, matching legacy behaviour). A named approver's active delegate (`User.delegate_to_id` / `delegate_until`) is also authorized. A non-authorized actor gets a 403 and the approval is not recorded.

The single-level strategy `"specific"` applies the same named-approver check (`approver_ids`, or the deprecated single `approver_id`) without the multi-level chain machinery — useful when a step needs exactly one or a small fixed set of eligible people but no sequential levels.

#### Escalation widens eligibility — it never narrows it

A level may carry `escalation_hours` + `escalation_to_user_ids`. When it has sat
at the head of the chain longer than `escalation_hours`, the
`approval_escalation` sweep calls `approval_chain.apply_escalation`, which makes
the escalation targets eligible and appends a PII-free record to the level's
`escalations` list (the sweep also writes an `invoice.approval_escalated` audit
row). The one invariant across every branch: **the set of people who may approve
can only grow.**

| Level shape | What escalation does |
|---|---|
| `parallel_mode: "any"`, non-empty `approver_ids` | Appends the targets, preserving the configured order — a new approver who can independently clear `required_approvals`. |
| `parallel_mode: "all"`, non-empty `approver_ids` | **Substitutes** every not-yet-approved approver with the targets (already-signed-off approvers are kept). Appending would make a stuck level need *more* sign-offs than before — the opposite of unblocking (issue #128). |
| **Empty `approver_ids` (unrestricted)** | **No-op, both modes.** `check_level_approver` reads an empty allow-list as "any RBAC-cleared actor may approve", so the targets are already eligible. Writing them in would turn `[]` into `[target…]` and 403 the entire AP team — the issue-#128 inversion arriving through the empty-list case. Such a level is never eligibility-blocked anyway: `any` mode counts distinct approvals without consulting `approver_ids`, and `all` mode over an empty list is satisfied by the first approval. |

Escalation is idempotent — once a level has absorbed a target set, re-running is
a no-op, so the sweep can run on a tight interval and across overlapping
replicas.

**A target who could not approve is never added.** Segregation of duties refuses
the payable's implicated actors (`uploaded_by_id` ∪ `segregation_actor_ids`,
unless the approval step sets `require_segregation: false`), and
`advance_approval_chain` refuses anyone who already approved a different level.
`apply_escalation` filters both out of the targets (`ineligible=` from
`approval_chain.escalation_ineligible`, plus the chain's own earlier approvals).
Adding them gained nothing in `any` mode — it only notified them to approve
something they would be refused — and in `all` mode it *substituted* them in as
a required approver, leaving a level nobody could clear (and, the sweep being
idempotent, permanently so). If every target is ineligible the escalation is a
no-op.

**Level 0 escalates before anyone approves.** Chain state is still created
lazily by the first approval, so a chain whose first approver never acts has no
state — and used to be invisible to the sweep, meaning level 0 could never
escalate. The sweep now also selects active instances with no chain state whose
snapshot's approval strategy is `chain` and whose invoice is
`ready_for_review`; for those it initialises the chain through the same
`approval_chain.init_chain_for_invoice` the first approval uses (identical
routing), clocking level 0 from when the invoice entered review — the open
`approval` step's `created_at`, else the instance's. The chain is only persisted
if an escalation actually fires; otherwise the transaction rolls back and the
lazy path is untouched.

Routing is still decided at the **first approval**, on the post-correction
invoice. A sweep-initialised chain carries `routing` (the indices of the
configured levels it was built from), and while no level has an approval
recorded, `approval_chain.ensure_chain_routed` (called by `approve_invoice`
after corrections) re-routes it if the corrected amount or coding now selects
different levels — an amount corrected up into the CFO band picks up the CFO
level. Levels in both routings keep their escalated approvers and history, and
level 0 keeps its clock; if no level applies any more the chain is dropped and
the approval is single-level. Once any approval is recorded the routing is
frozen, as it always was, and a chain without `routing` (hand-built / legacy) is
never rewritten.

An `all` level that *configures* an approver who is implicated in a particular
invoice still cannot clear unless an eligible escalation substitutes them — see
`docs/known-issues.md`.

### Segregation of Duties

`require_segregation: bool` on the approval step config. It is **default-on**:
an absent key means enabled, and only an explicit `require_segregation: false`
turns it off (`approval_chain.violates_segregation`). When enabled:

- The approver must not be one of the payable's **implicated actors** —
  `Invoice.uploaded_by_id` ∪ `Invoice.segregation_actor_ids`. The second column
  names the other people who shaped the payable's terms: a recurring template's
  author and material editors, or, on an inter-company mirror, the source
  payable's whole implicated set.
- An approver in that set is refused with 403.
- With `uploaded_by_id` NULL **and** the set empty there is nothing to refuse.
  That combination means the invoice came in through a channel with no employee
  behind it — email intake, inbound PEPPOL, or the supplier portal — not
  "pre-existing invoices": every path where a signed-in employee creates an
  invoice stamps the uploader.

Full rule, and why NULL is permissive rather than fail-closed: root `CLAUDE.md`
§ RBAC roles ("Approver ≠ creator keys on a SET") and `docs/decisions.md` §141,
§152, §192.

### Delegation / Out-of-Office

Users can designate a delegate who receives their approval assignments while they are away.

- `User.delegate_to_id` — FK to the delegate user (control plane).
- `User.delegate_until` — datetime after which delegation expires.
- `assign_reviewer()` checks delegation and reassigns to the delegate when active.
- `WorkflowStep.original_assigned_to` records the intended user so the audit trail shows both the original assignee and the delegate.

API:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/auth/delegation` | Check current delegation status |
| `POST` | `/api/auth/delegation` | Set delegate: `{delegate_to_id, until}` |
| `DELETE` | `/api/auth/delegation` | Clear delegation |

### Endpoints

| Method | Path                              | Purpose                          | Status Guard        |
|--------|-----------------------------------|----------------------------------|---------------------|
| POST   | `/api/invoices/{id}/assign`       | Assign a reviewer                | `ready_for_review`  |
| POST   | `/api/invoices/{id}/approve`      | Approve (with optional edits)    | `ready_for_review`  |
| POST   | `/api/invoices/{id}/reject`       | Reject with a reason             | `ready_for_review`  |
| POST   | `/api/invoices/{id}/resubmit`     | Re-enter review after edits      | `rejected`          |

### Approve

1. Validate invoice status is `ready_for_review`.
2. Apply any field corrections included in the request body.
3. Set `approval_date` to today, `approved_by` to the reviewer's name.
4. Transition to `approved`.
5. Complete the review WorkflowStep with `action=approved`.
6. Write audit log (`invoice.approved`).

### Reject

1. Validate invoice status is `ready_for_review`.
2. Set `rejected_by` to the reviewer's name.
3. Transition to `rejected`.
4. Complete the review WorkflowStep with `action=rejected`.
5. Create an exception record with the rejection reason.
6. Store rejection reason in the audit log details.
7. Write audit log (`invoice.rejected`).

### Resubmit

After a rejection, the user can edit the invoice fields and resubmit for another round of review. This transitions `rejected → ready_for_review` and creates a new review WorkflowStep.

A rejection counter is tracked in `WorkflowInstance.state_data`. After a configurable number of rejections, the invoice may be auto-escalated or locked.

## Stage 3: Send to ERP

**Endpoint:** `POST /api/invoices/{id}/send-to-erp`

Can be triggered manually by the user or automatically after approval.

1. Validate invoice status is `approved`.
2. Transition to `sending_to_erp`.
3. Create a WorkflowStep (`type=erp_export`).
4. Dispatch async ERP call. The invoice's `correlation_id` is sent as an idempotency key to prevent duplicate records in the ERP system.

### Retry Logic

| Scenario             | Behavior                                                                 |
|----------------------|--------------------------------------------------------------------------|
| Transient failure    | Retry with exponential backoff, up to 3 attempts. Stay in `sending_to_erp`. |
| Permanent failure    | Transition to `failed`. Record error in WorkflowStep and audit log.     |
| Success              | Transition to `sent_to_erp`. Store ERP reference ID in `state_data`.    |

**Manual retry:** `POST /api/invoices/{id}/retry-erp` — only valid when status is `failed` and the invoice was previously approved (i.e., `approved_by` is set).

## Stage 4: Done

`done` is the terminal state. When the ERP confirms receipt:

1. Complete the `erp_export` WorkflowStep.
2. Create a final WorkflowStep (`type=done`, `action=completed`).
3. Transition `sent_to_erp → done`.
4. Set `WorkflowInstance.state = "completed"`.
5. Write audit log (`invoice.erp_confirmed`).

The invoice is **immutable** in the later half of the lifecycle. PATCH and DELETE reject requests when the invoice is in `sending_to_erp`, `sent_to_erp`, `posted_in_erp`, `payment_scheduled`, `paid`, or `done` (the `IMMUTABLE_STATUSES` set in `app/api/invoices.py`).

## Workflow Models

### WorkflowDefinition

Represents the workflow template. The `steps_config` JSONB column defines the step sequence:

```json
{
  "steps": [
    { "number": 1, "type": "extraction", "name": "Data Extraction" },
    { "number": 2, "type": "approval",   "name": "Manager Approval" },
    { "number": 3, "type": "erp_export", "name": "ERP Export" },
    { "number": 4, "type": "done",       "name": "Complete" }
  ]
}
```

Step types `extraction`, `approval`, `erp_export`, `done` are canonical. Legacy aliases `upload`, `review`, `erp_push` are still accepted (`STEP_TYPE_ALIASES`) for backwards compatibility but new configs should use the canonical names.

Seeded per tenant at organization creation. Configurable for custom approval chains.

#### The step-type vocabulary has ONE owner

`app/services/workflow_step_types.py` is the single source of truth for every
step type the platform recognises, and for what may be done with each:

| Name | Meaning |
|------|---------|
| `CANONICAL_STEP_TYPES` | The four pipeline steps that drive the invoice state machine. **Order is load-bearing** — a `WorkflowStep.step_number` is this tuple's 1-based index, and `complete_current_step` finds the open step by ordering on that number. Nothing may be appended, reordered, or removed without migrating the rows already carrying those numbers. |
| `BUILDER_STEP_TYPES` | The five no-code builder types below. Orchestration config only: they have **no** step number and are never persisted as a `WorkflowStep`. |
| `KNOWN_STEP_TYPES` | The union — everything a persisted `steps_config` may legally name. |
| `STEP_TYPE_ALIASES` | The three legacy names, resolved before persisting so a query filtering on `"approval"` can never miss an alias-named row. |
| `is_known_step_type()` | The gate the definition-save chokepoint runs. |
| `canonical_step_index()` | Resolves a `step_number`, or refuses by name. |

`workflow_engine` and `workflow_builder` **re-export** these; neither redeclares
them. Before that, both modules held hand-copied literals with no cross-check
between them (plus a third copy as the `Literal` on
`schemas/workflow.py::WorkflowStepConfig.type`, which
`tests/test_workflow_step_types.py` now drift-guards). The consequence was two
real gaps:

- **Nothing validated a persisted step type.** `is_known_step_type` existed but
  had no production caller. `POST /api/workflows/import` takes `steps_config` as
  a free-form dict — the one save path a Pydantic `Literal` does not constrain —
  so a typo'd `"aproval"` step persisted happily and was then *silently ignored*
  at runtime, which reads to the engine as "no approval step configured". A
  workflow could lose its approval gate to a spelling mistake. `validate_builder_steps`
  now rejects any step whose type `is_known_step_type()` refuses, before persist.
- **The engine resolved a step number with a bare `.index()`.** A builder type
  reaching `create_workflow_step` raised `ValueError: list.index(x): x not in list`
  — a 500 naming neither the value nor the cause. `canonical_step_index()` now
  raises `NonCanonicalStepTypeError` (a recognised builder type used where a
  pipeline step is required) or `UnknownStepTypeError` (not a step type at all),
  both naming the offending value; both subclass `ValueError` so existing
  handlers still catch. `advance_workflow` resolves **before** closing the
  current step — it used to close it first, so the raise left an instance with a
  closed step and no successor.

The posture is `decisions §29`'s: a step type we do not recognise is refused by
name, never quietly coerced into something plausible.

**There is deliberately no boolean `is_canonical_step_type()`.** One existed,
with no caller and no test, and it answered a strictly worse version of the
question the table above already covers: `False` for both `"condition"` (a
recognised builder type used where a pipeline step is required) and `"aproval"`
(not a step type at all). Collapsing those two into one bare boolean is exactly
the silent coercion this section refuses — `canonical_step_index()` separates
them by name. Where a plain predicate genuinely suffices,
`validate_builder_steps` asks the inverse *after* `is_known_step_type()` has
passed (`step_type not in BUILDER_STEP_TYPES`); within that guarded set the
shorthand is equivalent to "resolves into `CANONICAL_STEP_TYPES`", aliases
included, and `tests/test_workflow_step_types.py` pins that equivalence so a
future alias or builder name cannot quietly break it.

#### Per-entity selection (multi-entity Phase 3)

`workflow_definitions` carries a nullable `entity_id` (`EntityMixin`): a definition either belongs to a specific subsidiary (`entity_id` set) or is **shared / org-wide** (`entity_id IS NULL`). When a new invoice is created, `workflow_engine.get_or_create_workflow_definition(db, organization_id, entity_id)` (called by `create_workflow_instance` with `invoice.entity_id`) resolves which definition governs it, in precedence order:

1. The invoice's own entity's active definition — prefer its `is_default`, then the oldest active (`created_at` tiebreak).
2. Otherwise a shared / org-wide active definition (`entity_id IS NULL`) — same default-then-oldest ordering.

If neither exists the org-wide default is auto-created with `entity_id = NULL` and `is_default = true`, so a single-entity tenant keeps getting exactly one org-wide definition — fully backward compatible. The resolved definition's `steps_config` is then snapshotted onto the `WorkflowInstance` as usual (in-flight invoices never see a later edit).

##### Activate / deactivate are scoped to the definition's own bucket

`PATCH /api/workflows/{id} {"is_active": …}` operates **inside one entity
scope**, never org-wide:

- **Activating** deactivates only the peers in the SAME bucket — the definition's
  own `entity_id`, or (NULL-safe) the shared bucket when it is the shared one.
  The peer-deactivation `UPDATE` used to carry no `entity_id` predicate, so
  activating subsidiary A's definition silently deactivated subsidiary B's *and*
  the shared org-wide fallback, defeating the per-entity resolution above.
- **Deactivating** is refused with **409** when it would leave the scope with no
  active definition at all. An entity-scoped definition may still be deactivated
  while an active shared definition remains (falling back to it is the documented
  behaviour); the last active shared one may not. Without the guard, the next
  invoice hits the lazy auto-create above — which mints an `is_default = true`
  row that collides with any existing shared default under
  `uq_workflow_definitions_one_default`, turning an ordinary invoice
  create/upload into a 500. Mirrors the default / active / in-flight-instances
  guards `DELETE /api/workflows/{id}` already applies.

At most one `is_default = true` definition may exist per `(organization_id, entity_id)`, enforced by the partial unique index `uq_workflow_definitions_one_default` on `(organization_id, COALESCE(entity_id, '00000000-…-0000'::uuid)) WHERE is_default = true` (the COALESCE sentinel collapses the NULL/shared bucket to a single key, since SQL treats `NULL != NULL`). The index is declared on the model (so fresh `create_all` tenants get it) and installed on existing tenants by migration `0050_workflow_per_entity_default` (which first demotes any pre-existing duplicate defaults, keeping the earliest `created_at` per group). See `../../docs/multi-entity.md`.

## No-Code Builder step types

The visual **No-Code Workflow Builder** lets an admin compose a workflow out of
the four canonical pipeline steps above plus five NEW builder step types. They are
stored in the **same** `steps_config` JSONB (`{ "steps": [ <step>, ... ] }`) — no
enum migration is needed. Each step keeps the standard shape:

```json
{ "number": 1, "type": "<type>", "name": "...", "enabled": true, "config": { ... } }
```

The builder types **orchestrate and branch**; they do **not** alter the invoice
state machine (`VALID_TRANSITIONS` is unchanged). The engine recognises them via
`workflow_engine.KNOWN_STEP_TYPES` / `is_known_step_type()` so a definition that
contains them is accepted rather than rejected. All builder logic lives in
`services/workflow_builder.py` and is consumed by the simulation service and the
import/create validation path.

| Type | What it does |
|------|--------------|
| `condition` | Branch the path on invoice field rules — jump (`goto`) to another step or fall through. |
| `parallel` | Fan an approval out to multiple branches; join on all / any / N approvals. |
| `webhook` | Call an external URL. **Recorded-not-sent by default** (local-first). |
| `email` | Send a notification through the existing email adapter (`console` default). |
| `delay` | Wait for a duration / until an invoice date field. **Never sleeps** — records intent. |

### Config shapes

- **condition**
  ```json
  { "rules": [ {"field": "amount", "operator": "gt", "value": 1000} ],
    "match": "all" | "any",
    "on_true_goto": <int|null>, "on_false_goto": <int|null> }
  ```
  - `field` ∈ `amount`, `currency`, `vendor_id`, `gl_account`, `cost_center`, `department`
  - `operator` ∈ `gt`, `gte`, `lt`, `lte`, `eq`, `ne`, `in`, `not_in`, `starts_with`
  - `*_goto` is a target step **`number`** (null = fall through to the next step)
- **parallel**
  ```json
  { "branches": [ {"name": "Finance", "approver_ids": ["..."]} ],
    "join": "all" | "any", "min_approvals": <int|null> }
  ```
- **webhook**
  ```json
  { "url": "https://...", "method": "POST"|"GET"|"PUT",
    "headers": {"X-Key": "..."}, "body_template": "...|null",
    "timeout_seconds": 10 }
  ```
- **email**
  ```json
  { "to": "approver"|"vendor"|"custom", "to_addresses": ["a@b.com"],
    "subject": "...", "body_template": "..." }
  ```
- **delay**
  ```json
  { "duration_seconds": 3600, "until_field": "due_date|null" }
  ```

### Condition goto / branching semantics

`evaluate_condition(config, ctx)` evaluates every rule against the invoice
context, then combines them with `match`:

- `match: "all"` (default) — every rule must pass (a rule-less condition is
  vacuously **true**).
- `match: "any"` — at least one rule must pass (a rule-less condition is
  vacuously **false**).

`amount` rules compare on `Decimal` (so `"100"` equals `100.00`); every other
field compares as a string. Numeric operators (`gt`/`gte`/`lt`/`lte`) always
coerce both sides to `Decimal`. The result carries the resolved branch target:
`goto = on_true_goto` when matched, else `on_false_goto`. A `null` goto means
"fall through to the next step." `validate_builder_steps` rejects a `goto` that
points at a step number not present in the workflow.

### Parallel join semantics

`resolve_parallel(config)` turns the join rule into a concrete `required`
approval count:

- `join: "all"` → every branch must approve (`required == len(branches)`).
- `join: "any"` → one approval clears the join (`required == 1`).
- `min_approvals` (when set) overrides the join, clamped to `[1, len(branches)]`.

The returned dict — `{branches, join, min_approvals, required}` — is what the
simulation/runtime uses to decide whether the parallel gate is satisfied.

### Local-first executor behavior

`execute_custom_step(step, ctx, *, dry_run=False)` runs the `webhook` / `email` /
`delay` steps and returns `{"type", "status": "ok"|"skipped"|"error", "detail"}`.
**`dry_run=True` (simulation) has zero side effects.** Per rail 7 (local-first),
every executor runs on a dev laptop with no cloud and no network:

- **webhook** — defaults to a no-network **recorded** result; the actual HTTP
  send belongs to deployed orchestration. Even when `config.enabled` is set, the
  engine records intent rather than calling out from the dev/simulation path. A
  missing `url` returns `status: "error"`.
- **email** — sends via the existing email adapter (`console` by default).
  `to: "custom"` with no addresses returns `skipped`; `approver`/`vendor`
  recipients are resolved by the caller at runtime (the engine records the kind
  only, never an address — PII-free). Adapter failures degrade to `error`,
  never raise.
- **delay** — **never sleeps**; it records the intended wait (`duration_seconds`
  or `until_field`). A deployed scheduler consumes the intent; dev + simulation
  proceed immediately.

`condition` and `parallel` are **not** executed through `execute_custom_step` —
they branch the path, so they're resolved via `evaluate_condition` /
`resolve_parallel` instead. `build_invoice_context(invoice)` maps an Invoice ORM
object (or a plain dict / SimInvoice) to the evaluation context, keeping `amount`
as a `Decimal` (never float).

### WorkflowInstance

One per invoice. Tracks progress through the definition's steps.

| Field            | Purpose                                                    |
|------------------|------------------------------------------------------------|
| `correlation_id` | Copied from the invoice for cross-table tracing            |
| `invoice_id`     | FK to the invoice                                          |
| `current_step`   | Index into the steps_config array                          |
| `state`          | `active`, `paused`, `completed`, `failed`                  |
| `state_data`     | JSONB — retry counts, extraction confidence, ERP ref, etc. |

### WorkflowStep

One row per step attempted. Created when a step begins, updated on completion.

| Field          | Purpose                                             |
|----------------|-----------------------------------------------------|
| `correlation_id`| Same as the instance                               |
| `step_number`  | Matches the definition step number                  |
| `step_type`    | `extraction`, `approval`, `erp_export`, `done` (canonical) |
| `assigned_to`  | Reviewer user UUID (for review steps)               |
| `action`       | Outcome: `extracted`, `approved`, `rejected`, etc.  |
| `completed_at` | When the step finished                              |

## Correlation ID

Every record related to an invoice shares the same `correlation_id`:

```
Invoice.correlation_id  (source of truth — auto-generated on creation)
    ├── WorkflowInstance.correlation_id
    │       └── WorkflowStep.correlation_id
    ├── AuditLog.correlation_id
    ├── PaymentSchedule.correlation_id
    └── Payment.correlation_id
```

To retrieve the full lifecycle of any invoice:

```sql
SELECT * FROM audit_log WHERE correlation_id = '<id>' ORDER BY created_at;
```

An `X-Correlation-ID` HTTP header is read/generated by middleware and stored in a context variable for structured logging.

## Audit Logging

Every state transition writes an audit log entry via `services/audit.py`. The `GET /api/invoices/{id}/audit-log` endpoint resolves `actor_id` UUIDs to human-readable `actor_name` strings by looking up users in the control-plane database. The frontend displays these in a timeline view within the invoice modal.

Each entry records:

- `correlation_id` — links to the invoice
- `organization_id` — tenant scoping
- `actor_id` — user who performed the action
- `action` — standardized action name
- `entity_type` / `entity_id` — what was changed
- `details` — JSONB with context (old/new status, rejection reason, ERP response, etc.)

### Action Names

| Action                         | Trigger                         |
|--------------------------------|---------------------------------|
| `invoice.created`              | Invoice created (manual entry)  |
| `invoice.uploaded`             | File uploaded                   |
| `invoice.extraction_completed` | AI extraction succeeded         |
| `invoice.extraction_failed`    | AI extraction failed            |
| `invoice.assigned_for_review`  | Reviewer assigned               |
| `invoice.approved`             | Reviewer approved               |
| `invoice.rejected`             | Reviewer rejected               |
| `invoice.resubmitted`         | Re-entered review after edits   |
| `invoice.erp_submitted`       | ERP push initiated              |
| `invoice.erp_confirmed`       | ERP confirmed receipt           |
| `invoice.erp_failed`          | ERP push failed                 |
| `invoice.erp_retried`         | Manual ERP retry                |

## Error Handling

| Scenario                    | Handling                                                             |
|-----------------------------|----------------------------------------------------------------------|
| Concurrent transitions      | `SELECT ... FOR UPDATE` prevents race conditions                     |
| Extraction timeout          | `services/extraction_reaper.py` runs in-process every `FEOH_EXTRACTION_REAPER_INTERVAL_SECONDS` (60s default); transitions any invoice in `pending` for more than `FEOH_EXTRACTION_TIMEOUT_SECONDS` (600s default) to `failed` and appends an `extraction_timeout` warning. Reviewer can re-trigger or fall back to manual entry. Same logic available as `python scripts/reap_stuck_extractions.py` for one-shot use. |
| Orphaned `sending_to_erp`   | (pending) — same pattern, not yet implemented for the ERP push path  |
| Duplicate invoices          | Check `invoice_number` + `vendor_name` per org; return `409 Conflict`|
| Rejection loops             | Track count in `state_data`; auto-escalate after N rejections        |
| File validation             | Max 25 MB; allowed types: PDF, PNG, JPEG, TIFF                      |
| ERP idempotency             | `correlation_id` sent as idempotency key to the ERP                  |

## API Endpoints Summary

### Workflow Actions

| Method | Path                                  | Purpose                                    | Returns |
|--------|---------------------------------------|--------------------------------------------|---------|
| POST   | `/api/invoices/upload`                | Upload file, start workflow                | 202     |
| POST   | `/api/invoices/{id}/extract`          | Manually (re-)trigger extraction           | 202     |
| POST   | `/api/invoices/{id}/reset-extraction` | Reset stuck `pending` extraction to `new`  | 200     |
| POST   | `/api/invoices/{id}/assign`           | Assign reviewer                            | 200     |
| POST   | `/api/invoices/{id}/approve`          | Approve invoice                            | 200     |
| POST   | `/api/invoices/{id}/reject`           | Reject invoice                             | 200     |
| POST   | `/api/invoices/{id}/resubmit`         | Resubmit for review                        | 200     |
| POST   | `/api/invoices/{id}/send-to-erp`      | Initiate ERP push                          | 202     |
| POST   | `/api/invoices/{id}/retry-erp`        | Retry failed ERP push                      | 202     |
| POST   | `/api/invoices/{id}/complete`         | Advance to next workflow step              | 200     |

Upload, extract, reset-extraction, resubmit and complete take
`INVOICE_ENTRY_ROLES` — invoice entry, `ap_clerk` included;
approve / reject are `invoice.approve`; assign, send-to-erp and retry-erp stay
role-gated to the manage roles. See `docs/user-management.md` for what an
entry-only caller may do.

### Read Endpoints

| Method | Path                              | Purpose                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/api/invoices/{id}/workflow`     | Workflow instance + steps        |
| GET    | `/api/invoices/{id}/audit-log`    | Full audit trail                 |
| GET    | `/api/invoices/{id}/extraction`   | AI extraction results            |

### Existing Endpoint Modifications

- `PATCH /api/invoices/{id}` — Reject when status is `sent_to_erp` or `sending_to_erp`.
- `DELETE /api/invoices/{id}` — Reject when status is `sent_to_erp` or `sending_to_erp`.

## New Files

| File                              | Purpose                                   |
|-----------------------------------|-------------------------------------------|
| `backend/app/api/workflow.py`     | Workflow action endpoints                 |
| `backend/app/schemas/workflow.py` | Request/response schemas for actions      |
| `backend/app/services/__init__.py`| Services package                          |
| `backend/app/services/workflow_engine.py` | State machine and transition logic |
| `backend/app/services/storage.py` | S3/MinIO file upload                      |
| `backend/app/services/extraction.py` | AI invoice extraction                  |
| `backend/app/services/review.py`  | Review approve/reject/resubmit logic      |
| `backend/app/services/erp.py`     | ERP integration client with retry         |
| `backend/app/services/audit.py`   | Audit log helper                          |

## Implementation Order

1. **Foundation** — State machine, audit service, extend InvoiceStatus enum.
2. **Upload** — Storage service, extraction service (mock initially), upload endpoint.
3. **Review** — Approve/reject/assign/resubmit endpoints.
4. **ERP** — ERP client, send/retry endpoints.
5. **Polish** — Guards on existing endpoints, read endpoints, frontend UI.
