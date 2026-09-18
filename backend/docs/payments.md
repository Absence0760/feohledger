# Payments

## Overview

Payments are the final step in the AP workflow. After an invoice is approved (or sent to ERP), it enters the payment pipeline where it gets scheduled, batched into a payment run, and executed.

```
Invoice Approved
    |
    v
Payment Scheduled          (auto or manual — based on due date & payment terms)
    |
    v
Payment Run Created        (batch of invoices grouped for execution)
    |
    v
Payment Executed           (ACH, wire, check, or virtual card)
    |
    v
Reconciled                 (matched against bank statement — future)
```

## Key Concepts

### Payment Schedule

A payment schedule is created for each approved invoice. It determines *when* to pay based on:

- **Due date** from the invoice
- **Payment terms** (Net 30, 2/10 Net 30, etc.)
- **Early-pay discount** — if terms like "2/10 Net 30" exist, the schedule tracks the discount window

| Field | Type | Description |
|---|---|---|
| invoice_id | UUID | FK to the invoice |
| correlation_id | UUID | Links to the invoice's correlation ID for traceability |
| due_date | Date | When payment is due |
| discount_date | Date | Last day to claim early-pay discount (nullable) |
| discount_percent | Decimal | Discount percentage if paid by discount_date (nullable) |
| payment_terms | String | Original payment terms from the invoice |

**Example:** An invoice with "2/10 Net 30" terms dated April 1:
- `discount_date`: April 11 (10 days)
- `discount_percent`: 2.00
- `due_date`: May 1 (30 days)

### Payment Run

A payment run is a batch of payments executed together. Think of it as "the Friday AP run" — an admin selects invoices to pay, reviews the batch, and executes.

| Field | Type | Description |
|---|---|---|
| status | String | `draft` → `submitted` → `processing` → `completed` / `failed` |
| total_amount | Decimal | Sum of all payments in the run |
| initiated_by | UUID | User who created/executed the run |
| executed_at | DateTime | When the run was executed |

**Payment Run Statuses:** (the full set — `schemas/payment.py::PaymentRunStatus`)

| Status | Meaning |
|---|---|
| `draft` | Created, invoices selected but not yet submitted |
| `executing` | Claimed by `/execute`; the per-payment dispatch loop is running |
| `submitted` | At least one payment is in flight, waiting on a processor webhook |
| `processing` | Payments are being executed (async) |
| `partial` | At least one payment succeeded AND at least one failed |
| `completed` | Every payment in the run succeeded |
| `failed` | Every payment failed |
| `cancelled` | A draft run cancelled before execution; its payment rows were deleted |

The bucketing and the precedence that turns payment outcomes into a run status
live in ONE place — `services/payment_runs.py::rollup_payment_statuses` /
`PaymentRunRollup.run_status` — shared by the dispatcher that *persists*
`run.status` and by the run-detail / runs-list reads that *report* it, so the
two can't drift.

### Payment

An individual payment record linked to a single invoice. Created when a payment run is executed or when a one-off payment is made.

| Field | Type | Description |
|---|---|---|
| invoice_id | UUID | FK to the invoice being paid |
| payment_run_id | UUID | FK to the batch run (nullable for one-off payments) |
| correlation_id | UUID | Links to the invoice's correlation ID |
| amount | Decimal | Payment amount |
| method | String | `ach`, `wire`, `check`, `virtual_card` |
| status | String | `pending` → `processing` → `completed` / `failed` / `cancelled` |
| reference | String | External reference (check number, wire ref, ACH trace) |

**Payment Statuses:**

| Status | Meaning |
|---|---|
| `pending` | Created, not yet processed |
| `processing` | Payment execution in progress |
| `completed` | Payment confirmed |
| `failed` | Payment failed (insufficient funds, bad account, etc.) |
| `cancelled` | Payment voided before execution |

## User Interface

The payments page (`/payments`) consolidates all payment activity — including virtual cards — into one view with three tabs.

### Page Layout

```
/payments
  ├── Summary Bar        — Total Paid, Pending, Ready to Pay, Payments, Rebates Earned
  ├── Tab: Queue         — invoices ready to pay (sorted by due date, overdue highlighted)
  ├── Tab: History       — all payments in one table (ACH, wire, check, virtual card)
  └── Tab: Runs          — payment batches
```

Virtual cards are **not** on a separate page. Card payments appear in the History tab with a card badge and last-4 digits. This gives one source of truth for "where did the money go."

### Queue Tab

Shows invoices in payable statuses (`approved`, `sent_to_erp`, `posted_in_erp`, `payment_scheduled`) that don't have a completed payment yet.

- Sorted by due date (soonest first)
- Overdue invoices highlighted in red with badge
- Shows payment terms per invoice
- Count shown in the tab badge

### History Tab

All payments across all methods in one table.

| Column | Description |
|---|---|
| Invoice # | Linked invoice number |
| Vendor | Vendor name |
| Method | Badge: ACH, Wire, Check, or **Card** (highlighted) |
| Amount | Payment amount |
| Status | pending / processing / completed / failed / cancelled |
| Reference | Check number, wire ref, ACH trace, or card last-4 |
| Date | Payment date |

Search and status filter chips available on this tab.

### Runs Tab

Payment batch history — each run shows status, total (under the run's own
currency — see *A payment states what its money is denominated in*), payment
count, and execution date.

### Every list here separates "empty" from "we could not look"

All five lists on this page — Queue, History, Runs, and the Rebates + Cards
tables stacked on the Cards tab — carry a `*Errored` flag alongside their
loading flag, so a failed read renders its own copy instead of the empty claim.
It matters here because each empty message is a **substantive claim**: "No
virtual cards issued yet." says this tenant has never issued a card, and
rendered over a 500 (with the toast that reported it already faded) it answers
"we could not look" with "there is nothing".

The Cards table was the last one without it. `/exceptions` states the rule
verbatim: *never reintroduce an empty message that outranks "we could not
look"*. Guards: `tests-e2e/payments/cards-load-states.spec.ts`,
`tests-e2e/payments/runs-load-states.spec.ts`.

### Summary Bar

Five KPI cards at the top:
- **Total Paid** — sum of all completed payments
- **Pending** — sum of pending/processing payments
- **Ready to Pay** — count of invoices in queue
- **Payments** — total payment count
- **Rebates Earned** — sum of virtual card rebates (green, only shown if > 0)

### Virtual Card Security

Card details (full number, CVV) are never stored in the database — only `last_four` and `provider_card_id`.

- Full card details are retrieved on demand from Lithic/Nium via `/api/cards/{id}/details`
- **Access restricted** to admin and AP manager roles — clerks and CFOs get 403
- **Every access is audit-logged** with action `card.details_viewed`
- Details are not cached in the frontend
- In future: require password re-entry before revealing

### Payment Flow

The flow is split into **create draft** and **execute** so a CFO can review what's about to be paid before money moves.

1. User navigates to **Payments > Queue**
2. **Selects invoices** via checkboxes (select-all available)
3. Action bar shows count and total: *"3 selected — $18,050.00"*
4. Clicks **Review & Pay** — review panel slides in
5. **Chooses payment method per invoice** (ACH, Wire, Check, Virtual Card) via dropdown
6. Clicks **Create Draft Run · 3 Invoices** — this:
   - `POST /api/payments/runs` creates a `PaymentRun` with `status='draft'`
   - Pending payment rows are created (no money has moved)
   - The Run Detail modal opens automatically showing the draft
7. Toast: *"Draft payment run created — review and execute"*
8. In the **Run Detail modal** the user reviews the payments table and clicks **Execute**:
   - `POST /api/payments/runs/{id}/execute` flips the run to `completed`
   - Generates payment references (e.g., `ACH-20260406-001`)
   - Updates invoice statuses to `payment_scheduled`
   - **Triggers async ERP sync** in background
9. Toast: *"Payment run executed — 3 payments completed. ERP sync in progress."*
10. Queue clears, History/Runs/Summary update; the modal stays open showing the now-completed run

The same Run Detail modal is reachable by clicking any row in the **Runs** tab — for completed runs it shows the payments + references; for stale drafts it offers Execute.

### Segregation of duties on payment runs (maker-checker)

The user who **creates** a payment run cannot also **execute** it (the
money-movement step) or **CFO-approve** it. Enforced in
`services/payment_controls.check_run_segregation` by comparing the actor's id to
the run's `initiated_by`, wired into both `POST /runs/{id}/execute` and
`POST /runs/{id}/approve` (both return **403** for the run's creator). A
different user must perform the second step.

This is **orthogonal to the role/permission split**. `require_permission`
separates `payment_run.approve` from `payment_execute` by *role*, but the
default `ap_manager` holds both, so without this identity check a single
`ap_manager` could create a run and immediately execute it — the entire payment
lifecycle with no second human. The control mirrors the invoice-approval
`check_segregation` (uploader ≠ approver).

**Default-on.** Single-operator accounts opt out per-org with
`Organization.settings.payments.require_run_segregation: false` (any value other
than an explicit `false` keeps the secure default). A legacy run with a NULL
`initiated_by` is never blocked (nothing to compare against).

### A standalone payment can't be injected into a run

`POST /api/payments` used to accept a `payment_run_id` in the body and write it
straight to the FK: the run was never checked to exist, to be `draft`, or to
belong to the caller's entity, and neither `run.total_amount` nor
`run.requires_cfo_approval` was recomputed. Two consequences:

- **The run-level CFO gate could be split around.** `requires_cfo_approval` is
  computed once, at run creation, from the run's own `total_amount`, and
  `/execute` reads only that flag. Injecting N legs each individually under
  `payments.cfo_approval_above` left both untouched, so `/execute` dispatched
  the inflated run with no sign-off. The per-payment CFO check on this endpoint
  bounds each injected leg, not the run.
- **A payment could be attached to a terminal run**, where nothing ever
  dispatches it — the row stays `pending` forever, occupying the invoice's
  `uq_payments_one_live_per_invoice` slot with `/void` as the only exit.

A payment that belongs to a run is created BY the run
(`services/payment_runs.create_payment_run_for_invoices`), which stamps the FK
itself, so the field has no legitimate caller here and is gone from
`PaymentCreate`. Pydantic ignores an unknown key, so a stray one is simply not
honoured. Pinned by `tests/test_payment_create_cfo_gate.py`.

### Every by-id route is entity-scoped

Multi-entity Phase 2 scopes reads and writes by the `X-Entity-ID` header. The
list surfaces (`GET /payments`, `/queue`, `/summary`, `/counts`, `/runs/`) have
honoured it since that landed, but every **by-id** route used to resolve its
row on the primary key alone — `GET /payments/{id}`, `/{id}/remittance`,
`POST /{id}/void`, `/{id}/compliance/{release,dismiss}`, `POST /payments`,
`GET /runs/{id}`, and `POST /runs/{id}/{approve,cancel,execute,retry-failed,resume}`.
Inside one tenant that let a user with subsidiary A selected read, void,
release, CFO-approve and execute subsidiary B's money simply by knowing the id:
the entity selector was advisory on exactly the routes that move money.

`_get_scoped_payment` / `_get_scoped_run` in `api/payments.py` are the fix,
mirroring `api/positive_pay.py::_get_scoped_file` on the sibling treasury
router. Two properties matter:

- **Opaque 404.** An out-of-scope id returns the same `Payment not found` /
  `Payment run not found` as one that doesn't exist — never a 403 — so the
  response can't be used to enumerate another subsidiary's payments.
- **The row locks are unchanged.** The scope predicate is one more `WHERE`
  clause on the same `SELECT ... FOR UPDATE`; pass `for_update=True` on the
  mutating callers (see the section below).

The consolidated view (no header, or `X-Entity-ID: all`) still sees every
entity's rows, so single-entity tenants and pre-multi-entity API consumers are
unaffected. Pinned by `tests/test_payment_entity_scope.py`.

**Run CREATION is scoped too**, one step earlier than the by-id routes.
`POST /payments/runs` resolved its invoices with `WHERE id IN (…)` and no
entity filter, so a run staged with subsidiary B selected could be built out of
subsidiary A's invoices — the run landed under B (visible and executable from
B's queue) while each payment was stamped with A's entity, so executing it
moved A's money from B's screen. `create_payment_run_for_invoices` now takes
the **selected** entity (`scope_entity_id`, from `get_entity_id`, nullable)
separately from the entity new rows are **stamped** with (`entity_id`, from
`get_write_entity_id`, never null) and filters the invoice lookup by the
former. Same split `POST /payments` has always used, same opaque 404, and the
consolidated view (`None`) is unrestricted exactly as before. The copilot's
draft-run route passes the same selected entity its own commitment rows were
built from, so the two can't diverge.

### Every run-state endpoint row-locks

`POST /runs/{id}/approve`, `/execute`, `/resume` and `/cancel` all read the
run with `SELECT ... FOR UPDATE`, and `POST /payments/{id}/void` (plus the two
compliance handlers) lock the payment the same way. The lock is not
belt-and-braces on any of them — each guards a distinct double-spend or
double-record race, and a plain `SELECT` there means two requests both pass
the status guard.

`/cancel` was the exception, and the most dangerous one, because it is the
endpoint that **deletes** the run's child `Payment` rows. `/execute` locks,
flips the run to `executing`, then commits — releasing the lock — *before* its
adapter loop, so an unlocked `/cancel` that had already read `draft` sailed
past the guard and deleted the very payments being handed to the processor.
Both outcomes were reproducible against the unfixed code: the canceller
winning outright (payments deleted, adapter never called, the run reporting
success having paid nothing) and the rows vanishing mid-dispatch (real money
out, no `Payment` record, run reads `cancelled`). Pinned by
`tests/test_payment_concurrency.py::test_cancel_racing_execute_cannot_delete_dispatched_payments`
— BUG D in that file's header.

**Adding a run- or payment-state endpoint? Lock the row.** The file's existing
handlers each carry a comment naming the race their lock prevents; follow the
pattern rather than assuming the status guard alone is enough.

### Execution atomicity + resuming a stuck run

`execute_payment_run`'s per-payment loop is durable, not all-or-nothing: each
payment is dispatched (via the internal `_execute_single_payment`) inside its
own try/except catch-all, then committed immediately — a failure on payment N
(including an uncaught error from a live FX/sanctions/processor adapter, not
just the anticipated `InternationalPaymentError`) can only roll back payment
N's own still-open attempt, never the payments already recorded before it.

A worker crash mid-loop leaves the run at `status="executing"` with every
payment up to the crash point already safely committed with its real
outcome, and everything after it still `pending`. `POST
/runs/{id}/resume` (same `payment.execute` permission gate) re-drives only
those still-`pending` payments and re-rolls-up the run's final status across
*every* payment on the run — nothing already settled is re-sent to the
processor. `POST /runs/{id}/execute` deliberately stays `draft`-only rather
than also accepting `executing`: a run that is still genuinely mid-execution
is also `executing`, and letting `/execute` resume it too would let a
concurrent call race an actively-running execution instead of only a
confirmed-stuck one (see the row-lock double-execute guard above). An
operator calls `/resume` only after confirming the run has made no progress
for an implausible amount of time.

### The live-payment 409 names the invoices

`uq_payments_one_live_per_invoice` is the DB-level backstop that stops an
invoice from carrying two live payments. When a run trips it the operator got
"One or more invoices already have a live payment scheduled." — which
identifies nothing; on a forty-invoice Friday run, bisecting the selection by
hand was the only way forward. The 409 now names the offending invoice
**numbers** (the identifier the row was selected by, and PII-free) and says what
to do about them.

Naming them requires the session to still be usable after the `IntegrityError`,
so `create_payment_run_for_invoices` now always wraps its inserts in a savepoint
— it used to do that only on the copilot's `plan_id` path, and on the manual
path the poisoned session was precisely why the message could not say more.

### Credit memos are netted on BOTH money paths

Applying a credit memo is the whole point of the feature: it must reduce what
the vendor is actually paid. The payment-run builder has netted applied memos
off each payment since that fix landed — but `POST /api/payments`, the
standalone path, did not. It bound the payment to a bare `invoice.amount` and
422'd any other figure, so a credited invoice paid the vendor the **full
pre-credit amount** there, and a caller who knew the correct net figure could
not even submit it. Every guard around *applying* a memo (vendor match,
currency match, no over-application) was solid, and none of it mattered on that
path.

Both paths now call `services/payment_runs.net_payable_amount(db, invoice)` —
`invoice.amount` minus the sum of its `applied` credit memos — so they can't
disagree about what an invoice is worth. Consequences on the standalone
endpoint:

- `PaymentCreate.amount` is now **optional**. When supplied it is only a
  cross-check against the net figure; the server never trusts it as the amount
  (that guard is what stops a $99,999 payment against a $500 invoice).
- The CFO-approval threshold compares the **net** amount — the money that
  actually moves — exactly as `create_payment_run` compares its netted total.
- A fully-credited invoice is refused with **409**: nothing is owed, and a
  zero-amount payment row would be a money record for money that never moves.

`credit_memos.py`'s own over-application guard (apply refuses a memo that would
exceed the invoice's remaining creditable balance) is what guarantees the net
can never go negative. Pinned by
`tests/test_payment_create_credit_memo_netting.py` (standalone) and
`tests/test_payment_run_credit_memo_netting.py` (runs).

### The invoice's payability is re-checked before the adapter call

Booking a run does not freeze the invoice. `POST /api/invoices/{id}/send-to-erp`
happily walks an invoice that already holds a `pending` run payment
`approved → sending_to_erp → sent_to_erp`, and the state machine only lets
`sent_to_erp` advance to `posted_in_erp` / `done` — `payment_scheduled` is
**not** a legal successor.

The dispatch leg used to discover that at the `transition_invoice` call, which
sits *after* `adapter.create_payment` returned and `provider_payment_id` was
assigned. `validate_transition`'s 409 unwound into `_dispatch_run_payments`'
generic `except`, which recorded `failed` / `unexpected_error:HTTPException` on
a payment the processor had already accepted — and nothing ever corrected it:
`classify_payment_failure` correctly reads the populated `provider_payment_id`
as `IN_DOUBT` (so `/retry-failed` refuses), the webhook won't advance an
already-terminal payment, and the reconciler only polls
`submitted`/`processing`. The money moved and no surface said so.

`_execute_single_payment` now refuses a non-payable invoice **before** the
adapter is called — beside the credit-memo `net_amount_changed` guard, and
retry-safe for the same reason (no order exists at the processor):

- `failure_reason` is `invoice_not_payable:<status>`, a named refusal rather
  than an `unexpected_error:*`.
- The prefix is in `_RETRY_SAFE_FAILURE_PREFIXES`, so `/retry-failed` may
  re-attempt it — and its own payability gate keeps skipping the row (as
  `invoice_not_payable`) until the ERP push completes and the invoice reaches
  the payable `posted_in_erp`.
- `api/payments.SCHEDULABLE_INVOICE_STATUSES` — the statuses the three dispatch
  legs transition to `payment_scheduled` — is now **derived** from
  `workflow_engine.VALID_TRANSITIONS` rather than restated as a literal, so it
  can never again name a status the state machine refuses. It is
  `PAYABLE_INVOICE_STATUSES` minus `payment_scheduled` (already there).

Pinned by `tests/test_payment_run_invoice_payability.py`.

### Why a payment failed, and retrying it

`Payment.failure_reason` is written on every failure path — compliance refusal,
card-issuance failure, an adapter raising, a void, a webhook-reported failure —
but for a long time it never left the database, and the partial-failure counts
existed only in the transient response body of the `/execute` call that
produced them. Reload the page and a `partial` run was a bare status word with
no way to ask "which ones, and why?" except reading the server log.

Now:

- `PaymentResponse` and the run-detail payments carry `failure_reason`,
  `provider`, `submitted_at`, `completed_at`.
- The run detail and the runs list carry the per-outcome rollup
  (`payments_completed` / `payments_failed` / `payments_in_flight` /
  `payments_pending`), **derived on read** from the child `Payment` rows — no
  stored running total, so it can't drift from the payments it summarises. (The
  runs list computes all of them in one grouped query; it used to issue a count
  per run.)
- `POST /runs/{id}/retry-failed` re-attempts them.

**`/retry-failed` in one paragraph.** Accepted only on a `partial` / `failed`
run (`draft` → use `/execute`; `executing` → use `/resume`). Same
`payment.execute` permission, the same maker-checker `check_run_segregation`,
and the same CFO-threshold gate as `/execute` — skipping any of those would
make retry a way around all three. It books a second attempt for the failed
payments it judges safe, then re-drives the run through the same
`_dispatch_run_payments` loop, so anything already `completed` / `submitted` /
`processing` / `pending_compliance` is never re-sent to the processor.

#### A retry books a NEW payment; it never re-arms the old one

`Payment.correlation_id` is the **processor's idempotency key**, not a local
trace id: `payment_adapters/base.py` says so, `column` / `dwolla` /
`stripe_treasury` / `increase` send it as `Idempotency-Key`, `modern_treasury`
as `idempotency_key=`, and `checkeeper` burns it into a 48-hour Redis `SET NX`
slot explicitly so a retry can't print a second physical cheque.

A re-attempt is genuinely a new order, so it needs a new key. Minting one onto
the *failed row* — which is what this endpoint originally did — also meant
clearing that row's `failure_reason`, `provider_payment_id`, `submitted_at` and
`completed_at`: it destroyed the only handles anyone had for reconciling
attempt #1 with the processor, and overwrote two regulated money timestamps.

So attempt #1 is never written to at all. Attempt #2 is an **INSERT** on the
same run carrying `retry_of_payment_id` (migration `0080`, tenant-scoped),
which the `payments` rollups use to count the **latest attempt per invoice**
(`services/payment_runs.active_run_payments`) rather than every row ever — all
three call sites (`_dispatch_run_payments`, the run detail, the runs list) go
through it, so a fully recovered run reports `completed` instead of `partial`
forever. `failed` / `cancelled` sit outside `uq_payments_one_live_per_invoice`,
which is what lets the second row claim the invoice's live-payment slot; a
savepoint per insert is the backstop for a conflict arriving as a race. The
superseded attempt stays visible on the run detail — an operator has to be able
to see that an invoice took two goes — it just isn't counted twice.

#### Only a failure we can prove never reached the processor is re-attempted

`services/payment_runs.classify_payment_failure` (pure, unit-tested) is the
gate. A failed payment is **in doubt** — and skipped as `needs_reconciliation`
for a human to void or reconcile — when any of these hold:

| Signal | Why it's in doubt |
|--------|-------------------|
| `provider_payment_id` populated | An order exists at the processor; every adapter here returns a handle only from a create call that succeeded. |
| `unexpected_error:*` | The dispatcher swallowed an exception. A read timeout *after* the processor accepted looks identical to one before it. |
| `*_transport_error:*` | The request may well have been received and actioned before the connection died. |
| `*_api_error:*` | The provider answered — a 5xx can still have created the order. |
| `reconciler_max_age_exceeded*` | A genuinely `submitted` payment, real money in flight, that `payment_reconciler` gave up waiting on. **This is the case that made the old re-arm a double-pay.** |
| `checkeeper_duplicate_suppressed` | The 48-hour print slot was already claimed — a cheque for this order was very likely already printed. |
| `adapter_error:*` (card leg) | The card provider may have minted a card we never recorded. |
| blank / unrecognised reason | Fail-closed: a future adapter or a legacy row is not waved through. |

Everything else — our own `compliance_refusal:` / `compliance_dismissed` /
`international_payment_error:`, the card leg's `card_issuance_*` /
`cards_not_enabled`, and each adapter's pre-flight refusals (`*_not_configured`,
`*_no_counterparty`, `*_no_external_account`,
`*_no_destination_funding_source`, `*_missing_mailing_address`,
`*_idempotency_unavailable`, `method '…' is not supported by …`) — provably
never left this process, so re-sending is safe.

**Adapter authors:** a *pre-flight* refusal must use one of those codes, not
free prose, or it classifies as unrecognised and can never be auto-retried.
Modern Treasury's two pre-flight refusals were prose and were normalised for
exactly this reason.

#### The other skip reasons

Every skip is reported back in `skip_reasons`; nothing skipped is mutated or
re-sent.

- `invoice_not_payable` — the invoice is voided, re-rejected or already `done`,
  so paying it would move money against something nobody currently approves.
- `invoice_has_blocking_exception` — an unresolved
  `PAYMENT_BLOCKING_EXCEPTION_TYPES` flag (`duplicate` / `fraud_flag` /
  `line_total_mismatch`). Run creation refuses these outright; this endpoint
  re-dispatches money days or weeks later, so a `fraud_flag` raised in the
  interim (a BEC bank-detail swap, an altered or never-issued cheque off a
  Positive Pay return) has to stop the re-send here too. Both callers share
  `services/payment_runs.blocked_invoice_ids` so they can't drift.
- `net_amount_changed` — a credit memo applied while the payment sat `failed`
  (`credit_memos.py` gates on neither invoice status nor an existing payment)
  means the failed row's `amount` is no longer what the vendor is owed. The
  retry re-derives `net_payable_amount` and **skips**; the amount is never
  silently adjusted, so the operator builds a fresh run through the full gate
  set.
- `invoice_has_live_payment` — the invoice has since acquired another live
  payment.
- `invoice_has_live_card` — a virtual card was minted against the invoice
  (`POST /api/cards/generate`) while this payment sat `failed`, so the card is
  already paying it on a rail this retry isn't using. Shared with the run
  builder and the standalone endpoint via
  `services/payment_runs.card_claimed_invoice_ids`; a `virtual_card` retry is
  exempt because that rail converges on the existing card. See § A live card is
  a claim on its invoice.

`retryable_failures` on the run detail counts only failures the endpoint will
actually re-attempt, so the retry button can never offer an action that could
only be skipped.

**Idempotency** comes from the run claim: the row is locked and flipped to
`executing` before anything is booked, so a double-click blocks on the lock and
then 409s against the non-retryable status.

Every retry writes a `payment.retried` audit row naming **both** payment ids
(the superseded attempt and its successor) plus the failure reason it replaced,
and one `payment_run.retried` row for the batch. Pinned by
`tests/test_payment_run_retry.py`.

### The `virtual_card` leg — converging on an invoice's existing card

A `virtual_card` payment skips the payment adapter and mints a card instead
(`_execute_single_payment` → `services/card_issuance`). Because an invoice can
hold at most one LIVE card (`uq_virtual_cards_one_live_per_invoice`), the leg
has to cope with that card already existing — it may have been minted by `POST
/api/cards/generate`, or by a concurrent payment run. Both are reachable, so
the leg mirrors the batch endpoint's two-layer handling:

1. **Pre-check** — `find_live_card_for_invoice` before the provider call. An
   invoice that already holds a live card never reaches the adapter, so no
   second spendable card is minted and then orphaned by the index rejecting its
   row.
2. **Savepoint** — the insert goes through `persist_card`, which flushes inside
   a `begin_nested()` block. A racer that claimed the slot between the
   pre-check and the flush trips the index; the savepoint contains it so the
   dispatch loop's audit row and per-payment commit still succeed. Without it
   the `IntegrityError` left the whole session needing a rollback, so the
   loop's very next statement raised `PendingRollbackError`, the run never
   rolled up, and it stayed `executing` — with `/resume` re-driving the same
   payment into the same crash.

The payment then settles against whichever card is live — but **only if that
card can actually be what settled it** (`card_issuance.card_settlement_block`).
Converging marks the payment `completed`, i.e. asserts money moved, so the
assertion has to be true. Two honest failure states rather than a misleading
`completed`:

| `failure_reason` | When |
|---|---|
| `card_already_charged` | the occupying card is `charged`/`completed` — its funds already moved, under a *different* payment |
| `card_already_issued_insufficient_limit` | the card is live and unspent but its `amount_limit` is below this payment's amount |
| `card_issuance_conflict` | the contended live-card slot was empty on re-read (the winner cancelled its card in between) — surfaced for AP rather than silently re-calling the provider |

The spent-card case is **not** a race. `amount_limit` is the authorization
ceiling and is never reduced by spend (a charge only sets `amount_charged`), so
a limit-only check would happily settle against a redeemed card. The reachable
flow is: mint → the vendor redeems it (webhook → `charged`) → AP voids that
payment → the invoice returns to the payable pool (`approved` is in
`PAYABLE_INVOICE_STATUSES`) → the next run finds the same spent card.

When convergence *is* valid, the payment claims the card's `payment_id` if
nothing else owns it (a card from `POST /api/cards/generate` carries none) —
`list_payments` resolves a row's card via `VirtualCard.payment_id ==
Payment.id`, so without that link the UI shows no card on a payment whose
reference reads `CARD-…`. A card already naming another payment is never
re-pointed; that payment is live and the link is its badge.

Both outcomes write a PII-free audit row against the card (ids, last four,
exact amount as a string — never a PAN): `card.generated` on a fresh mint
(matching the batch endpoint, so a card-lifecycle query shows a creation event
on both mint paths) and `card.reused` when the payment settled against a card
it did not mint. A converged payment does **not** re-email the vendor — the
reveal link was already sent when the card was minted.

#### A live card is a claim on its invoice — no other rail may also pay it

Convergence covers the case where the *second* payment is itself a card. It
cannot cover an ACH or wire, because those rails never consult the card at all.

`POST /api/cards/generate` mints a spendable card for the full invoice amount
and — unlike the leg above — books **no** `Payment` row and leaves the invoice
`approved`. Every "is this invoice already being paid" gate keys on `Payment`:
`uq_payments_one_live_per_invoice` and its `_live_payment_invoice_numbers`
pre-check count payment rows, and `/payments/queue` excludes an invoice only
once it has a `completed` payment. So a directly-minted card left the invoice
fully payable by ACH — the vendor held a card for the face amount **and**
received a wire, with nothing in either audit trail contradicting the other.

`payment_runs.card_claimed_invoice_ids` (over
`card_issuance.live_card_invoice_ids`, which reuses the index's own
`status <> 'cancelled'` predicate) is the gate, run by all three paths that
book money — the run builder, the standalone `POST /api/payments`, and
`/retry-failed` (skip reason `invoice_has_live_card`), so the refusal can't be
walked around by using a different one.

It is **method-aware**: `virtual_card` is exempt, because that rail converges on
the existing card rather than opening a second outflow. Every other rail is
refused, including a payment with no `method` recorded (it will not take the
card leg either — fail closed). A `charged` card is the case that matters most:
the money already moved on that rail, and an ACH run would move it again.

The exit is cancelling the card, which vacates the slot the index and this gate
share.

### Voiding a card payment cancels the card

`POST /payments/{id}/void` on a `virtual_card` payment also closes the card at
the provider (`_cancel_card_for_void` → `card_issuance.cancel_card_at_provider`).
Without that, the void only moved our books: the card stayed live and
bearer-spendable with no payment behind it, and — still occupying the invoice's
live-card slot — it was rediscovered by the next payment run.

Provider-**first**, mirroring `POST /api/cards/{id}/cancel`: the row flips to
`cancelled` (plus a `card.cancelled` audit row tagged `via: payment_void`) only
once the provider confirms the close. Best-effort like the payment rail — a
card-provider outage records the outcome instead of blocking the accounting
void. The outcome lands on the `payment.voided` audit row as `card_outcome`:

| `card_outcome` | Meaning |
|---|---|
| `card_cancelled` | closed at the provider and in our DB |
| `card_already_charged` | already spent — the provider cannot un-spend it; AP must chase the refund. `card_settlement_block` is what then stops a later run settling against it |
| `card_already_cancelled` / `no_card_linked` | nothing to do |
| `cards_not_configured` / `card_provider_not_configured` / `card_cancel_rejected` / `card_cancel_error:<Type>` | the provider could not confirm; the card is left live (unverified cancels are never recorded) |

`virtual_cards.payment_id` is **not** unique — a cancel-then-reissue leaves the
dead row pointing at the same payment — so the lookup orders live-first (at most
one non-`cancelled` card can exist per invoice, per
`uq_virtual_cards_one_live_per_invoice`) then newest. An unordered `LIMIT 1`
could otherwise hand back the cancelled row and report `card_already_cancelled`
while the spendable card stayed open, which is the exact failure this leg
exists to prevent. The row is also taken `FOR UPDATE`, which is what makes the
retry below idempotent under concurrency.

#### The outcome is on the response, and so is the remedy

Both figures ride `PaymentResponse`: `void_card_outcome` (the tag above) and
`void_card_disposition` — the **verdict**
(`card_issuance.card_cancel_disposition`), which is what the UI branches on:

| `void_card_disposition` | Meaning | UI |
|---|---|---|
| `closed` | dead at the provider | nothing to report; the dialog closes |
| `no_card` | this payment issued no card | nothing to report |
| `not_closed_final` | already **charged** — it can never be closed | shown, with **no** retry (one could only fail forever) |
| `not_closed_retryable` | still LIVE and bearer-spendable | shown, with a retry |
| `null` | the leg never ran (not a card payment, or not a void read) | nothing to report |

A client must never enumerate the outcome *tags*: it mis-reads every tag added
later, and it fails in the dangerous direction — rendering a live card as shut.
`card_cancel_disposition` classifies an unrecognised tag as
`not_closed_retryable` for the same reason.

**`POST /payments/{payment_id}/void/retry-card-cancel`** is the remedy for a
`not_closed_retryable` card. It re-runs `_cancel_card_for_void`'s exact
provider-first path (`via: payment_void_retry` on the `card.cancelled` row) and:

- **409s on anything but an already-`voided` `virtual_card` payment.** That is
  what keeps it from becoming a second way to close a card: `POST
  /api/cards/{id}/cancel` is reachable on a LIVE payment, where it would kill
  the card while the payment and its invoice still claim money is in flight
  (`docs/decisions.md` §96, §132). This one can only ever *finish* a reversal.
- **Gates on `payment.void`** — the permission of the void it completes, not the
  card router's bare `require_roles(ADMIN, AP_MANAGER, CFO)`, so an org that
  split the duties keeps the reversal's other half behind the same gate.
- **Is idempotent by construction.** The card row is locked `FOR UPDATE`; an
  already-`cancelled` row short-circuits to `card_already_cancelled` with no
  provider call and no second `card.cancelled` row, and every card adapter
  treats an already-closed card at the provider as a confirmed cancel. A second
  retry is a no-op, not an error.
- **Moves no money and re-touches nothing else.** The payment stays `voided`,
  the invoice is not transitioned, and the payment rail is not re-asked — so
  `void_adapter_outcome` is `null` on this response, because this call did not
  ask it.
- **Records the attempt either way.** Every call writes a PII-free
  `payment.void_card_cancel_retried` audit row carrying the outcome and its
  disposition — an operator needs to see that a retry happened and what the
  provider said, not only the retries that changed something.

The `/payments` History tab's void dialog renders this: on a non-closed verdict
it stays open, states that the card is still live (or that it was already
charged and cannot be closed), shows the labelled outcome — falling back to the
raw tag, which is PII-free by construction — and offers **Retry closing the
card** only where a retry can work.

### Sanctions / compliance hold resolution

`_execute_single_payment` parks a payment at `status="pending_compliance"`
in two cases: the invoice has no screenable vendor at all, or
`services/compliance.check_payment_compliance` itself returns a `hold`
verdict (sanctions/KYC review required). Both cases open a
`payment_compliance_hold` Exception (`exception_type`, severity `error`)
scoped to the invoice — deduplicated on `(invoice_id,
"payment_compliance_hold", "open")` so a retried/resumed execution never
opens a second one for the same hold. This is what makes the hold visible
in the normal exceptions queue instead of only in the payment's own
`failure_reason` field.

Two endpoints resolve it. Neither requires the invoice to actually own an
open `payment_compliance_hold` exception — resolving it is best-effort/a
no-op if none exists — the payment's own `pending_compliance` status is
the real gate:

- **`POST /payments/{id}/compliance/release`** (`payment.execute`) —
  re-runs the exact same compliance-then-adapter path
  (`_execute_single_payment`) that produced the hold. This is deliberate:
  it is a retry of the real gate, not a bypass. If the underlying problem
  is unresolved (e.g. the vendor is still unlinked, or screening still
  returns `hold`), the payment lands right back on `pending_compliance`
  with a fresh exception opened, rather than being forced through. AP's
  fix — attaching a real vendor, correcting sanctions data, etc. — happens
  out-of-band before calling this.
- **`POST /payments/{id}/compliance/dismiss`** (`payment.void`) — gives up
  without ever reaching the adapter: flips the payment straight to
  `failed` with `failure_reason` set from the required `{reason}` body.
  Use when the payment genuinely should not go out (e.g. the vendor turned
  out to be sanctioned, or is confirmed defunct).

Both are row-locked (`SELECT ... FOR UPDATE`), 409 on any payment not
currently `pending_compliance`, and resolve the invoice's open
`payment_compliance_hold` exception (`resolution` = `"released"` or
`"dismissed: <reason>"`) on success.

**A release that settles hands off to the ERP sync**, exactly as the end of
`/execute` does. `services/payment_erp_sync` is the only path that flips an
invoice `payment_scheduled → paid` and nothing re-invokes it for an
already-`completed` payment, so a release on a rail that confirms
synchronously (the virtual-card leg always does; so does any adapter returning
`completed`) used to move the money and strand the invoice at
`payment_scheduled` forever — under-counting the aging report, the
`/dashboard` pipeline, the vendor's payment history and the 1099 YTD totals
while the payment row itself looked correct. A release that lands
`submitted` / `processing` is deliberately not dispatched: its own webhook
does that once the rail confirms. `dismiss` never dispatches — nothing
settled.

**A raising adapter on release is recorded, not 500ed.** `/release` wraps
`_execute_single_payment` the way `_dispatch_run_payments` does — a live FX /
sanctions / processor adapter can raise anything, and unguarded the exception
unwound the request: FastAPI 500ed, the session rolled back, and the payment
reverted to `pending_compliance` with no `provider_payment_id` recorded even if
the processor had already accepted the order (on the card leg, a rollback after
`persist_card` discarded the `VirtualCard` row while a real spendable card
existed at the provider). It is now marked `failed` with
`unexpected_error:<ExceptionClass>` — the class only, never the adapter's
message, which can embed a partial account number or PAN.

### An invoice-less payment never reaches the processor

Every gate in `_execute_single_payment` — the credit-memo net re-check, the FX
rate lock and the **entire** sanctions/KYC compliance gate — is written
`if invoice is not None`. A payment whose invoice could not be resolved
therefore used to fall straight through to `adapter.create_payment` with an
empty `invoice_number` and `vendor_name`: money to a payee nobody screened, at
a rate nobody locked, for an amount nobody re-verified — the exact inverse of
the two "no screenable vendor → hold, never pay unscreened" branches directly
below it. It now fails closed with `invoice_missing` before the adapter is
called. Unreachable in normal operation (deleting an invoice cascades its
payments); the branch exists so the fall-through can't be re-introduced.

### Voiding preserves the settlement timestamp

`completed_at` is the regulated timestamp for **when the money moved**.
`/void` used to stamp the void instant onto it, destroying the settlement time
on a `completed` payment — and the audit row recorded `previous_status` but not
the previous timestamp, so it was unrecoverable. `/retry-failed` refuses to
overwrite the same field and says why. The void now leaves `completed_at`
alone whenever it is already set, records the void instant as
`details.voided_at` on the append-only `payment.voided` audit row (alongside
`details.settled_at`), and still stamps a terminal timestamp for a payment that
never settled (`pending` / `submitted` / `processing` / `pending_compliance`),
which has no settlement time to protect.

**Tests:** `tests/test_payment_terminal_timestamps.py`.

### Payment processor adapters

The actual money movement is handled by an adapter pattern in `backend/app/services/payment_adapters/` — same shape as ERP, extraction, and card adapters. Each adapter implements:

```python
class PaymentAdapter:
    async def create_payment(payload: PaymentPayload) -> PaymentResult: ...
    async def get_payment_status(provider_payment_id: str) -> PaymentStatus: ...
    def parse_webhook(headers: dict, body: bytes) -> WebhookEvent | None: ...
    async def test_connection() -> bool: ...
```

Registered providers:

| Provider | Methods | Use case |
|---|---|---|
| `mock` | ach, bacs, chaps, check, faster_payments, international_ach, international_wire, rtp, sepa, virtual_card, wire | Local dev — settles instantly with deterministic fake references. Default when `Organization.settings.payments` is empty. |
| `modern_treasury` | ach, wire, rtp, check | Production. Real bank rails via Modern Treasury's REST API. Idempotent on `correlation_id`. |
| `stripe_treasury` | ach, wire | Production. Stripe Treasury for orgs already on Stripe; settles via Treasury FinancialAccount. |
| `increase` | ach, wire, check | Production. Increase API; same correlation-id idempotency story. |
| `column` | ach, wire | Production. Column.com bank-as-a-service. |
| `dwolla` | ach | Production. ACH-only. Use when the org doesn't want a full Treasury account. |
| `checkeeper` | check | Production. Outsourced check printing + mailing. Pairs with one of the ACH/wire adapters. |

Per-org config lives at `Organization.settings.payments`:

```json
{
  "provider": "modern_treasury" | "mock",
  "org_id": "org_...",                // Modern Treasury org ID
  "api_key": "...",
  "originating_account_id": "internal_account_...",
  "webhook_secret": "..."             // HMAC-SHA256 secret for signature verification
}
```

Only `provider` is read by the dispatcher; the rest are per-adapter, and the
block above is Modern Treasury's set. **`program_type` and `sandbox` are not
read by any payment adapter** — they belong to the card and extraction
families. Both appeared here until 2026-09-17 and had propagated into
`docs/founder-runbooks/payment-rails-onboarding.md` as live settings. Modern
Treasury selects sandbox by credential set, not by flag.

`tests/test_payment_adapter_doc_drift.py` pins the Methods column of the table
above to each adapter's `supported_methods`, so a rail added or dropped in code
fails until this table follows.

#### Lifecycle

```
[execute payment run]                       [webhook arrives]
        │                                          │
        ▼                                          ▼
adapter.create_payment(payload)            adapter.parse_webhook(headers, body)
        │                                          │
        ▼                                          ▼
 PaymentResult{status, provider_payment_id}    WebhookEvent{provider_payment_id,
        │                                                  status, amount, currency}
        ▼                                          │
 Payment row gets:                                 ▼
   provider, provider_payment_id,            Payment row gets:
   reference, status,                          status (only if not already terminal),
   submitted_at                                reference, failure_reason,
                                               completed_at (on terminal)
                                                   │
                                                   ▼
                                             verify_settlement(...) — see below
```

The orchestrator never auto-completes — only the adapter response (mock) or a webhook (real processor) flips a payment to `completed`. This prevents the platform from claiming money has moved when it hasn't.

#### Settlement-amount verification

A verified HMAC authenticates the **sender**, not the **content**. A signed
`completed` event proves the processor is talking to us about a payment we
know; it does not prove the processor moved the amount on the instruction.
Until this check existed, the handler took the status at face value — it
stamped the regulated `completed_at`, captured any accepted early-pay discount
off *our* authorized number, and handed the run to the ERP sync, with no
comparison against what AP authorized. A wire that left at $50,000 against a
$5,000 instruction, a partial settlement, or a mis-mapped provider integration
all reconciled clean, and the only net was a bank statement someone had to
remember to upload days later.

Identity is not reconciliation. The two reconcilers further downstream already
make exactly this call:

| Where | Signal | Divergent amount becomes |
|---|---|---|
| `positive_pay.classify_presented_items` | cheque found by number | `amount_mismatch` — an ALTERED cheque |
| `bank_reconciliation.classify_discrepancy` | bank line carrying our own trace reference | `amount_mismatch` — linked, excluded from `matched_count` |
| **`payment_settlement.verify_settlement`** | **processor webhook** | **`amount_mismatch` / `currency_mismatch` → `fraud_flag`** |

`services/payment_settlement.py` is pure (no DB, no clock, no I/O) and runs on
every `completed` event. `failed` / `cancelled` events are not verified — no
money moved, so whatever figure they echo reconciles against nothing.

**Two authorized legs, not one.** A cross-currency payment debits
`Payment.source_amount` in the org's home currency and credits `Payment.amount`
in the invoice's currency, and different processors report different sides.
Reporting *either* leg is a match; a third number is not. The target leg's
currency is the invoice's (the `Payment` row doesn't carry one), so the handler
loads the invoice once and reuses it for the discount capture.

| Outcome | When | What the handler does |
|---|---|---|
| `matched` | within one cent of an authorized leg whose currency is compatible | unchanged: capture the discount, hand the run to the ERP sync |
| `amount_mismatch` | a currency-compatible leg exists, none within tolerance | open a `fraud_flag`, skip the discount capture |
| `currency_mismatch` | the reported currency matches no authorized leg | open a `fraud_flag`, skip the discount capture |
| `unverified` | the provider's webhook carried no amount, **or carried one that is not a usable number** | unchanged, but the blind spot is recorded on the audit row |

Tolerance is one cent — the same band `positive_pay.DEFAULT_AMOUNT_TOLERANCE`
and `bank_reconciliation.AMOUNT_MATCH_TOLERANCE` use. Three reconcilers
disagreeing about what "the same amount" means is how a discrepancy hides in
the gap. `variance` is signed and 2 dp: **positive means the processor moved
MORE than we authorized**, matching `bank_reconciliation.match_variance`.

**What a discrepancy does, and deliberately does not, do.**

- The payment still records as `completed` with its `completed_at`. Money
  moved; refusing to record that does not un-move it, and a payment silently
  parked in `submitted` forever is strictly worse.
- The verdict rides the **same append-only audit row** that records the money
  moving (`details.settlement`), on every completion — matched, mismatched and
  unverified alike. That row is WORM-shipped; the exception row is mutable and
  gets resolved.
- A payment-blocking **`fraud_flag`** opens on the invoice. Deliberately not a
  new exception type: this is the electronic equivalent of the ALTERED cheque
  `api/positive_pay.py` already flags, and `fraud_flag` is in
  `PAYMENT_BLOCKING_EXCEPTION_TYPES`, so the invoice can't be swept into
  another payment run until a human clears it — which is what matters the
  moment this payment is voided and the invoice returns to `approved`. Deduped
  on `(invoice_id, fraud_flag, open|escalated)`, the same rule Positive Pay's
  own return processing uses.
- The **discount capture is skipped**.
  `discount_capture.capture_offers_for_settled_payment` matches an accepted
  offer's discounted payoff against `payment.amount` — our authorized figure,
  which the rail has just contradicted — so capturing would permanently mark
  savings realized on a number that is in dispute and misreport them to the CFO.
- The **ERP sync still runs**, and the invoice still transitions to `paid`.
  `positive_pay` and `bank_reconciliation` both record-and-flag without
  rewriting the settlement, and this matches them. Suppressing the dispatch at
  the webhook would also be leaky — any sibling payment's webhook
  re-dispatches the sync for the whole run.

##### An under-settlement holds the invoice short of `paid`

`Payment` records both figures: `amount` is what AP **authorized**,
`settled_amount` / `settled_currency` (migration `0083`) is what the rail says
it **moved**. That distinction is what lets the ERP sync answer a question it
previously couldn't — *may this invoice be marked paid?* — separately from the
verifier's *did the rail report what we authorized?*.

`payment_settlement.settlement_coverage` is the pure classifier:

| State | When | Effect |
|---|---|---|
| `covered` | some authorized leg is satisfied, **or** nothing was ever reported | invoice proceeds to `paid` |
| `short` | a figure was reported and falls below every currency-compatible leg by more than a cent | invoice held at `payment_scheduled` |
| `uncertain` | reported in a currency matching no authorized leg, **or** reported a figure the column cannot hold | invoice held at `payment_scheduled` |

Over-settlement is `covered`: the vendor is not short, so the payable is
discharged even though the verifier still raises `amount_mismatch` on the same
numbers. And a NULL `settled_amount` is `covered` — it means an amount-free
rail or a row predating `0083`, and treating "we don't know" as a shortfall
would hold every invoice those rails settle. Absence is not evidence, exactly
as the verifier treats a missing amount as `unverified`.

##### A figure the column cannot hold

`settled_amount` is `NUMERIC(15, 2)` — 13 integer digits. A processor reporting
more than that used to parse (`payment_adapters.parse_amount` guards only
against values so large that `quantize` itself raises), verify, and then raise
`NumericValueOutOfRangeError` at the flush. That took the **whole webhook
transaction** with it: the `fraud_flag` the verdict had already decided on was
rolled back, the payment's completion was never recorded, the handler 5xx'd,
and the processor retried into the identical failure. The single most
suspicious settlement a rail can report was the one nothing was recorded about.

##### A figure that is not a number at all

`json.loads` accepts `NaN` and `Infinity` by default, so an adapter parsing a
hostile or broken payload really can hand the verifier one — as can a rail
reporting a magnitude past the decimal context. Those reach the same
`unverified` OUTCOME as an absent figure (neither is evidence of a discrepancy,
and inventing a `fraud_flag` out of garbage would flag every payment on a
broken integration), but under their own reason,
`provider_reported_unusable_amount`, because the two diverge downstream.

The NaN-aware guard runs **first**. It used to run last: the tolerance
comparison quantized the reported figure before anything NaN-aware saw it, so
`InvalidOperation` escaped out of a pure function on the money path — the
webhook 5xx'd and the processor retried into the identical failure for as long
as it kept retrying, while the payment stayed in flight. A retry storm rather
than a wrong figure, but the recording layer one call later
(`persistable_settled_amount` → `fits_numeric`) was already written to
quarantine exactly these values.

So the unusable figure is carried onto the verdict, recorded as
`settled_amount_unstorable`, and coverage returns `uncertain` — the invoice
**holds**, behind the same two exits a shortfall has. It is never laundered
into the fails-open NULL that means "no rail ever reported a figure".

`payment_settlement.persistable_settled_amount` is now the one splitter both
writers use (the webhook and the reconciler backstop, so they cannot disagree
about what is storable). An over-range figure leaves `settled_amount` NULL and
sets `settled_amount_unstorable` (migration `0085`) instead.

**Why a flag and not a NULL, and not a wider column.** NULL already means "no
rail ever reported a figure" and deliberately fails OPEN; collapsing a garbage
report into that would mark the invoice paid on the strength of a number we
know is wrong. Widening the column just moves the cliff — no legitimate
settlement is 14 integer digits, so a value that doesn't fit is a corrupt or
hostile report, not a big payment. The flag makes coverage return `uncertain`,
so the invoice holds behind the same two exits a shortfall has.

The figure itself is not lost: `SettlementVerification.as_details` writes it as
an exact decimal string onto the append-only `audit_log` row, whose `details`
is JSONB and has no range limit. **The column carries the decision input; the
audit row carries the evidence.**

**A held invoice has two exits**, and having them is what makes the hold safe:

| Exit | Endpoint | Result |
|---|---|---|
| Accept the shortfall as final | `POST /api/payments/{id}/settlement/accept` (`payment.execute`, requires a `reason`) | invoice → `paid`, reason + figures on the append-only trail |
| Reject the settlement | `POST /api/payments/{id}/void` (`payment.void`) | invoice → `approved`, re-payable at the right amount |

**Both exits are reachable in the app**, not only over the API. `/payments` →
History renders what the rail settled beside what AP authorized (a `Settled …`
sub-line under the amount — the hold was previously invisible), and offers an
**Accept settlement** row action gated on `payment.execute`. Its confirm dialog
shows both figures, requires the `reason`, states that accepting declares the
shortfall final and releases the invoice to `paid`, and — because only the
server can decide coverage — reads the invoice's own status first so an invoice
that is no longer held gets an explanation rather than a confirm that can only
409. Void is the existing row action beside it.

`accept` refuses (409) when the settlement already covers the invoice, so it
can't become a general "force to paid" lever, and when the payment never
reached `completed`. It deliberately does **not** resolve the `fraud_flag`:
unlike `payment_compliance_hold`, which only the compliance path raises,
`fraud_flag` is shared with Positive Pay's altered-cheque detection, so
clearing "the open one" could silently close an unrelated fraud finding. The
exception queue stays the separate human sign-off.

> **Why the earlier attempt was reverted.** A first version held the invoice
> keyed on the open exception rather than on a persisted figure. Because
> `payment_erp_sync._sync_payments` is the only writer of
> `payment_scheduled → paid` and nothing re-invokes it once a run's payments
> are terminal, an operator who cleared the flag — the correct response to an
> over-settlement — stranded the invoice permanently. The condition is now a
> durable fact on the row, and both exits above are tested
> (`tests/test_payment_settlement_hold.py`).

**Per-provider coverage.** `WebhookEvent.amount` is in MAJOR units. Modern
Treasury, Stripe, Increase and Column exchange minor units;
`payment_adapters.base.to_minor_units` / `minor_units_to_decimal` are exact
inverses of each other and both resolve the currency's **real ISO-4217
exponent** (2 almost everywhere; 0 for JPY/KRW/CLP…, 3 for BHD/KWD/OMR…). They
were a flat `* 100` / `/ 100` pair: symmetric, so it could never raise a
phantom mismatch, but symmetrically wrong off-cent — a JPY payment was sent at
100× the intended amount and a Gulf-dinar one at a tenth, and a genuine
scale-off on those currencies read as `matched`. Both legs moved together,
because fixing the parse side alone would have turned a symmetric error into a
real mispricing. Checkeeper and `mock` exchange major-unit decimal strings and
are unaffected.

| Provider | Field(s) read | Verified? |
|---|---|---|
| `modern_treasury` | `data.amount` (minor), `data.currency` | yes |
| `stripe_treasury` | `data.object.amount` (minor), `.currency` (lowercase) | yes |
| `increase` | `associated_object.amount` (minor), `.currency` | yes |
| `column` | `data.amount` (minor), `data.currency_code` | yes |
| `checkeeper` | `check.amount` (major string), `check.currency` | yes |
| `mock` | `amount` / `currency` when supplied | yes (local-first) |
| `dwolla` | — (envelope carries none) | yes, via `fetch_settlement` |

**`fetch_settlement` — the pull counterpart.** Dwolla's event body is a bare
`{id, topic, resourceId, _links}` envelope; the transfer's amount is only
reachable by following `_links.resource`, which the synchronous
signature-verification path must not do. And the reconciler backstop
(`services/payment_reconciler.py`) has the same gap for a different reason —
`PaymentAdapter.get_payment_status` returns a bare `PaymentStatus` by design —
so the very case it exists for (the webhook never arrived) had the least
evidence. Both settled `unverified`.

`PaymentAdapter.fetch_settlement(provider_payment_id) -> SettlementReport` is
the optional capability that closes them. It follows exactly the contract
`get_balance` established: the base implementation returns
`available=False, unavailable_reason="not_supported"`, so an adapter that never
implements it is unaffected and the verdict stays `unverified` rather than
becoming an invented one.

Two call sites, both **guarded**, because a settlement fetch must never break
the webhook that is recording money movement nor halt the sweep:

- the webhook handler, only when the event carried no amount (no redundant
  call on rails that already report it); and
- the reconciler, whenever it is the thing that settled the payment.

Any failure — no capability, transport error, unparseable body — leaves the
settlement exactly where it was. The conversion helpers likewise return `None`
(never `Decimal("0")`) for anything unparseable, so an absent figure can never
read as a total under-settlement.

**One recorder, both paths.** Fetching the missing figure, running
`verify_settlement`, persisting `settled_amount`/`settled_currency`, computing
the **realized FX gain/loss** and raising the `fraud_flag` on a discrepancy all
live in `services/payment_settlement_record.py`, and the webhook and the
reconciler call the *same* functions —
`record_completion` returns one `SettlementCompletion` carrying both the
verdict and the FX figure, and `as_audit_details()` is the single builder for
the `details` fragment both paths put on their audit row. They had drifted: the backstop persisted a figure
and stopped there — no verdict, no `details.settlement` on the audit row, no
exception. So a rail reporting a 10× overpayment settled silently (over-
settlement is `covered` by design, so `payment_erp_sync` marked the invoice
`paid`), and a short settlement stranded the invoice at `payment_scheduled`
with nothing in the queue to explain it. Because `payment_webhook` refuses an
already-terminal payment, a late webhook could never supply the missing
verdict either. The verdict now rides the reconciler's append-only audit row
exactly as it does the webhook's, on every completion.

Then they drifted again, one field over. **Realized FX gain/loss** — the
difference between the liability a foreign-currency invoice accrued at its
locked rate and the cash that actually left in the home currency — was computed
inline in the webhook handler and nowhere else, so every cross-currency payment
the backstop recovered booked none. Same population, same cause, same silence:
nothing re-derives the figure once the payment is terminal. It is now part of
`record_completion`, and `tests/test_payment_completion_record.py` is the drift
guard — a source scan asserting that
`international_payments.realized_fx_gain_loss_for_settlement` has exactly one
caller and that neither completion path calls `record_settlement` directly.

**Tests:** `tests/test_payment_settlement.py` (the verdict table + the coverage
classifier), `tests/test_payment_settlement_adapters.py` (per-provider
extraction + the minor-unit exponent round-trip),
`tests/test_payment_settlement_webhook.py` (handler behaviour),
`tests/test_payment_settlement_hold.py` (the hold and both its exits, DB-backed),
`tests/test_payment_fetch_settlement.py` (the capability + both call sites),
`tests/test_payment_completion_record.py` (realized FX on both completion
paths + the one-owner drift guard).

#### Webhook URL

Each tenant configures their processor's webhook URL to:

```
https://app.com/api/payments/webhook/{tenant_slug}/{provider}
```

Tenant is encoded in the path (no `X-Tenant-Slug` header needed — processors don't always support custom headers). The adapter verifies the signature; bad signatures, unknown events, and missing payments all return `204` silently to avoid leaking probing information.

Before any of that, the handler bounds the body against `payment_webhook_max_bytes` (default 4 MiB) — a declared `Content-Length` over the cap rejects without ever awaiting `request.body()`, and the actual read is re-checked in case the header lied or was absent (chunked transfer). The HMAC check happens inside `adapter.parse_webhook`, well after this point, so the cap is what stops an unauthenticated caller from having an arbitrarily large payload buffered fully into memory (memory-exhaustion DoS on a public route) — mirrors `erp_webhook` / `peppol_inbound` / `cards.card_webhook`.

#### Adding a new processor (Stripe Treasury, Increase, Column, …)

1. Copy `mock_adapter.py`, implement the four methods.
2. Register with `@register_payment_adapter("stripe_treasury")`.
3. Map the processor's status enum to `PaymentStatus` in your adapter (Modern Treasury's `_STATUS_MAP` is the reference).
4. Add the provider to the org-settings UI dropdown.
5. Tests: dispatcher returns the new adapter, status-map covers every documented status, webhook signature verification works.

#### Local testing with stripe-mock

The `stripe_treasury` adapter can run offline against Stripe's official API mock —
no `sk_test_` key, no network. It's opt-in under the Compose `payments` profile:

```bash
pnpm stripe:up     # stripe/stripe-mock on :12111
# backend/.env:
#   FEOH_STRIPE_API_BASE=http://localhost:12111/v1
# org settings.payments: provider=stripe_treasury, api_key=sk_test_x, financial_account_id=fa_x
pnpm stripe:down   # stop it
```

`FEOH_STRIPE_API_BASE` (empty = live Stripe) repoints the adapter's API base; a
per-config `api_base` overrides it. stripe-mock returns canned fixtures from
Stripe's OpenAPI spec — it validates request shape + response parsing
(`create_payment`, `get_payment_status`, `test_connection`), not stateful flows
or real webhooks. The seam is locked by `backend/tests/test_stripe_api_base.py`
(CI-safe, no container). For the other processors, the in-process `mock` adapter
remains the local default.

### The payments KPIs are denominated, not just summed

`GET /payments/summary` and `GET /payments/queue` are reporting surfaces — no
money moves on them — but both used to add figures in different currencies
together and label the result with nothing at all.

`Payment.amount` is denominated in the **invoice's** currency:
`international_payments.prepare_international_payment` sets
`amount=invoice.amount` and puts the home-currency debit on
`source_amount`/`source_currency`. A book with one foreign invoice therefore
made `total_paid` / `total_pending` a silent two-currency mixture. Both now
route through `currency_conversion.payment_reporting_amount_sql` — the same
resolver the 1099 report and the vendor risk score use:

1. `source_amount` when `source_currency` IS the org's reporting currency (the
   rate-locked figure that actually left the bank), else
2. `amount` when the invoice's own currency IS the reporting currency (the
   ordinary domestic case — a single-currency tenant's numbers are unchanged).

A payment neither rung can establish is **excluded** and counted on
`unconverted_payment_count`, never added at face value; a filed total is not a
place to guess. `currency` on the response says what the figures are in.

`/payments/queue` sums INVOICE amounts, so it uses the row-level
`reporting_amount_for_row` (persisted rate-locked `reporting_amount` → same
currency 1:1 → face value + `unconverted`). Here a row that can't be resolved
is still counted — dropping an invoice would understate what is due, which on a
work queue is worse than a flagged approximation — and the count rides out on
`unconverted_count`. Each item keeps its own `amount` + `currency` for display;
only the totals are converted. `total_savings` is computed off the same
rate-locked figure as the outflow, so the two totals are in one currency.

Nothing is converted at read time in either endpoint: a rate fetched on a read
makes a historical total move under the reader (`docs/decisions.md` §18).

**`total_pending` now includes `pending_compliance`.** A payment held by the
sanctions/KYC gate is authorized money still out there; omitting it put that
money in NEITHER KPI — not paid, not pending — invisible in the one place a
treasurer looks for "what is still committed".

**Tests:** `tests/test_payment_summary_currency.py` (DB-backed),
`tests/test_payment_summary.py` (shape).

**The rebate figure is denominated too, and it is the odd one out.** `total_paid`
/ `total_pending` convert through `payment_reporting_amount_sql`; the rebate
total cannot, because `card_rebates` carries no currency column at all — a
rebate's denomination is only knowable through the card it accrued on. So it
*filters* rather than converts: join `virtual_cards`, keep the rows whose
currency matches, and count the rest onto `excluded_rebate_count`. The shared
expression is `currency_conversion.card_currency_sql` — named for the card,
because card figures read it directly while rebate figures reach it through the
join — and six rollups read it: this endpoint, the dashboard KPI,
`GET /api/cards/dashboard`, `GET /api/cards/rebates`, the analytics
rebate-yield numerator, and the billing usage meter. Five of the six were bare
cross-currency `SUM`s, several under a response that declared a currency.

`excluded_rebate_count` is deliberately **not** folded into
`unconverted_payment_count`. They answer different questions: one is a payment
whose reporting-currency figure could not be ESTABLISHED (a missing rate), the
other a rebate whose currency is perfectly well known and simply is not this
one. A single combined number would describe neither, and the two have
different remedies — book the missing rate, versus nothing to fix.

### A payment states what its money is denominated in

The section above is about *rollups*. This one is about a single row, and it
was the older hole: `PaymentResponse` and `PaymentRunResponse` joined the
invoice for `vendor_name` and `invoice_number` but never carried
`invoice.currency`, so every `/payments` reader rendered a per-row figure under
the **org's default** currency — a code nobody had established.

`payments` has no currency column by design: a payment settles in its invoice's
currency (`_execute_single_payment` reads `invoice.currency` per leg), and
`payment_runs.total_amount` is one bare `Numeric` kept meaningful by
`create_payment_run_for_invoices` refusing a run whose invoices span more than
one currency. Both responses now carry the code off rows they already joined:

| Field | Source | `None` when |
|---|---|---|
| `PaymentResponse.currency` | the joined `Invoice.currency` | no invoice joined (`invoices.currency` is NOT NULL, so this is defensive) |
| `PaymentRunResponse.currency` | `api/payments._run_currencies` — one grouped query over the run's legs' invoices | the run has no payments, or a legacy run predating the single-currency guard whose legs disagree |
| `GET /runs/{id}` `currency` + per-payment `currency` | the same rows the detail already loads | as above |
| `POST /runs` `currency` | `_run_currencies` on the just-created run | as above |

**Where it cannot be PROVEN the answer is `None`, never a substituted default**
(`docs/decisions.md` §79/§82). The mixed-legacy-run case is the sharp one: that
run's `total_amount` is a sum across currencies, denominated in nothing real, so
stamping a code on it would dress a meaningless figure up as a genuine one. The
shared rule is the pure `api/payments._one_currency`, so the list, the detail
and the create response cannot disagree about it. `POST /runs`' confirmation
message also stopped hardcoding a `$` in front of the total.

**Why this mattered most in one place.** `POST /payments/{id}/settlement/accept`
exists because a rail can settle a different amount — or a different currency —
than AP authorized, and its dialog puts "Authorized" beside "Settled". The
settled half had `settled_currency` on the wire and the authorized half had
nothing, so a EUR payment rendered a fabricated `$1,200.00` directly above a
real `€1,150.00`: the two figures that screen exists to compare, on the screen
built to catch `currency_mismatch`. The `/payments` create-run **review panel**
had the same class one screen earlier, dropping the `currency` the queue row
directly above it renders.

The client renders an unprovable currency as a **bare grouped figure** — no
symbol, no code — rather than borrowing the org default
(`routes/payments/+page.svelte::formatRowMoney`, which falls back to the shared
`utils/discountRecommendation::formatAmountWithoutCurrency` — the same primitive
`/discounts` uses for its own unprovable-currency case).

**Tests:** `tests/test_payment_response_currency.py` (DB-backed, plus the pure
`_one_currency` refusal cases). The e2e assertions deliberately match the
currency **symbol**, not just the digits — `$500.00` and `€500.00` are
indistinguishable to a digits-only check, which is why the suite could not see
this: `tests-e2e/payments/recovery-exits.spec.ts` (whose fixture is EUR on
purpose, so a fixture in the tenant's own currency can't make the wrong
rendering look right) and `tests-e2e/payments/queue-multi-currency.spec.ts`.

**Still open (tracked in `docs/followups.md`):** `RunDetailModal.svelte` formats
its five money cells via `fmt(x)` with the `currency` argument omitted, which
resolves through `money.ts`'s `resolveCurrency` to `DEFAULT_CURRENCY` (USD)
regardless of the org — the same fabrication, one click away, and worse than the
list was (it ignores the tenant's default too). The backend half is done: the
`GET /runs/{id}` response this modal already consumes now carries `currency` on
the run AND on each payment, so closing it is passing those through the five
existing `fmt()` calls.

### The payment queue is paginated

`GET /api/payments/queue` used to return the tenant's **whole** approved-unpaid
invoice set on every view. It now takes `page` / `page_size` (via
`app/api/pagination.py::pagination_params`, default 20 / max 100) and returns

```
{items, total, page, page_size, selectable_total, blocked_total,
 total_amount, total_savings, currency, unconverted_count, by_currency}
```

`items` is one ordered page (`Invoice.due_date ASC NULLS LAST, Invoice.id ASC`
— the `id` tie-breaker is what stops a row hopping between pages). Every other
field describes the **whole** queue, computed in SQL by
`_payment_queue_rollup` — grouped by currency, with the reporting-amount
expressions built by `currency_conversion.invoice_reporting_amount_sql` (the
one owner of the rule, shared with the dashboard) — so a KPI or banner can't
contradict the list:

- `total` — every payable row. `selectable_total` is what "select all N
  matching" resolves (rows a run accepts on the DEFAULT rail); `blocked_total`
  is rows a run refuses on EVERY rail. **They do not add up to `total`** — a
  rail-pinned row (`required_method`) leaves the selectable set while staying
  perfectly payable, so `blocked_total` is counted in its own right rather than
  subtracted. See § The queue reports every refusal the run builder enforces.
- `by_currency` — `[{currency, count, total_amount, total_savings}]`, exact
  decimal strings, so the frontend renders honest per-currency pay-bar
  subtotals without holding every row. Never a cross-currency sum.
- `total_savings` / per-currency `total_savings` come off the same rate-locked
  figure as the outflow (INNER-joined discount aggregate restricted to rows
  with a live `discount_date` / `discount_percent`).

**A rate lock is two columns, and the rollup checks both.** The queue used to
carry its own copy of the reporting-amount `CASE`, testing the lock as
`reporting_amount IS NOT NULL AND upper(reporting_currency) = tgt`. For a row
carrying an amount but a NULL `reporting_currency` that predicate is NULL, not
FALSE — and `NOT NULL` is NULL too, so the row fell through the `unconverted`
`CASE` and was counted as **converted** while the money it contributed came
from the unlocked face `amount`. A foreign-currency total presented as fully
converted, which is precisely what `unconverted_count` exists to prevent
(`docs/decisions.md` §35). The shared builder states
`reporting_currency IS NOT NULL` explicitly and matches
`reporting_amount_for_row`, which requires both columns. Pinned by
`tests/test_payment_queue_reporting_lock.py`, whose last case fails if the
queue ever restates the rule inline again.

**The row and the rollup round the same way.** Both compute
`invoice.amount * discount_percent / 100` from the same `PaymentSchedule` row,
so they have to agree to the cent. They did not: the per-row figure used
`Decimal.quantize(Decimal("0.01"))`, which takes the decimal context's default
`ROUND_HALF_EVEN`, while the rollup's `round(numeric, 2)` is Postgres'
half-away-from-zero. A 21.00 invoice at 2.50% — exactly 0.5250 — rendered
`0.52` in the row and summed as `0.53` in the banner above it. The Python side
now passes `ROUND_HALF_UP` explicitly, matching Postgres and matching every
other money quantizer in the codebase (`international_payments._quantize_money`,
`payment_settlement._q`, `currency_conversion._quantize_money`); money here is
non-negative, so the two names describe one rule. Latent until now only because
nothing in the app writes a `PaymentSchedule` (`scripts/seed.py` is the sole
writer) — which is why it is pinned by
`tests/test_payment_queue_discount_rounding.py` rather than left as a comment.

**`GET /api/payments/queue/ids`** is the "select all N matching" resolver
(mirrors `GET /api/invoices/ids`): `{ids, total, truncated, currency,
by_currency}` for the whole **selectable** set — every row a run would accept on
the default rail, so it excludes blocked rows AND rail-pinned ones — ordered the same
way and capped at `MAX_SELECT_ALL_IDS` (5000). Same RBAC as `/queue`
(`admin` / `ap_manager` / `cfo`). The frontend `/payments` Queue tab has a
Load-More footer and a pay-bar "Select all N matching" button whose count +
per-currency totals + mixed-currency guard read this response, not the loaded
page.

**Tests:** `tests/test_payment_queue_reporting_lock.py` (the two-column rate
lock + the no-inline-copy guard),
`tests/test_payment_queue_discount_rounding.py` (the row and the rollup agree
on a half-cent), `tests/test_payment_queue_pagination.py` (DB-backed — page 1 caps,
`total` is the whole set, page 2 appends the tail with no dup/drop, `/queue/ids`
whole set + per-currency breakdown, a blocked row stays blocked on its page and
is excluded from `/queue/ids`, `ap_clerk` 403 on both);
`frontend/tests-e2e/payments/queue-pagination.spec.ts`.

### `PaymentRun.status` is derived from its payments on read

`_dispatch_run_payments`' final rollup is the only writer of the persisted
`PaymentRun.status`. Nothing else rewrites it — not the processor webhook, not
the reconciler backstop, not `/compliance/{release,dismiss}` — so a run that
rolled up `submitted` (one payment held `pending_compliance`) and then had that
payment dismissed kept reporting `status: "submitted"` while its own payments
said `failed`. `/retry-failed` gates on
`RETRYABLE_RUN_STATUSES = ("partial", "failed")` and `/execute` / `/resume` gate
on the claim states, so the run was a **dead end**: it showed
`retryable_failures: 1` and every button on it 409ed — precisely the "button
that can't act" the `retryable_failures` field exists to prevent.

`services/payment_runs.derive_run_status(persisted, rollup)` is the one rule
both the reads and the retry gate apply:

| Persisted status | Meaning | Derived |
|---|---|---|
| `draft` / `executing` / `cancelled` (`CLAIM_RUN_STATUSES`) | a CLAIM on the run, not an outcome | passed through unchanged — re-deriving would let a rollup un-claim a run mid-dispatch, and `/execute` / `/resume` gate on exactly these |
| `submitted` / `partial` / `failed` / `completed` | an outcome of its payments | recomputed from the run's ACTIVE payments (`active_run_payments` → `rollup_payment_statuses`) |

The runs list, the run detail and `/retry-failed`'s gate all route through it,
so the status an operator sees and the status the endpoint gates on cannot
diverge. `recompute_run_status` additionally **persists** the derived value at
each site that moves a payment outside the dispatch pass
(`/compliance/release`, `/compliance/dismiss`, `/void`), so a direct
`SELECT status FROM payment_runs` — an operator at `psql`, an export, a future
consumer — reads the truth too.

**The rollup itself no longer fails open.** `PaymentRunRollup.run_status`
returned `completed` whenever nothing was completed, failed or in flight — so a
run with every payment still `pending` (nothing attempted) and a run with no
payments at all both reported success without a cent moving. All-pending is
the resumable state, so it reports `executing`; no payments at all reports
`draft`. Neither claims success. The **default** is fail-closed too:
`RUN_PAYMENT_FAILED_STATUSES` includes `voided` (a human reversing a payment
after the fact — a non-success terminal, like `cancelled`), and `completed` is
returned only when EVERY active payment completed. A run of all-voided payments
reports `failed`, not `completed` with `payments_completed: 0`; a payment status
no bucket recognises (a future adapter status) reports `partial` / `failed`, not
success.

**Tests:** `tests/test_payment_run_status_derivation.py`.

### The reconciler backstop — durability and the aged-out row

`services/payment_reconciler.py` re-polls every non-terminal payment when a
webhook never arrives. Two properties of that sweep are load-bearing.

**It commits per payment, not per tenant.** Each terminal transition is locked
(`db.refresh(payment, with_for_update=True)`), written, audited and committed
before the next payment is polled — the same shape `_dispatch_run_payments`
uses, for the same two reasons. Held across the whole tenant, payment #1's row
lock spanned every subsequent `await adapter.get_payment_status(...)`, so a
webhook for that payment blocked on `payment_webhook`'s own `FOR UPDATE` for
the rest of the sweep (and a cancelled request there leaves its Redis dedup
claim unreleased, deduping the provider's retry away for the full TTL). And any
raise mid-loop discarded every terminal status, `completed_at` and audit row
the sweep had already decided for that tenant.

**Aging a payment out is a reconciliation event, not a settlement.** Past
`FEOH_PAYMENT_RECONCILE_MAX_AGE_HOURS` a still-`submitted` payment is flipped
to `failed` — real money may still be in flight at the rail; we have simply
stopped waiting. Two consequences follow, and both are handled explicitly:

- **`completed_at` is not stamped.** It is the regulated settlement timestamp,
  and this payment did not settle. (`/retry-failed` refuses to overwrite the
  same field for the same reason.)
- **A de-duped `payment_reconciliation` exception is opened** against the
  invoice. `failed` is in `LIVE_PAYMENT_TERMINAL_STATUSES`, so the aged-out row
  stops holding the invoice's live-payment slot — while the invoice itself is
  still `payment_scheduled`, a *payable* status. Without the exception it
  simply reappeared in `GET /payments/queue` and the next run paid it a second
  time, silently. `payment_reconciliation` is in
  `PAYMENT_BLOCKING_EXCEPTION_TYPES`, so a fresh run refuses that invoice until
  a human has confirmed with the processor and voided or re-paid — the same
  fail-closed posture `_RETRY_SAFE_FAILURE_PREFIXES` already takes by excluding
  `reconciler_max_age_exceeded` from `/retry-failed`.

Flagging is best-effort (a failure there is logged by exception class and never
costs the transition), de-duped on an already-`open`/`escalated` row for the
invoice so a down rail doesn't accumulate one exception per sweep, and PII-free
— payment id, run id, age in hours, invoice status.

**Tests:** `tests/test_payment_reconciler_durability.py`.

### ERP Payment Sync

After a payment run executes, the system syncs payment data to the connected ERP
on a detached asyncio task:

```
Execute Payment Run (response sent immediately)
    |
    └── Background task (same event loop):
        ├── For each COMPLETED payment in the run:
        │   - Push payment details to ERP (amount, method, reference, date)
        │   - Update invoice status: payment_scheduled → paid
        │   - Log: "[payment-sync] Syncing payment abc: invoice=INV-001, $1,500, method=ach"
        ├── In-flight payments (submitted/processing) are skipped here — they
        │   sync when their terminal-status webhook lands (which re-triggers
        │   the sync for that run).
        |
        └── Log: "[payment-sync] Run xyz: 2 synced, 1 skipped (in-flight), 0 failed"
```

- **Only `completed` payments mark their invoice `paid`.** A run can mix a
  settled `completed` payment with one still `submitted` at the processor;
  marking the in-flight one's invoice `paid` would claim money moved before
  the rail confirmed it (and pre-empt the webhook's own `paid` transition).
  The webhook handler re-dispatches the sync once the in-flight payment settles.
- **This is the primary code path that flips `payment_scheduled → paid`**, and
  nothing re-invokes it once every payment in the run is terminal. Anything
  that wants to defer that transition therefore needs its own release
  mechanism first — the settlement hold has two (accept / void); see
  § Settlement-amount verification → An under-settlement holds the invoice
  short of `paid`. The **inbound ERP webhook** (`api/erp_webhook.py`) is the
  other writer: the tenant's own ERP legitimately reports `Paid` once a bill
  payment is applied there. It runs the **same `settlement_coverage` check off
  the same `Payment.settled_amount`** before transitioning, and on a
  non-covering verdict opens an `erp_reconciliation` exception and leaves the
  invoice at `payment_scheduled` (silent 204) rather than flipping a
  short-paid invoice to `paid` and stranding `/settlement/accept` behind a 409.
- **A completed payment whose settlement doesn't cover the invoice is held
  here**, not marked `paid`, and counted separately in the sync's log line.
- Sync runs async — **doesn't block** the payment run response
- Runs as an `asyncio` task on the app's own loop — **deliberately NOT** the
  worker-thread pattern `extraction_dispatch` uses. The pass reaches
  `transition_invoice`, whose notification hook resolves recipients through the
  module-level `database.control_session_factory`, an engine bound to the main
  loop. Driving that from a second loop raises `RuntimeError: got Future
  attached to a different loop` and can return the half-used connection to the
  pool the *request* path draws from, so unrelated control-plane requests hang
  behind it. This is a pure-`await` I/O pass, so it yields to the request loop
  rather than blocking it — the reason extraction needs a thread does not apply.
  Guarded by `test_dispatch_runs_on_the_callers_loop_not_a_new_one`.

**Files:** `backend/app/services/payment_erp_sync.py`

#### No ERP configured skips the push, not the transition

`settings.erp` gates exactly one thing here: resolving the ERP adapter and
pushing to it. A tenant without one — the **direct schedule, no ERP** branch the
state machine explicitly supports (`approved → payment_scheduled`, no
`sending_to_erp` leg) — still gets its settled invoices marked `paid` by this
pass, through the identical per-leg guards (payment `completed`, invoice
`payment_scheduled`, settlement covering). The adapter is simply never resolved
and no push is attempted.

This used to be an early return over the **whole** pass, which was a live defect
rather than a config nicety: because this module is the only automatic writer of
`payment_scheduled → paid`, every settled invoice of an ERP-less tenant sat at
`payment_scheduled` forever. The aging report, the `/dashboard` pipeline, the
vendor's payment history and the 1099 YTD totals all under-counted paid spend,
and `retention_sweep` never saw the invoice as archivable — while the payment
row itself stayed perfectly correct, which is what made it easy to miss.

`get_erp_adapter({})` is deliberately **not** called on that path. It fails
closed on an unusable config (`docs/decisions.md` §29), which would turn "this
tenant has no ERP" into a permanent strand plus an `erp_reconciliation`
exception for a situation that is not an error. A *named but unsupported* ERP
type is the opposite case and still fails its leg loudly — see § A failed leg is
a strand. In the counts, such a leg reports `synced` (its ERP-facing work is
vacuously complete) with `transitioned` carrying the real answer.

#### A failed leg is a strand, and it is visible

Because nothing re-invokes this sync for a payment that is already `completed`,
a leg that *fails* is not "retried next tick" — it is permanent. The money has
moved, but the invoice stays `payment_scheduled`, the ERP is never told, and the
invoice's aging and 1099 YTD totals are wrong from then on.

That used to be invisible: a `logger.warning` carrying an exception class name
and a per-run `failed` counter that died with the fire-and-forget task. No
exception row, no notification, no persisted marker.

Two things changed:

- **Every failed leg opens a de-duped `erp_reconciliation` exception** naming
  the payment, the run, and the retry endpoint, so the strand lands in the queue
  an AP manager already works. `erp_reconciliation` is the type
  `api/erp_webhook` already raises for "the ERP and our ledger disagree and a
  human must reconcile" — the same situation, so no new taxonomy entry. The
  description is PII-free (identifiers, the failure's exception *class*, the
  invoice's current status) — never the raw error message, which can embed
  partial account data.
- **Each leg commits on its own.** The loop used to run every payment inside one
  transaction with a single commit at the end; a leg that failed with a DB error
  poisoned that transaction, so the final commit raised, the outer handler
  rolled back, and the run's *successful* `payment_scheduled → paid` transitions
  were discarded too — silently, with nothing to re-invoke them. Each leg now
  re-reads its own rows by id (so no leg is left holding ORM state expired by
  another leg's rollback) and commits independently.

**The exit is `POST /api/payments/runs/{run_id}/sync-erp`** — an explicit,
audited (`payment_run.erp_sync_retried`) re-run of the same pass, `payment.execute`-gated
and entity-scoped, which *awaits* the pass and returns its real per-leg counts
instead of "queued". It is idempotent by construction, not by a claim: the pass
skips every payment that isn't `completed` and every invoice that isn't
`payment_scheduled`, so a repeat call writes no second transition. It moves no
money — it only reports money that already moved. 409 when the run has no
settled payment to sync.

**It is reachable in the app**, not only over the API: `/payments` → Runs
carries a **Retry ERP sync** row action gated on `payment.execute` (hidden on a
`draft` / `cancelled` run, which can hold no settled payment), whose confirm
names the run and says it moves no money, and whose result renders the real
returned counts — leading with `transitioned`, with an explicit "nothing to
recover" state when it is zero.

**Read `transitioned`, not `synced`, to answer "did this recover anything".**
`synced` counts legs whose ERP-facing work completed, and stays true for a
settled payment whose invoice was already `paid` — so a repeat call reports the
same `synced` count with `transitioned: 0`. (Once the real `adapter.post_payment()`
lands, re-pushing an already-`paid` invoice's payment is still work done, which
is why the two counters are separate rather than one being redefined.)

**The invoice is taken `FOR UPDATE` before the status check**, like every other
status transition in the codebase. The retry endpoint awaits the pass
synchronously, so a manual retry can overlap the background task a webhook
just dispatched for the same run; two unlocked readers would both see
`payment_scheduled`, both clear the coverage check, and both transition — a
duplicate audit row and a duplicate "invoice paid" notification, which (unlike
the outbound-webhook emit, keyed on the invoice id) has no dedupe.

**Voiding is not an exit for this state.** `POST /api/payments/{id}/void`
returns the invoice to `approved`, which invites a second payment for money that
already left. That asymmetry with the `held` (short-settlement) path — a
deliberate hold, which *does* have accept-or-void as its two exits — is why the
retry endpoint exists.

The endpoint deliberately does **not** resolve the `erp_reconciliation`
exception on success. That type is shared with the ERP-void path in
`api/erp_webhook`, so auto-closing "the open one" could silently clear an
unrelated reconciliation — the same reasoning `POST /{id}/settlement/accept`
documents for `fraud_flag`. The human closes it after confirming.

### 5. Reconciliation (Future)

Matching payments against bank statement entries:

- Import bank statement (CSV/OFX)
- Auto-match by amount + date + reference
- Manual match for unmatched entries
- Flag discrepancies

## API Endpoints

### Implemented

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/payments` | List payments (paginated, filterable) |
| `GET` | `/api/payments/{id}` | Get single payment |
| `POST` | `/api/payments` | Create individual (standalone) payment. The amount is bound server-side to the invoice amount **net of applied credit memos**; `amount` in the body is optional and only a cross-check (422 on disagreement). Always standalone — `payment_run_id` is **not** a request field (see § A standalone payment can't be injected into a run). Runs the same financial-integrity gate as a run. See § Credit memos are netted on BOTH money paths. |
| `GET` | `/api/payments/runs/` | List payment runs |
| `POST` | `/api/payments/runs` | Create a payment run (draft) |
| `GET` | `/api/payments/runs/{id}` | Get payment run with its payments |
| `POST` | `/api/payments/runs/{id}/execute` | Execute the payment run + trigger ERP sync. `draft`-only — a run stuck `executing` (worker crash mid-run) is resumed via the endpoint below, not this one. |
| `POST` | `/api/payments/runs/{id}/resume` | Resume a run stuck in `executing` — re-dispatches only its still-`pending` payments; anything already `completed`/`failed`/`submitted`/`processing`/`pending_compliance` from before the crash is left untouched. Same `payment.execute` permission gate as `/execute`. |
| `POST` | `/api/payments/runs/{id}/retry-failed` | Re-attempt the safely-retryable FAILED payments of a `partial`/`failed` run by booking a NEW attempt row (the failed row is never mutated). Never re-dispatches a payment that already succeeded, nor one whose fate at the processor is unknown (`needs_reconciliation`); also skips an invoice that is unpayable, carries an unresolved payment-blocking exception (any member of `PAYMENT_BLOCKING_EXCEPTION_TYPES`), has since been credited, or already has another live payment. Same `payment.execute` gate, segregation check and CFO threshold as `/execute`. See § Why a payment failed, and retrying it. |
| `POST` | `/api/payments/runs/{id}/sync-erp` | Re-run the ERP sync-back for a run whose settled payments didn't land — the exit for an invoice stranded at `payment_scheduled` after a failed sync leg. Awaits the pass and returns its `synced`/`transitioned`/`skipped`/`held`/`failed` counts (read `transitioned` for "did this recover anything"); idempotent by construction; moves no money. `payment.execute`-gated, entity-scoped, audited `payment_run.erp_sync_retried`. 409 when the run has no settled payment. See § ERP Payment Sync → A failed leg is a strand, and it is visible. |
| `POST` | `/api/payments/runs/{id}/cancel` | Cancel a draft run — deletes its child payment rows so the invoices return to the queue, and flips the run to `cancelled`. |
| `GET` | `/api/payments/queue` | List invoices ready for payment — paginated (`page` / `page_size`, default 20). Response carries the whole-set `total` / `selectable_total` / `blocked_total` / `by_currency` + per-row `blocked` / `blocked_reason` / `required_method` — the verdict of `payment_runs.run_refusal_reasons`, the SAME predicate set the run builder enforces. See § The payment queue is paginated and § Financial-integrity exception gate → The queue reports every refusal the run builder enforces. |
| `GET` | `/api/payments/queue/ids` | "Select all N matching" resolver — `{ids, total, truncated, currency, by_currency}` for the whole selectable set, capped at 5000. Same RBAC as `/queue`. |
| `GET` | `/api/payments/summary` | KPIs: total paid, pending, queue count, rebates — every money figure in the org's reporting currency, with `unconverted_payment_count` / `excluded_rebate_count` reporting what each had to leave out. `CardRebate` is a **tenant** table (`decisions.md` §57), so the rebate figure is read from the tenant session and joins `virtual_cards` for its currency + entity; there is no `control_db` dependency and no try/except fallback — a DB failure is a 500, not a confident `0`. See § The payments KPIs are denominated, not just summed. |
| `POST` | `/api/payments/{id}/void` | Void a pending/completed payment. Reverses a `virtual_card` payment's card at the provider too, and reports whether it managed to via `void_card_outcome` + `void_card_disposition` — see § Voiding a card payment cancels the card. |
| `POST` | `/api/payments/{id}/void/retry-card-cancel` | Re-attempt ONLY the card close for an already-`voided` card payment, when the void's best-effort leg left the card live. `payment.void`-gated, idempotent, audited `payment.void_card_cancel_retried`; moves no money and re-asks no rail. 409 unless the payment is `voided` **and** `virtual_card`. See § The outcome is on the response, and so is the remedy. |
| `POST` | `/api/payments/{id}/compliance/release` | Re-run compliance-then-adapter for a payment stuck `pending_compliance`. `payment.execute`-gated, 409 outside that status. See § Sanctions / compliance hold resolution. |
| `POST` | `/api/payments/{id}/compliance/dismiss` | Give up on a payment stuck `pending_compliance` — flips it to `failed` with a required `{reason}`, never reaches the adapter. `payment.void`-gated, 409 outside that status. See § Sanctions / compliance hold resolution. |
| `POST` | `/api/payments/{id}/settlement/accept` | Accept a short / unverifiable settlement as final and release the held invoice to `paid`, recording the required `{reason}` + figures on the append-only trail. `payment.execute`-gated; 409 when the payment isn't `completed` or when its settlement already covers the invoice. See § An under-settlement holds the invoice short of `paid`. |

**Query parameters for `GET /api/payments`:**

| Parameter | Type | Description |
|---|---|---|
| `page` | int | Page number (default: 1) |
| `page_size` | int | Items per page (default: 25) |
| `status` | string | Filter by payment status |
| `method` | string | Filter by payment method |
| `invoice_id` | string | Filter by invoice ID |
| `search` | string | Search vendor, invoice number, or reference |
| `amount_min` | decimal | Minimum amount (inclusive) |
| `amount_max` | decimal | Maximum amount (inclusive) |

Both amount bounds are parsed as `Decimal`, never `float` — money is exact
(root `CLAUDE.md` § Project invariants), and a bound routed through a double is
re-rounded before it ever reaches the `Numeric(15, 2)` column it is compared
against. They are then snapped onto that column's own two-decimal grid in the
direction of the comparison (`amount_min` rounds up, `amount_max` rounds down,
via `app/api/money_filters.py`), because SQLAlchemy casts the bind parameter to
the column's scale — so an over-precise bound would otherwise be rounded to
nearest, straight back onto the boundary row it was written to exclude. Both
`GET /api/payments` and `GET /api/payments/counts` take them through the shared
`_payment_list_filters` builder, so the History chips and the table can never
resolve different sets for the same bound. The invoice list, its `/counts` and
its `/ids` resolver share `_invoice_list_filters` the same way.

**`POST /api/payments/runs` request body:**
```json
{
  "items": [
    { "invoice_id": "uuid", "method": "ach" },
    { "invoice_id": "uuid", "method": "wire" },
    { "invoice_id": "uuid", "method": "virtual_card" }
  ]
}
```

### Planned

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/payments/schedules` | List payment schedules with discount info |
| `PATCH` | `/api/payments/{id}` | Update payment (status, reference) |

## Data Model

```
invoices
  |-- payment_schedules (invoice_id FK)
  |       due_date, discount_date, discount_percent, payment_terms
  |
  |-- payments (invoice_id FK)
          amount, method, status, reference
          |-- payment_runs (payment_run_id FK)
                  status, total_amount, initiated_by, executed_at
```

All records carry a `correlation_id` matching the invoice for full traceability via audit logs.

## Payment Methods

| Method | How it works |
|---|---|
| **ACH** | Electronic bank transfer. 1-3 business days. Cheapest option. |
| **Wire** | Immediate bank transfer. Higher fees. Used for large/urgent payments. |
| **Check** | Physical check printed and mailed. Slowest. Used for vendors without electronic payment. |
| **Virtual Card** | Single-use credit card number. Vendor charges the card. Can earn rebates. |

The payment method can be set:
- Per-invoice when creating a payment
- Per-vendor as a default (stored on the vendor record's `bank_details` JSONB)
- Organization-wide default (future)

## Early-Pay Discounts

Many vendors offer discounts for early payment (e.g., "2/10 Net 30" = 2% discount if paid within 10 days).

The payment queue highlights discount opportunities:

- Shows the discount amount (e.g., "$30.00 savings if paid by Apr 11")
- Sorts discount-eligible invoices to the top when the discount window is closing
- The savings are calculated as `invoice.amount * discount_percent / 100`

This helps AP teams prioritize payments that save money.

## Integration Points

### ERP Systems

After payment execution, the payment details (amount, method, reference, date) can be sent to the ERP system alongside the invoice data. The `correlation_id` links the payment back to the original invoice submission.

### Bank / Payment Processor

Payment execution requires integration with a payment processor or bank API:

- **ACH**: Requires bank routing + account number (stored in vendor `bank_details`)
- **Wire**: Requires SWIFT/BIC + IBAN or routing + account
- **Check**: Requires a mailing address on the vendor's own `bank_details.mailing_address`
  (street/city/state/postal/country) — the `checkeeper` adapter reads that exact
  key; a vendor with none refuses with `checkeeper_missing_mailing_address` (see
  `services/payment_adapters/checkeeper.py`)
- **Virtual Card**: Requires vendor acceptance of card payments

Currently, payment execution is a status change only — actual bank integration is a future phase.

### Provider resolution — an unsupported name fails closed

`get_payment_adapter` (`services/payment_adapters/dispatcher.py`) resolves
`Organization.settings.payments.provider` to an adapter:

- **no configured provider → `mock`.** The local-first default (guard rail 7);
  an org that has configured nothing is a normal state.
- **a configured name we have no adapter for → `UnknownPaymentProviderError`.**

It used to fall back to `mock` here too. `mock` is not an inert stub — its
`create_payment` returns `success=True, status=completed` immediately, its
`parse_webhook` verifies no signature, and its `void_payment` returns `True`
unconditionally. So a single typo in the admin-entered settings value
(`modern-treasury` for `modern_treasury`) made **every payment in every run
report as settled while no money moved**, flipped the invoices to `paid`,
routed the public webhook route to an unverified parser under a name the
`provider == "mock"` early-return there cannot catch, and recorded upstream
voids that never happened. The ERP and FX dispatchers had the same fallback
with comparable consequences. Full rationale + the per-caller policy:
[decisions §29](../../docs/decisions.md).

What each caller does with the refusal:

| Caller | Behaviour |
|---|---|
| `POST /runs/{id}/execute`, `/resume`, `/retry-failed` | `_require_payment_adapter` runs **before** the run is claimed → 409, run stays `draft`, nothing dispatched |
| `POST /{id}/compliance/release` | Same pre-flight → 409, payment stays `pending_compliance` |
| `POST /{id}/void` | Still voids locally (the books reflect intent); audit `adapter_outcome: "provider_not_supported"` instead of a fictitious `voided_upstream` |
| `POST /payments/webhook/{tenant}/{provider}` | Silent 204 like every other rejection — no parse, no tenant DB opened |
| `payment_reconciler` sweep | Propagates → the tenant counts as a sweep failure → `degraded` on `GET /api/health/sweeps` |
| `corridor_quotes.compare_quotes` | Skips just that provider (`unavailable_reason: "provider_not_supported"`) so one bad name can't take the auction down |
| `POST /organization/test-payments` | Names the bad provider and lists the registered alternatives — where an admin should find the typo |
| `cashflow.fetch_provider_balance` | Degrades to the manual opening balance (best-effort by contract) |

The international leg is the FX twin: an unsupported `settings.fx.provider`
fails the payment with `failure_reason="fx_provider_unsupported"` rather than
locking a fabricated rate onto the row. The reason names the condition and not
the admin's raw settings value, because every AP user reads `failure_reason`.

Guards: `tests/test_payment_provider_resolution.py`, plus the webhook half in
`tests/test_payment_webhook_security.py`.

### Audit Trail

Every payment **status transition** writes an append-only audit row (project
invariant — the `audit_log` table is DB-level immutable). Executing a run is
the load-bearing money-movement event, so it writes both a run-level row and
one row per child payment recording its terminal state (the transition that
set the regulated `completed_at`). Voiding, CFO sign-off, and cancellation
each write their own row. `details` is PII-free: ids, status, method, the
Decimal `amount` as a string, and the reference — never bank/account values.

| Action | Trigger | Entity |
|---|---|---|
| `payment_run.executed` | A run is executed (`POST /runs/{id}/execute`); rolls up `payments_completed` / `_in_flight` / `_failed` / `cards_issued` + `total_amount` | `payment_run` |
| `payment.completed` | A child payment settled (mock adapter or, in prod, a webhook). Any completion that ran the settlement verifier — the webhook path AND the reconciler backstop — also carries `details.settlement` — the settlement-amount verdict (`matched` / `amount_mismatch` / `currency_mismatch` / `unverified`) with the settled + authorized amounts as exact strings and the signed variance — plus `details.realized_fx_gain_loss` (exact string, absent rather than zero when there is no FX exposure) on both paths. See § Settlement-amount verification | `payment` |
| `payment.failed` | A child payment failed during execution | `payment` |
| `payment.submitted` / `payment.processing` | A child payment is in flight awaiting the processor webhook | `payment` |
| `payment.pending_compliance` | A child payment held by the sanctions/KYC gate | `payment` |
| `payment.compliance_released` | `POST /{id}/compliance/release` re-ran compliance-then-adapter for a held payment | `payment` |
| `payment.compliance_dismissed` | `POST /{id}/compliance/dismiss` gave up on a held payment, flipping it to `failed` | `payment` |
| `payment.voided` | A completed / in-flight payment voided (`POST /{id}/void`); also writes the `invoice.voided_return_to_approved` invoice row | `payment` |
| `payment_run.cfo_approved` | CFO sign-off on an over-threshold draft run | `payment_run` |
| `payment_run.cancelled` | A draft run cancelled before execution | `payment_run` |

The invoice side of a payment is audited separately by
`transition_invoice` (`invoice.payment_scheduled` on execute,
`invoice.voided_return_to_approved` on void) against the `invoice` entity.

## Role Access

| Action | Admin | AP Manager | AP Clerk | CFO |
|---|---|---|---|---|
| View payment queue | Yes | Yes | No | Yes |
| Create payment run | Yes | Yes | No | No |
| Execute payment run | Yes | No | No | Yes |
| View payment history | Yes | Yes | No | Yes |
| Void a payment | Yes | No | No | Yes |

CFO approval is required for executing payment runs above a configurable
threshold (`Organization.settings.payments.cfo_approval_above`, a `Decimal`).
The gate is **strict `>`**, matching the setting name (`cfo_approval_above`): a
run whose total is *above* the threshold requires CFO sign-off; a run exactly
*at* the threshold does not, and a threshold of `0` (or negative) disables the
gate entirely. A configured-but-unparseable threshold fails **closed** — the run
is created `requires_cfo_approval=True` and the misconfiguration is logged
(PII-free) for an admin to correct, rather than silently disabling the control.

`PaymentRun.total_amount` is a single bare `Numeric` column with no currency
of its own, so `create_payment_run_for_invoices` refuses (422) a batch whose
invoices don't all share one currency — summing a USD and a EUR invoice into
one total would misfire (or fail to fire) this gate on a face-value
coincidence across currencies. Each `Payment` still settles independently in
its own invoice's currency at execution time; this only constrains what one
run can report a single total for.

#### The threshold is denominated in the org's REPORTING currency

`cfo_approval_above` is a bare number, exactly like
`settings.expense_approval.cfo_threshold`, and what it is denominated in is the
org's **reporting currency** (`currency_conversion.resolve_reporting_currency`).
So the amount has to be expressed in that currency *before* it can be compared.

Refusing a mixed-currency batch (above) closes only the batch half of that
problem. A run entirely in ONE foreign currency was still compared at face
value, which made the gate fail **OPEN** for every foreign payable priced below
the threshold in its own units: a **GBP 9,000** run — **USD 11,400** at the rate
already locked on its invoice — slipped under a USD 10,000 threshold and
executed with no CFO sign-off. The standalone `POST /api/payments` gate had the
identical hole.

Both now compare the **reporting-currency** figure, via
`services/payment_controls.cfo_approval_decision` (one owner, so the two paths
can't drift) fed by `currency_conversion.reporting_amount_at_locked_rate`:

- **No FX call.** The rate was locked onto the invoice row when it was last
  saved (`materialize_reporting_amount` → `invoices.reporting_fx_rate` +
  `reporting_source_currency`). A rate fetched on a read would make a control's
  verdict move with the market.
- **Fail-closed, both ways.** An unparseable threshold requires sign-off
  (unchanged), and an amount that *cannot* be expressed in the reporting
  currency — no locked rate, or a lock that no longer describes the row's
  currency pair — is treated as **over** the threshold, never under. This is
  stricter than the display rollups' `reporting_amount_for_row`, deliberately:
  a rollup prefers a slightly stale figure to a missing one, a control does not.
- `PaymentRun.total_amount` is unchanged — it is still what the run *pays*, in
  the currency its invoices share. The `payment_run.created` audit row carries
  `cfo_threshold_currency` / `cfo_evaluated_amount` / `cfo_reason` so the
  decision is reconstructable when the two figures differ.

Guards: `tests/test_payment_run_critical_path.py` (the reporting-currency
section) and `tests/test_payment_create_cfo_gate.py`.

### Financial-integrity exception gate

Independently of the CFO threshold, `create_payment_run` refuses any invoice
that still carries an **unresolved** (`open`/`escalated`) exception of a class
listed in `payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`. This gate (along with
the payable-status check, credit-memo netting, and the CFO-threshold
computation above) lives in `services/payment_runs.py::create_payment_run_for_invoices`
— `POST /api/payments/runs` and the AI Cash-Flow Copilot's
`POST /api/cash-flow/plans/{plan_id}/draft-run` (`docs/cash-flow-copilot.md`
§5/§6) both call the same function, so a fraud-control gate can never diverge
between the manual and copilot-driven paths:

| Type | What it would let through |
|------|---------------------------|
| `duplicate` | the same invoice approved and paid twice |
| `fraud_flag` | a bank-detail swap, rush payment, statistical anomaly, an altered / never-issued cheque from a Positive Pay return, or a processor settlement that didn't reconcile against what AP authorized (§ Settlement-amount verification) |
| `line_total_mismatch` | a header `amount` that openly disagrees with the invoice's own line items — the run pays the header, and the header is never silently recomputed from the lines (see `line-total-reconciliation.md`) |
| `payment_reconciliation` | a second payment for money that may already be moving — the reconciler aged a still-`submitted` payment out to `failed`, which frees the invoice's live-payment slot while the rail has never confirmed either way (§ The reconciler backstop) |

Each is raised as an `error`-severity advisory flag, and **approval does not gate
on any of them** — nothing in `services/review.py` or `workflow_engine.py` reads
warning severity, so all three can be approved straight past. Payment-run
creation is the gate that stops the money.

Resolving or dismissing the exception is the human sign-off that clears it and
makes the invoice payable again; `escalated` still blocks, because it means a
human is still working it. The run is refused as a whole, and the 409 detail
names each offending invoice **with the type that blocked it** (`INV-042
(payment_reconciliation)`), so the operator drops or clears them rather than
guessing. The type is read from `blocking_exception_types`, never spelled out
in the message: the wording used to recite a fixed "duplicate/fraud/line-total"
list, which silently went wrong the moment `payment_reconciliation` joined the
tuple — an invoice held because money may still be in flight was refused with
three causes it doesn't carry. Same rule on the standalone `POST /api/payments`.
The exception `description` is still never included (it can carry vendor / bank
/ amount detail); only the fixed PII-free type vocabulary is.

**Every path that books money runs this gate**, via the shared
`payment_runs.blocking_exception_types` (and its ids-only wrapper
`blocked_invoice_ids`) so they can't drift:

| Path | Why it has to re-check |
|------|------------------------|
| `POST /api/payments/runs` + the copilot draft-run | the batch entry point |
| `POST /api/payments` (standalone) | books money exactly like executing a run, and has no `/execute` step to gate later. Until this was wired it was a complete bypass: an invoice the run path refused with a 409 could simply be posted here instead |
| `POST /api/payments/runs/{id}/retry-failed` | a flag raised *between* run creation and a re-send days later (a BEC bank-detail swap, an altered cheque off a Positive Pay return) has to stop the re-send |
| `_execute_single_payment` (the `/execute`, `/resume` and `/compliance/release` dispatch leg) | run creation refuses a blocked invoice, but a draft run can sit for days awaiting CFO sign-off and nothing freezes the invoice — a `fraud_flag` from an approved bank-detail swap raised in that window has to stop dispatch, right beside the payable-status re-check, refused BEFORE the adapter call (`invoice_blocked:<type>`, retry-safe). The live-card claim (`card_claimed_invoice_ids`) is re-checked at the same point (`invoice_has_live_card`) |

Coverage: `tests/test_payment_run_blocking_exceptions.py` drives real exception
rows against a real DB, so the membership of the tuple is pinned in **both**
directions (a `po_mismatch`, which is advisory here, must not block) — and
covers the standalone path AND the dispatch-time re-check (a flag raised after
the run is built) with the same parametrised cases, including that clearing the
flag releases it there too.

#### The queue reports every refusal, from the run builder's own predicate set

A gate the operator can't see is a gate they walk into. The run builder enforces
**four** per-invoice refusals, and the queue used to derive its `blocked` flag
from ONE of them — so every other refusal was a row the queue happily offered and
`create_payment_run_for_invoices` then hard-409'd, taking the whole select-all
batch down with no way to bisect which row did it.

They now come from one place. `payment_runs.run_refusal_reasons(db, invoices,
methods=..., net_amounts=...)` is the single answer to *"would a payment run
refuse this invoice, and why"*, and **both** surfaces read it: the builder turns
its verdict into the 409s below, and `GET /api/payments/queue` turns it into the
row's flags. A refusal added there reaches the queue for free — the way exception
types already did.

| Refusal | Reason code | On the queue |
|---------|-------------|--------------|
| an unresolved (`open`/`escalated`) payment-blocking exception | the exception **type** (`duplicate` / `fraud_flag` / `line_total_mismatch` / `payment_reconciliation`) | `blocked: true` |
| applied credit memos cover the whole invoice — a `$0` payment a real rail rejects as `failed` | `fully_credited` | `blocked: true` |
| a live virtual card already claims the invoice (`POST /api/cards/generate` mints one with no `Payment` row behind it) | `live_virtual_card` | `blocked: false`, `required_method: "virtual_card"` |
| a live payment already claims the invoice (`uq_payments_one_live_per_invoice`) | `live_payment` | the row is **excluded** — see below |

Not covered there, deliberately, because neither is a per-invoice question a row
can render: `PAYABLE_INVOICE_STATUSES` (the queue's own base filter IS that
predicate, so a non-payable invoice is never a queue row), and the
single-currency-per-run rule + the CFO threshold, which are properties of the
SELECTION rather than of any one invoice.

**A live payment is excluded, not marked.** `_queue_base_where()` drops any
invoice with a **live** payment — `Payment.status NOT IN
LIVE_PAYMENT_TERMINAL_STATUSES`, the SAME definition the builder's
`live_payment_invoices` guard uses. It used to exclude only
`Payment.status == "completed"`, so an invoice with a `submitted` /
`processing` / `pending` payment — every real rail, ACH settles in 1-3 days —
was a selectable queue row that a run then 409'd on the unique index. A
**terminal** (`failed` / `voided` / `cancelled`) payment leaves the invoice
offered again — re-pay after a failure. It is excluded rather than badged
because it is not a refusal to act on: something is already paying that invoice,
so it is not "ready to pay" in the first place.

**A card claim pins the rail; it does not block the row.** The builder refuses a
card-claimed invoice on every rail EXCEPT `virtual_card`, which *converges* onto
the existing card rather than opening a second outflow (`CARD_CONVERGING_METHODS`
— the documented mint-a-card-then-run-it flow). Blocking the row would break that
flow, so the row stays selectable with `required_method` set, and the frontend
pins its method select to that rail. It IS excluded from `selectable_total` and
`/queue/ids`, because "select all N matching" stages the default rail.

Consequently **`blocked_total` and `selectable_total` are not complementary**, and
`blocked_total` is counted in its own right rather than as `total -
selectable_total`: a card-claimed row leaves the selectable set while its checkbox
still works, and subtracting would report it to the operator as something to go
and clear.

| Field | Meaning |
|-------|---------|
| `blocked` | a run refuses this invoice on **every** rail |
| `blocked_reason` | the stable, PII-free reason code — set for a `required_method` row too, so the UI can say WHY the rail is pinned |
| `required_method` | the one rail this invoice is still payable on, or `null` |
| `selectable_total` | rows "select all N matching" resolves (`/queue/ids`) — excludes blocked AND rail-pinned rows |
| `blocked_total` | rows whose checkbox is disabled, i.e. exactly the `blocked` ones |

Four properties are load-bearing:

- **One predicate set, not five.** The builder's four gates and the queue's flags
  are the same function. The whole-set aggregates and `/queue/ids` restate those
  predicates in SQL (they must — otherwise every KPI would stream every invoice id
  into Python), and `test_payment_queue_blocked.py` compares the two directly over
  a population carrying every refusal at once, rather than trusting the
  restatement.
- **The reason is a code, never prose.** An exception's `description` can name a
  vendor, a bank account or an amount; `blocked_reason` stays inside the fixed
  PII-free vocabulary (which also lets the frontend localise it). An invoice
  carrying several blocking exceptions reports the one earliest in
  `PAYMENT_BLOCKING_EXCEPTION_TYPES` — a fixed order, so the answer doesn't
  depend on row order.
- **The 409 messages did not change.** Folding four hand-written gates into one
  shared resolver kept each refusal's own operator-facing message (`_REFUSAL_ORDER`
  reports them in the order the separate checks used to run), and the final rung
  is fail-CLOSED — a future reason code with no message still refuses the run
  rather than staging it.
- **Additive.** All three fields default to "no refusal", so a client that ignores
  them behaves exactly as before.

The live-payment refusal is now also a **pre-check**, not only the
`IntegrityError` backstop: the same 409 arrives before the insert, and the
savepoint + `uq_payments_one_live_per_invoice` still catch the concurrent race.

Coverage: `tests/test_payment_queue_blocked.py` — a blocking type marks the row,
an advisory one (`po_mismatch`) does not, `escalated` still blocks,
resolved/dismissed releases, every member of the tuple is reported, the reason is
deterministic under multiple exceptions, the description never reaches the
payload, a fully-credited invoice is blocked while a partly-credited one stays
payable, a card-claimed invoice is rail-pinned (and a cancelled card releases it),
`blocked_total` counts blocked rows rather than merely-unselectable ones, the SQL
selectable set equals the Python verdict, the reason vocabulary is pinned to what
the builder can raise, each refusal still 409s with its own message, and the
converging card rail is still accepted.

## Code Structure

```
backend/app/api/payments.py                # All payment endpoints (CRUD, runs, queue, summary)
backend/app/models/payment.py              # Payment, PaymentRun, PaymentSchedule models
backend/app/schemas/payment.py             # Pydantic schemas
backend/app/services/payment_runs.py       # Shared run-creation validation + rollups
backend/app/services/payment_settlement.py # Pure settlement-amount verifier (webhook)
backend/app/services/payment_reconciler.py # Backstop polling for missing webhooks
backend/app/services/payment_erp_sync.py   # Async ERP sync after payment execution
```

## Implementation Status

| Feature | Status |
|---|---|
| Payment queue (approved invoices ready to pay) | Done |
| Payment history (all methods, filterable) | Done |
| Payment runs list | Done |
| Summary bar (KPIs) | Done |
| Create payment run (select invoices, choose methods) | Done |
| Review panel (per-invoice method selection, total) | Done |
| Execute payment run (complete payments, generate references) | Done |
| Async ERP sync after execution | Done |
| Invoice status update on payment (→ payment_scheduled → paid) | Done |
| Payment schedules (auto-created in seed) | Done |
| Seed data (3 payments, 1 run, payment schedules) | Done |
| Early-pay discount highlighting | Planned |
| Cancel/void payment run | Planned |
| Bank reconciliation | Planned |
| Actual payment processor integration (ACH, wire) | Planned |
| Virtual card generation in payment run | Planned |
