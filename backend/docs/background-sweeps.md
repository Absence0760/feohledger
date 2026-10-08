# Background sweeps — health, supervision, and the shared loop runner

Fifteen long-lived asyncio tasks are started in `app/main.py`'s lifespan, each
behind its own `FEOH_*_ENABLED` gate. They are enumerated with what they do in
§ The sweeps themselves at the foot of this document. The sections below cover
the machinery they all share: how a tick's outcome is recorded, how a
dead sweep is detected, and how an operator reads either.

## The problem this closes

Every sweep used to carry its own copy of the same loop:

```python
while True:
    try:
        await <sweep>_once()          # <-- return value DISCARDED
    except Exception as exc:
        logger.error("[x] sweep raised: %s", exc.__class__.__name__)
    await asyncio.sleep(interval)
```

Twelve of the fourteen `*_once` functions already returned a result dataclass
carrying a `failures: int`, and **every loop threw it away at the call site**.
The counter's only consumer was a conditional aggregate `logger.info` inside
`*_once` itself: never persisted, never exposed, never alerted on. There was
also no supervision — `asyncio.create_task` with no `add_done_callback` — and
`GET /api/health` returned a static `ok` that said nothing about whether any
sweep was alive.

The consequence, and the reason this was worth fixing: an `audit_shipping` sink
misconfigured for months (the adapters raise by design, so rows stay unshipped
and retry forever — the SOC 2 WORM evidence trail simply isn't leaving the
tenant DB) looked *identical* to one running clean.

## The mechanism — one, not fourteen

`app/services/sweep_health.py` owns all of it:

| Piece | What it does |
|-------|--------------|
| `run_sweep_loop(name, tick, …)` | The single loop body every `run_*_loop` delegates to. Ticks, records the outcome, sleeps, re-raises `CancelledError` on shutdown. |
| The registry | Per-sweep last-run timestamps, last outcome, consecutive-failure streak, lifetime run counts, and the integer counters the sweep's own result dataclass reported. |
| `supervise_task(task)` | The `add_done_callback` that records a sweep task ending — cleanly cancelled, or **died**. |
| `extract_counts` / `failure_count` | Pure helpers turning any sweep's result into PII-free counters. |

A `run_*_loop` is now four lines:

```python
async def run_reaper_loop() -> None:
    await run_sweep_loop(
        SWEEP_EXTRACTION_REAPER,
        lambda: reap_once(),
        interval_seconds=settings.extraction_reaper_interval_seconds,
        log=logger,
        log_prefix="[reaper]",
    )
```

Two details are load-bearing:

- **`tick` is a zero-arg callable that resolves `*_once` at call time**
  (`lambda: reap_once()`, never a captured `reap_once` reference). The suites
  patch the module attribute, and a captured reference would sail past the
  patch.
- **`log` is the calling module's logger**, so per-sweep log filters keep
  working and a failure is attributed to the sweep that had it. Only the loop
  *body* is shared, not the log identity.

## What counts as a failed run

Two things, and the first is the one the follow-up existed for:

1. **The tick completed but reported failures** (`failures > 0` on its result,
   or any `*_failures` counter — `vendor_rescreen` counts `vendor_failures`
   and `recurring_invoices` counts `template_failures` apart from `failures`).
   Outcome `partial`, logged at WARNING.

   **The `*_failures` suffix is a decision, not a naming habit.** A per-item
   counter joins the health signal only when the item failing means the
   *platform* is failing. `recurring_invoices.templates_skipped` deliberately
   does NOT carry the suffix: a template missing a vendor or an amount is a
   tenant configuration problem no platform operator can fix, so counting it
   would pin the sweep at `degraded` indefinitely and drown the streak alert
   that exists for real breakage. That skip is surfaced per-template instead —
   a persisted marker, an audit row, and an auto-pause that bounds it (see
   `recurring-invoices.md` § A skipped period is never silent).
2. **The tick raised.** Outcome `error`, logged at ERROR with the exception
   **class**.

Either increments `consecutive_failures`; a clean run resets it to zero. Once
the streak reaches `FEOH_SWEEP_FAILURE_ALERT_STREAK` (default 3) the sweep is
reported `degraded` and the loop emits the alertable line:

```
[audit-shipper] NOT MAKING PROGRESS: 3 consecutive failed runs (last error class: ClientError)
```

It is emitted on each *multiple* of the streak, not every tick — a 60-second
sweep stuck for a day would otherwise write 1440 identical lines and the signal
an operator alarms on has to stay greppable.

## PII discipline

Only an exception's **class name** is ever recorded or logged, and never with
`exc_info`. The stdlib logging module appends the full traceback — including
`str(exc)` — whenever `exc_info` is passed, regardless of what the format string
names, so passing it leaks exactly the vendor names and account fragments the
PII-out-of-logs invariant exists to exclude. `payment_reconciler` had already
diagnosed this and fixed it for itself; six other loops still passed
`exc_info=True` and two called `logger.exception`. All fourteen now share the
reconciler's posture.

Recorded counters are integers pulled off the sweep's own result dataclass by
`extract_counts`, which keeps **only** `int` fields (and excludes `bool`). A
future result field holding a slug, a name or a message cannot be lifted into
the health payload by accident.

## Reading it: `GET /api/health/sweeps`

Admin-gated (`require_roles(ROLE_ADMIN)`), no tenant DB access, PII-free.

```json
{
  "state": "degraded",
  "failure_alert_streak": 3,
  "sweeps": [
    {
      "name": "audit-log-shipper",
      "state": "idle",
      "enabled": true,
      "started_at": "2026-08-16T09:00:00Z",
      "last_run_started_at": "2026-08-16T11:04:00Z",
      "last_run_finished_at": "2026-08-16T11:04:02Z",
      "last_outcome": "partial",
      "last_error_class": null,
      "last_failure_count": 4,
      "consecutive_failures": 37,
      "total_runs": 128,
      "total_failed_runs": 37,
      "exit_error_class": null
    }
  ]
}
```

Per-sweep `state` is one of:

| State | Meaning |
|-------|---------|
| `disabled` | Its `FEOH_*_ENABLED` flag is off in this process. Expected, not a problem. |
| `not_started` | **Enabled but never registered.** Flips the aggregate to `failing`. |
| `starting` / `running` / `idle` | Registered; a tick is pending, in flight, or done. |
| `stalled` | A tick has been in flight far past the sweep's own cadence — see below. |
| `stopped` | Cancelled cleanly at shutdown. |
| `died` | The task ended on its own — a real defect. It is gone for the life of the process. |

The aggregate `state` is `failing` if any sweep died or is enabled-but-absent,
`degraded` if any is past its streak or stalled, else `ok`.

### The hung-tick hole, and `stalled`

Failure counting alone leaves one case invisible: a sweep **hung inside**
`*_once` — a DB connect with no timeout, an adapter socket that never returns —
never raises, never completes, and so never touches the streak. It sits in
`running` forever while reporting perfectly healthy, which is precisely the
"alive but not progressing" state this whole mechanism exists to expose.

`stalled` closes it. It is **derived on read**, not stored — nothing is
executing while a tick hangs, so there is no code path that could write it. A
tick is stalled once it has been in flight longer than
`max(STALL_FACTOR × interval, STALL_FLOOR_SECONDS)` (3× and 15 minutes; module
constants in `sweep_health.py`, not env knobs — the tunable dial is the failure
streak). The threshold is deliberately generous so a legitimately long tick (the
audit shipper draining a large backlog) is not called stalled: a 60-second sweep
gets the 15-minute floor, a daily one gets three days, by which point a tick
still in flight is unambiguously hung.

Stalled reports `degraded` rather than `failing` because a very long tick has a
plausible benign explanation; a dead task has none.

### What the payload deliberately omits

**No cross-tenant cardinality.** An ordinary tenant admin holds `ROLE_ADMIN`, so
returning the raw counters (`tenants_scanned`, `rows_shipped`, …) would tell
them how many organizations the platform sweeps. Only `last_failure_count`
crosses the boundary — the actionable number, and zero on a healthy platform.
The full counters stay in the registry and in the logs, where the reader is a
platform operator who is already trusted with them.

### The UI: `/admin/health`

`frontend/src/routes/admin/health/+page.svelte` (admin only, matching
`require_roles(ROLE_ADMIN)`; typed client
`frontend/src/lib/api/sweepHealth.ts`) is the operator view of this payload.
Because the public probe deliberately reports nothing (see below), this page is
the only place a dead, stalled or repeatedly-failing sweep is visible at all.

It renders the aggregate `state`, the configured `failure_alert_streak`, how
many sweeps are reporting, and a count of the ones **needing attention** —
`died` / `not_started` / `stalled`, or past the streak. `disabled` is
deliberately excluded from that count: an off flag is an expected state, not a
fault, and it is chipped `neutral` rather than alarm-toned. Each row shows the
sweep's state, last run, last outcome, consecutive failures and run totals; a
failure carries only the exception **class**, exactly as the payload does.

It loads once on mount and is **not polled** — the read is admin-gated but
writes no audit row and touches no tenant DB, so an on-mount load is safe, while
polling a process-local registry would just add noise. An operator presses
*Refresh* for a newer sample. `frontend/tests-e2e/admin/sweep-health.spec.ts`
covers the live roster, a mocked `died` sweep, the error/retry state, the clerk
refusal (redirect plus the backend's 403) and a WCAG 2.2 AA axe pass.

### `GET /api/health` is unchanged

It stays the public, unauthenticated, static `{"status": "ok"}` liveness probe,
and it deliberately does **not** fold in sweep health. A degraded background
sweep is not a reason to pull a healthy process out of rotation; wiring it in
would turn "the audit shipper's sink is misconfigured" into a rolling restart
loop that fixes nothing.

## Locking: a sweep must never hold what the request path needs

A sweep that mutates rows the request path also locks has to bound the lock, not
just the work. `approval_escalation` did neither: it ran

```sql
SELECT * FROM workflow_instances WHERE state = 'active' FOR UPDATE   -- no LIMIT
```

and held every one of those row locks until the end of the tick.
`review.approve_invoice` takes the same row lock, so a tenant with 20 000 open
invoices had its entire approval surface blocked once per tick — and with
replicas (which the § Scope section below assumes), two sweeps locking
overlapping sets in unspecified order deadlock, the tick aborts, and the streak
counter above starts climbing for a reason no config change explains.

The shape every such sweep uses instead is two-phase:

1. Select candidate **ids**, unlocked, `ORDER BY id`, `LIMIT` a page.
2. Per id: `db.get(Model, id, with_for_update=True)` → re-check the predicate
   the id query used (it can have changed under you) → apply → `commit()`,
   which releases the lock before the next row is touched. Nothing to write is
   a `rollback()`, not a hold.

`with_for_update` makes `Session.get` bypass the identity map, so step 2 is a
real `SELECT … FOR UPDATE` on exactly one row. Ordering by id gives every
replica the same lock order, so concurrent sweeps queue instead of deadlocking.

**Step 2's re-check is correctness, not an optimisation.** `extraction_reaper`
had step 1 only: it loaded whole `Invoice` objects up front and transitioned
them from that snapshot. An extraction finishing *during* the tick was then
silently overwritten — `transition_invoice` validates against the stale
in-memory `pending`, `pending → failed` is a legal edge, and the UPDATE stamped
`failed` over the row's real, freshly-committed state. A successfully-extracted
invoice came out `failed`, carrying an `extraction_timeout` warning about an
extraction that had actually succeeded, and could not come back:
`failed → ready_for_review` is not a legal edge, so a reviewer had to re-run
extraction on a document that was already done. The same window swallowed an
invoice that had reached `approved` (`pending → approved` is legal too). The
predicate the id query used is exactly the thing that can have changed under
you, so re-testing it under the lock is the whole point of the shape.

`vendor_rescreen` and `recurring_invoices` had step 1 and the per-item commit
but not step 2 — a plain `db.get(Model, id)`, no lock and no re-check — and each
paid for it differently.

**`vendor_rescreen`: a duplicate that cannot be un-written.** The id query is
unlocked, so between reading an id and touching the row another replica's sweep,
or a manual `POST /api/vendors/{id}/screen`, can screen the same vendor and
commit. Acting on the stale snapshot bills the third-party sanctions provider a
second time for one screening event and appends a second `SanctionsCheck` row
*and* a second `vendor.screened` audit row for it. Both trails are append-only,
so the duplicate cannot be tidied away afterwards — the compliance evidence
permanently overstates how many times the vendor was screened. The
`status == "active"` clause is re-checked too: re-screening a vendor an admin
just retired is work nobody asked for, and a `match` verdict applies a payment
block to a retired supplier.

**`recurring_invoices`: the case a unique index cannot catch.**
`uq_invoice_recurring_period` makes a *duplicate* invoice for one
`(template, period_key)` impossible, which reads like it already covers this
sweep. It does not. The failure without the re-check produces an invoice for a
period that is **not due yet**, on a distinct period key the index is happy to
accept: replica A generates period P, advances `next_run_on` to P+1 and commits;
replica B then locks the row, reads the *fresh* P+1 cursor and — taking whatever
`next_run_on` says, with no re-check — generates P+1 early. A subscription
invoice lands in the approval queue for a month that has not started, and the
cursor jumps to P+2, so the real P+1 tick raises nothing at all. An idempotency
index constrains *what* you may write; only the re-check constrains *whether you
should be writing at all*.

Each sweep spells its re-check as a `_still_due(...)` helper carrying the same
clauses as its own SELECT, so the two cannot drift.

**The id ordering has to be a _total_ order.** `recurring_invoices` sorted by
`next_run_on` alone, which is only a partial order — templates sharing a due date
come back in whatever order the plan produces, so two replicas can take the same
two locks in opposite orders and deadlock. It now sorts `(next_run_on, id)`.
"Ordering by id gives every replica the same lock order" only holds while the
sort key is unique.

**Page, don't cap, unless the work removes itself from the candidate set.**
Escalation doesn't change `state`, so a per-tick cap would re-serve the same
lowest-id rows forever and never reach the rest; it keyset-paginates
(`WHERE id > :last`) until the tenant is exhausted, with
`FEOH_APPROVAL_ESCALATION_BATCH_SIZE` as the page size. `discount_auto_trigger`
and `contract_renewal` are in the same position and page the same way (below).
`retention_sweep` and `recurring_invoices` *can* cap, because an archived invoice
/ a generated period leaves the candidate set and the next tick resumes past it.

The page boundary is safe for the same reason the two-phase shape is safe at all:
step 2 re-claims the row under `FOR UPDATE` and re-checks the predicate there, so
a page read after several commits is no more stale than the one unbounded read it
replaces.

`discount_auto_trigger` and `contract_renewal` were the last two loading their
whole candidate set per tenant in one unbounded `SELECT`. Neither could cap — an
offer skipped for a below-threshold ROI stays `offered`, and a contract outside
its own lead window stays un-alerted — so bounding them meant a keyset cursor and
a page-size setting each, not a `LIMIT`. Both now page:
`FEOH_DISCOUNT_OPTIMIZATION_BATCH_SIZE` and `FEOH_CONTRACT_RENEWAL_BATCH_SIZE`
(the latter used by *both* of that sweep's passes, each carrying its own cursor —
sharing one would make the expiry pass start wherever the alert pass stopped).

**A coarse pre-filter refined in Python is the same ceiling wearing a `WHERE`
clause.** `contract_renewal`'s alert pass selected `end_date <= today + 3650
days` — every active contract carrying an end date, for any tenant whose
contracts are not all decades out — and discarded the out-of-window rows after
loading them. The lead window is now a real per-row SQL predicate,
`end_date - today <= COALESCE(renewal_notice_days, <platform default>)`
(`contract_renewal.lead_window_predicate`), and the Python check that survives is
the *under-lock re-check* step 2 requires. Both halves resolve the NULL fallback
through the same `resolve_notice_days`, so the candidate query and the re-check
cannot disagree about what a missing notice window means — and the `COALESCE`
finally gives `FEOH_CONTRACT_RENEWAL_DEFAULT_NOTICE_DAYS` a reader, which it had
never had. A per-row interval expression is the sort of thing that coerces
differently in SQL than in Python (`date - date` is an `integer`, but a bare bind
parameter leaves Postgres choosing between three `date - ?` overloads, hence the
explicit `CAST(:today AS DATE)`), so `tests/test_sweep_candidate_pagination.py`
evaluates the real expression in real Postgres against every boundary — one day
before the window, exactly on it, one day after, and the NULL notice the NOT NULL
column cannot itself hold — and asserts it agrees with the Python re-check.

**And isolate per item, or the pagination is decorative.** A raise from inside
the loop is the second way to starve a tail, and it does not look like a
pagination bug at all. Candidate ids are ordered ascending and the cursor is a
local that resets every tick, so one row that keeps failing aborts the loop at
the *same place* on every tick and nothing after it is ever processed again —
permanently, because nothing about that row changes. Both `extraction_reaper`
and `approval_escalation` had adopted the per-row **commit** from
`vendor_rescreen` / `recurring_invoices` but not their per-row
`try` / `rollback`, and both docstrings claimed the property the commit alone
does not give. Round 16 brought the last holdouts onto the same shape —
`discount_auto_trigger`, `contract_renewal`, `payment_reconciler` and
`billing/dunning_sweep`. The shape is:

```python
for row_id in candidate_ids:
    try:
        ...                       # lock, re-check, apply
        await db.commit()
    except Exception as exc:      # one item must not halt the tenant
        logger.warning("... %s", exc.__class__.__name__)   # class only
        await db.rollback()       # the next item must start clean
        item_failures += 1
        continue
    processed += 1
```

The counter is not optional either, and its **name** carries meaning: the
per-item total is surfaced as a `*_failures` field on the sweep's result
dataclass (`invoice_failures`, `instance_failures`, `vendor_failures`,
`template_failures`, `offer_failures`, `contract_failures`, `payment_failures`)
because `sweep_health.failure_count` sums exactly those. Swallow the item without
counting it and the tick reports `ok` while making no progress — the blind spot
this whole registry exists to close. Keep it separate from `failures`, which
means "the tenant sweep aborted outright".

**The suffix is a claim, so some counts deliberately don't carry it.**
`qms_sync`'s `skipped` and `unchanged`, and `recurring_invoices`'
`templates_skipped`, are provider or configuration facts — an unmapped
disposition, a record re-fetched identical, a template that isn't due. Naming any
of them `*_failures` would pin a working sweep at `degraded` and drown the streak
alert. The test is whether the item failing means the *platform* is failing.

**Two independent passes in one transaction can undo each other.**
`contract_renewal` ran its renewal-alert pass and its end-of-term expiry pass back
to back and committed once, so a raise while expiring a contract rolled back every
`renewal_alert_sent_at` the alert pass had just stamped — the notification emails
had already been dispatched, the markers had not, and the next tick re-sent them
all, until it hit the same poison row again. The reverse held too. Per-item
commits make the passes genuinely independent.

**`payment_reconciler` is the two-phase shape with a third party in the middle.**
It resolves the settled figure from the processor (`fetch_settlement`) — a live
rail round trip — deliberately *before* any lock, then locks, re-checks that the
row is still `submitted`/`processing`, writes, and commits. Running the fetch
inside the lock is the version that shipped: `payment_webhook` takes the same row
lock, so the sweep blocked a real webhook for the whole duration of the HTTP call,
on precisely the row a webhook was most likely arriving for. Resolving first and
locking second is the shape `payment_erp_sync` uses when it resolves the ERP
adapter ahead of the invoice lock, and the re-check under the lock is what makes
it safe — a webhook that settled the row during the fetch is caught there and the
sweep skips rather than double-writing a terminal status.

Two consequences of that loop worth knowing before editing it. It iterates
**loaded ORM rows**, not ids, and `Session.rollback()` expires every object in the
identity map — so the pre-lock decision inputs are snapshotted into locals, and a
bare attribute read after a skip-path rollback would trigger a lazy refresh that
an async session raises on rather than transparently reloading. And its counters
and the `dispatch_payment_sync` hand-off are taken **after** the per-payment
commit, never off the in-memory mutation: `dispatch_payment_sync` is what flips
the invoice `payment_scheduled → paid`, so handing it a run whose payment
transition was rolled back would mark an invoice paid for a payment still sitting
`submitted`.

`webhooks/delivery.deliver_due` is the same shape with one addition: the claim
is `FOR UPDATE SKIP LOCKED`, because a delivery already in flight elsewhere
should be skipped, not waited for (see `public-api.md` § A due delivery is
claimed before it is sent).

## Scope: per-process, in-memory

State resets on restart, and with several replicas each answers for itself.
That is deliberate:

- A durable per-sweep run table would be an Alembic migration fanned out to
  **every tenant DB** for telemetry that is platform-level, not tenant-level.
- There is no platform-scoped settings row to hang a JSON marker off;
  `Organization.settings` is per-tenant, and these sweeps span all tenants.
- "Is *this* process's sweep alive and progressing?" is the question an operator
  actually asks of a suspect replica, and it is exactly what a per-process
  registry answers.

The cluster-wide view is the log sink: every replica emits the same PII-free
`NOT MAKING PROGRESS` line, which is the alertable signal in a deployed
environment.

## Drift guards

`tests/test_sweep_health.py` pins three things a rename would otherwise break
silently:

1. Every `asyncio.create_task(..., name="…")` / `start_sweep(..., name="…")`
   string in the lifespan is a canonical sweep name — the task name **is** the
   registry key.
2. Every lifespan sweep is started via `start_sweep()`, so none can go back to
   being unsupervised.
3. Every entry in `SWEEP_ENABLED_FLAGS` names a real `Settings` attribute.

## Adding a sweep

1. Write `<sweep>_once()` returning a result dataclass with a `failures: int`.
2. Add a `SWEEP_<NAME>` constant and its `SWEEP_ENABLED_FLAGS` entry in
   `sweep_health.py`.
3. Write `run_<sweep>_loop()` as the four-line delegation above.
4. Start it in the lifespan with `start_sweep(run_<sweep>_loop(), name=…)`
   behind its `FEOH_*_ENABLED` gate, and cancel it in the `finally`.
5. Document it in § The sweeps themselves below, and its env vars
   in `docs/environment.md`.

The drift guards fail until steps 2 and 4 agree.

## The sweeps themselves

What each long-lived task does. Moved here from `backend/CLAUDE.md` so that
file stays small enough to load cheaply; this document is now the single home
for both the enumeration and the shared machinery above.

| Service | What it does |
|---------|-------------|
| `services/extraction_reaper.py` | Sweeps every tenant DB on a timer; transitions invoices stuck in `pending` extraction to `failed`. |
| `services/audit_log_shipper.py` | Centralized audit-log shipper (SOC 2). Sweeps every tenant DB, reads unshipped `audit_log` rows in batches, fans them out to every configured `audit_shipping` adapter (CloudWatch Logs + S3 Object Lock), then marks `shipped_at=now()`. All adapters must ACK before rows are marked; failures leave rows unshipped so the next tick retries. Disabled by default — flip `FEOH_AUDIT_SHIPPING_ENABLED` on in deployed envs. See `docs/audit-log-shipping.md`. |
| `services/approval_escalation.py` | Sweeps every tenant's active workflow instances and appends `escalation_to_user_ids` onto any approval chain level waiting longer than its configured `escalation_hours` — including level 0 of a chain nobody has approved yet, and never a target the approval path would refuse (segregation / an earlier level's approver); see `workflow-design.md` § Escalation widens eligibility. Disabled by default (`FEOH_APPROVAL_ESCALATION_ENABLED`); flip on in deployed envs. |
| `services/payment_reconciler.py` | Backstop polling for payments whose processor webhook went missing. Re-fetches status from the payment adapter when a `submitted`/`processing` payment sits longer than `FEOH_PAYMENT_RECONCILE_AFTER_MINUTES`. Disabled by default (`FEOH_PAYMENT_RECONCILE_ENABLED`); flip on in deployed envs alongside Modern Treasury. |
| `services/contract_renewal.py` | Contract renewal-alert sweep. Sweeps every tenant DB; finds `active` contracts within their own `renewal_notice_days` of `end_date` with no alert sent, notifies the owner + AP managers once (`contract_renewal_due` event), then stamps `renewal_alert_sent_at` for idempotency (cleared on `POST /api/contracts/{id}/renew`). Same tick also transitions any `active` contract whose `end_date` has actually passed to `expired` (`contract.expired` audit row) — the only runtime path that sets `ContractStatus.expired`; idempotent (status guard). Disabled by default (`FEOH_CONTRACT_RENEWAL_ENABLED`); `FEOH_CONTRACT_RENEWAL_INTERVAL_SECONDS` / `_DEFAULT_NOTICE_DAYS`. See `docs/contracts.md`. |
| `services/discount_auto_trigger.py` | Dynamic-discounting auto-capture sweep. Sweeps every tenant DB; auto-accepts `offered` `DiscountOffer`s whose annualized ROI clears `FEOH_DISCOUNT_AUTO_CAPTURE_ROI_THRESHOLD`, writing a `discount_offer.auto_accepted` audit row. **Only flags `offered → accepted` — never creates a Payment/PaymentRun**; the status guard is the dedupe. Disabled by default (`FEOH_DISCOUNT_OPTIMIZATION_ENABLED`). See `docs/dynamic-discounting.md`. |
| `services/retention_sweep.py` | Retention-policy enforcement sweep (SOX records management). Sweeps every tenant DB; soft-archives overdue terminal (`done`/`paid`) invoices via a `meta.archived_at` marker (idempotent — re-run never double-archives) and writes a `retention.archived` manifest. **Composes with the audit-immutability trigger — NEVER deletes `audit_log` rows**; for the audit class "retention" verifies WORM shipment (`shipped_at`) + records overdue/unshipped counts only. Windows are per-class on `Organization.settings.retention` (`resolve_retention_months`); `GET/PUT /api/retention-policy` reads/updates them. Disabled by default (`FEOH_RETENTION_ENABLED`); `FEOH_RETENTION_INTERVAL_SECONDS` / `_DEFAULT_MONTHS`. See `docs/retention.md`. |
| `services/recurring_invoices.py` | Recurring / subscription invoice generation sweep. Sweeps every tenant DB; finds `active` `RecurringInvoiceTemplate`s whose `next_run_on` has arrived, generates the next pre-coded `Invoice` into the approval queue (period_key `YYYY-MM` / `YYYY-Qn` / `YYYY`), advances `next_run_on`, and writes a `recurring_template.generated` audit row. **Idempotent on `(template, period_key)`** via the partial unique index `uq_invoice_recurring_period` (a double-fire never double-creates) **and — since the index cannot express "not due yet" — a re-check of `status == active` + `next_run_on <= today` under the row lock before generating** (`docs/decisions.md` §66); **only creates an Invoice in the queue — never creates a Payment/PaymentRun**, exactly like `discount_auto_trigger`. Per-tenant cap `FEOH_RECURRING_INVOICES_MAX_PER_SWEEP`. Disabled by default (`FEOH_RECURRING_INVOICES_ENABLED`); `FEOH_RECURRING_INVOICES_INTERVAL_SECONDS`. See `docs/recurring-invoices.md`. **The generated invoice names the template's author**: `created_by_user_id` (migration 0096) is stamped by `POST /api/recurring`, and `generate_one` stamps `actor_id or template.created_by_user_id` onto `Invoice.uploaded_by_id`, so the sweep's output is no longer exempt from the approver-≠-creator check (`docs/decisions.md` §141). |
| `services/cash_flow_alerts.py` | Projected-cash-shortfall alert sweep. Sweeps every org; skips any without a persisted `settings.cashflow.min_balance_threshold` (the threshold IS the opt-in), builds the org-wide commitment rows (`entity_id=None` — a treasury shortfall is a whole-group question, the same posture `GET /analytics/by-entity` takes), resolves the opening balance through the shared `services/cashflow.resolve_opening_balance`, and runs the pure `compute_cash_position` → `detect_threshold_breaches`. Notifies the finance leaders (`ALERT_ROLES`, drift-guarded against `api/cash_flow.py::COPILOT_ROLES`) once per projected shortfall period via `cash_shortfall_projected` (`entity_type="cash_position"`, `entity_id` NULL). **Never moves money** — no Payment/PaymentRun, no discount, no invoice; the only write beyond the notification rows is the alerted-period marker on the org's settings JSON, which is also the dedupe (cleared when the projection clears, so a recurrence re-alerts). Notify happens BEFORE the marker write so a crash between them re-alerts rather than swallowing the warning. Disabled by default (`FEOH_CASHFLOW_SHORTFALL_ALERTS_ENABLED`); `FEOH_CASHFLOW_SHORTFALL_ALERTS_INTERVAL_SECONDS` / `_HORIZON_DAYS`. See `../docs/cash-flow-copilot.md` § Proactive projected-shortfall alerts. |
| `services/billing/ai_overage.py` | AI-read-invoice overage reconciliation (decisions §253, §255), sweep name `billing-ai-overage`. The primary overage report runs right after each billable extraction commits (`after_ai_read`); this is its backstop. Per org: reports any overage unit the post-read leg missed (`report_ai_overage` — one meter event per unit past the plan allowance, ordinal identifier `ai-overage:<org>:<period>:<n>`, max-merged marker on `settings.billing.ai_overage_reported`, clamped to the customer's spending cap), for the current UTC month and the previous one while the provider still accepts its timestamp; then re-runs the 80% / 100% / cap notice check. A period too old to report counts as a failure and is logged with the unit count. **Reports usage only — never charges, never touches an invoice**; the provider invoices it. One org's failure never halts the tick. Disabled by default (`FEOH_BILLING_AI_OVERAGE_SWEEP_ENABLED`); `FEOH_BILLING_AI_OVERAGE_SWEEP_INTERVAL_SECONDS` (default hourly, inside Stripe's 24 h idempotency window). See `billing.md` § AI-read invoice metering. |
| `services/scheduled_reports.py` | Scheduled-report runner. `run_scheduled_reports_once` sweeps every tenant DB; runs each `enabled` schedule whose `next_run_at` has arrived (`execute_schedule`: generate CSV via `report_export` → email recipients → advance `next_run_at` / persist a `[retry N]` failure marker, auto-disabling after 5 consecutive failures). `next_run_at` advances via `advance_next_run` from the slot the run was **due** at, never from the tick that picked it up — anchoring on the wall clock made a "daily 09:00" report land later every day and walk around the clock inside a month; a missed window catches up in whole cadence steps to the first future slot, never a backlog burst. "Today" for each report's `period_days` window is `utils/dates.utc_today()`, matching the `/analytics` exports and the copilot. One tenant's failure never halts the sweep. Disabled by default (`FEOH_SCHEDULED_REPORTS_ENABLED`); `FEOH_SCHEDULED_REPORTS_TICK_SECONDS`. Its input surface is `api/scheduled_reports.py` (`/api/analytics/scheduled-reports`, admin-only to mutate). See `docs/analytics.md` § Scheduled report delivery. |

| `services/sweep_health.py` | **The mechanism every sweep above shares.** `run_sweep_loop` is the single loop body each `run_*_loop` delegates to — it ticks, records the outcome into an in-process registry (last-run timestamps, `ok`/`partial`/`error`, the consecutive-failure streak, the integer counters the sweep's own result dataclass reported), sleeps, and re-raises `CancelledError` on shutdown. **A tick that completes reporting `failures > 0` is a failed run, not a healthy one** — the case that used to make a months-long broken `audit_shipping` sink indistinguishable from a clean platform. Past `FEOH_SWEEP_FAILURE_ALERT_STREAK` (default 3) the sweep is `degraded` and the loop emits the alertable PII-free `NOT MAKING PROGRESS` ERROR, on streak multiples so a 60-second sweep can't drown the sink. `supervise_task` attaches the `add_done_callback` that records a sweep task **dying** (vs. being cancelled at shutdown) — every start in `main.lifespan` goes through the `start_sweep()` helper, drift-guarded by an AST scan. Only exception **class names** are ever recorded or logged, never `exc_info` (the stdlib appends the traceback text regardless of the format string). Read at `GET /api/health/sweeps`. See `docs/background-sweeps.md`. |

These long-lived asyncio tasks are started in `main.lifespan` (each behind its `FEOH_*_ENABLED` gate) via `start_sweep()` — supervised — and cancelled on shutdown. Their per-tick health is queryable at **`GET /api/health/sweeps`** (admin-only; process-local; no cross-tenant cardinality in the payload). `GET /api/health` stays the public static liveness probe and deliberately does NOT fold sweep health in — a degraded sweep is no reason to pull a healthy process out of rotation. See `docs/background-sweeps.md` and `../docs/decisions.md` §24.
