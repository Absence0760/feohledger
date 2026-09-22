# Recurring / Subscription Invoices

Predictable, fixed-cadence spend (rent, SaaS seats, utilities, insurance)
shouldn't need a fresh upload + extraction every period. A
`RecurringInvoiceTemplate` captures the vendor, amount, GL coding, entity and a
simple cadence (monthly / quarterly / annual + day-of-period); the
`recurring_invoices` background sweep generates the next `Invoice` on schedule,
pre-coded and pre-matched, so it lands straight in the approval queue. Common in
Bill.com (recurring bills), Tipalti (subscription spend), Stampli and Airbase —
absent here until this feature.

This is the feature behind roadmap **Priority 1 → Recurring / Subscription
Invoices**.

## Data model

`RecurringInvoiceTemplate` (`app/models/recurring_invoice.py`, table
`recurring_invoice_templates`, migration `0046_recurring_invoices`,
tenant-scoped + `EntityMixin` + `TimestampMixin`). Money is exact: `amount` is
`Numeric(15, 2)` — never float.

| Field | Type | Notes |
|-------|------|-------|
| `id` | uuid PK | |
| `name` | varchar(200) | Human label, e.g. "Acme Towers — monthly rent". Required. |
| `vendor_id` | uuid FK → vendors | Nullable so a template can be drafted before the vendor exists, but the sweep needs it set to generate. Indexed. |
| `vendor_name` | varchar(255) | Denormalised for display (mirrors `Invoice`). |
| `description` | varchar(500) | |
| `amount` | numeric(15,2) | Fixed amount stamped onto each generated invoice. Required to generate. |
| `currency` | varchar(3) | Default `USD`. |
| `gl_account` / `cost_center` / `department` / `project` / `po_number` / `payment_terms` | varchar | Pre-coding carried onto every generated invoice. A `gl_account` is refused (422) on create and on a PATCH that changes it when it belongs only to another entity's chart, or — whenever the template entity's effective active chart has any account — when it is not an active account of it (retired or unknown): every generated invoice lands under the template's `entity_id` and carries this code (`services/gl_chart.py`, `docs/decisions.md` §194/§199). A PATCH that re-sends the stored code unchanged is not judged, so a template whose account is retired later stays editable — and keeps stamping that code on what it generates until someone re-codes it (`docs/followups.md`). |
| `cadence` | enum | `monthly` (default), `quarterly`, `annual` — how often the template generates. |
| `day_of_period` | integer | Day-of-month (1–28) the invoice is dated/generated on. Capped at 28 so every month is valid (no Feb-30 clamp guesswork). |
| `start_date` | date | Required. First period the template is eligible for. |
| `end_date` | date | Nullable. After it, the template generates nothing (sweep nulls `next_run_on`). |
| `next_run_on` | date | The next calendar date the sweep should generate for. Advanced after each successful generation. NULL = nothing pending (ended / past `end_date`). Indexed (the sweep's `WHERE`). |
| `last_period_key` | varchar(40) | period_key of the most recently generated invoice (`"2026-06"`, `"2026-Q2"`, `"2026"`). Display + a cheap "already ran this period" guard ahead of the DB unique index. |
| `last_generated_at` | timestamptz | When the last invoice was generated. |
| `generated_count` | integer | Running count of invoices generated. Default 0. The `DELETE` guard checks `> 0`. |
| `status` | enum | `active` (default — generates on schedule) \| `paused` (suspended; skip generation) \| `ended` (terminal). Indexed. |
| `variance_tolerance_pct` | numeric(6,2) | Per-template override of the variance tolerance (percent) used to flag an arrived invoice from this vendor that deviates from `amount`. NULL falls back to the org / platform default. |
| `notes` | varchar(500) | |
| `meta` | jsonb | Free-form bag. No PII / banking data. |
| `organization_id` | uuid | Indexed. |
| `entity_id` | uuid FK → entities | From `EntityMixin`; the generated invoices inherit it. |
| `created_at` / `updated_at` | timestamptz | From `TimestampMixin`. |

The status / cadence string constants live on the model
(`STATUS_ACTIVE`/`_PAUSED`/`_ENDED`, `CADENCE_MONTHLY`/`_QUARTERLY`/`_ANNUAL`);
the Pydantic `Cadence` / `TemplateStatus` `StrEnum`s in
`app/schemas/recurring_invoice.py` mirror them on the wire.

## Idempotency (the generation guard)

The link + idempotency live on the **invoice** side, added by migration `0046`
alongside the template table:

- `invoices.recurring_template_id` — nullable FK → `recurring_invoice_templates`
  (the generated invoice's link back to its template), indexed.
- `invoices.recurring_period_key` — varchar(40), the period this invoice
  satisfies.
- **`uq_invoice_recurring_period`** — a **partial unique index** on
  `(recurring_template_id, recurring_period_key) WHERE recurring_template_id IS
  NOT NULL`.

That partial unique index is the idempotency mechanism: **at most one invoice
per (template, period)**. A double-fire of the same period — two sweep ticks
racing, a retried tick, a manual `generate-now` overlapping the loop — can never
double-create. The losing insert raises `IntegrityError`, the transaction rolls
back, and no second invoice (or duplicate spend) lands. The partial predicate
keeps the index off ordinary, non-recurring invoices.

`last_period_key` on the template is a cheap pre-check (skip the period we
already ran) but is **not** the authority — the DB unique index is. This mirrors
the PEPPOL-inbound dedupe and the discount auto-capture status guard: a
DB-enforced uniqueness invariant, not a Redis TTL that a late redelivery could
slip through.

### period_key format

The period_key is a calendar-period string, stable per cadence:

| Cadence | Format | Example |
|---------|--------|---------|
| `monthly` | `YYYY-MM` | `2026-06` |
| `quarterly` | `YYYY-Qn` | `2026-Q2` |
| `annual` | `YYYY` | `2026` |

## Generation sweep (background service)

`services/recurring_invoices.py` is a long-lived asyncio loop started in
`main.lifespan`, mirroring the `contract_renewal` / `discount_auto_trigger`
pattern (fresh per-tenant engine, one tenant's failure logged but never halting
the sweep). Each tick:

1. Enumerate tenant DBs from the control plane.
2. Per tenant, find `active` templates whose `next_run_on` has arrived
   (`next_run_on <= today`), up to `FEOH_RECURRING_INVOICES_MAX_PER_SWEEP` per
   tick (the per-tenant cap keeps a backlog from monopolising a tick).
3. For each, compute the `period_key`, generate the next `Invoice` (pre-coded
   from the template: vendor, amount, currency, GL / cost-center / department /
   project / PO / terms, `entity_id`), stamp `recurring_template_id` +
   `recurring_period_key`, and land it in the approval queue (status `new` →
   the normal workflow pipeline takes over).
4. Advance the template: bump `generated_count`, set `last_period_key` /
   `last_generated_at`, and roll `next_run_on` forward by one cadence step
   (nulled once it passes `end_date`).
5. Write a `recurring_template.generated` audit row (`entity_type =
   recurring_template`, `details` carries the new invoice id + period_key — no
   PII).

The generation is **idempotent on `(template, period_key)`** via the DB unique
index above, so a concurrent or retried tick is safe. `generate_one` inserts
inside a SAVEPOINT (`begin_nested`) and, on the resulting `IntegrityError`,
returns the invoice the winner already generated — the savepoint is what keeps
the losing INSERT from poisoning the sweep's tenant transaction and taking every
sibling template generated earlier in that tick down with it.

The `db.add` sits **inside** the `begin_nested()` block, not before it:
`SessionTransaction._take_snapshot` flushes the session when that boundary
opens, so a row added first is INSERTed *before* the SAVEPOINT exists and its
`IntegrityError` escapes the block meant to contain it. Same trap documented at
`card_issuance.persist_card` — see `virtual-cards.md` § *Persisting the row*.
(`POST /recurring/{id}/generate-now` also pre-checks the period, so the
sequential retry short-circuits before ever reaching the savepoint; the
savepoint is purely the concurrency backstop.)

**The sweep never moves money.** It only creates an `Invoice` in the queue; the
CFO-gated payment run is what funds it, exactly as for a manually-uploaded bill.

### Who counts as the generated invoice's creator

Segregation of duties keys on a **set** of actors, not one column
(`approval_chain.violates_segregation`), because a template can be shaped by more
than one person. `generate_one` stamps both halves of it:

| Column on the generated `Invoice` | Holds |
|---|---|
| `uploaded_by_id` | `actor_id or template.created_by_user_id` — the caller of `POST /{id}/generate-now`, else the employee who authored the template, else NULL for a template created before migration 0096 |
| `segregation_actor_ids` | `implicated_actor_ids(template, uploader_id=…)` — the template's author ∪ its `material_editor_ids`, **minus** whoever already landed in `uploaded_by_id`. `None` when that leaves nobody |

`violates_segregation` refuses an approval by the uploader **or** by anyone in
the set, so without either stamp the AP manager who clicked generate-now, or the
one whose standing instruction fires every month, could also approve the invoice
it produced.

The sweep's fallback closes what used to be a real, narrower gap. Nobody *runs*
the sweep, so there is no live actor — but an employee *authored* the template,
and that is the person segregation has to exclude. Before
`created_by_user_id` existed there was nowhere to record them, so the invoice
landed NULL and `violates_segregation` read it as "no employee created this
row": the template's author could approve the invoice their own standing
instruction had raised, exempt exactly as a legacy pre-`uploaded_by_id` row is.
`POST /api/recurring` now stamps the author and the sweep falls back to it.

**The audit row still records `actor_id` honestly** — `None` for the sweep.
The two columns answer different questions: *who performed this action* is
nobody, while *whose instruction created this payable* is the author. Only the
second is a segregation input, and conflating them would put a person in the
audit trail as having acted when they did not.

**Existing templates are deliberately not backfilled.** There is no honest
author to recover for a template written before the column, and inventing one
manufactures either a refusal (blocking an innocent approver) or an absolution
(clearing a guilty one). Those templates keep the legacy NULL reading — a
shrinking set, not a standing hole. A tenant that wants the exemption gone for
such a template can re-create it; template CRUD and invoice approval are
separately gated either way. See `../../docs/authentication.md` § Segregation of
duties.

### The editor is implicated too, which is why it is a set and not a column

Recording the author closed half the gap. An `ap_manager` who PATCHes *someone
else's* template — repointing the vendor and the amount — shapes the payable as
completely as whoever wrote it, and was recorded nowhere on it, so they could
still approve what it generated. Stamping the editor into `created_by_user_id`
instead would simply have moved the exemption to the author: no single column
holds both people, which is why the predicate and every path feeding it changed
together (migration 0097).

`PATCH /api/recurring/{id}` appends the caller to
`RecurringInvoiceTemplate.material_editor_ids` when — and only when — a
**material** field actually changes value. Material means *a term of the
generated payable*, and the classification lives in exactly one place,
`models/recurring_invoice.MATERIAL_EDIT_FIELDS`:

| | Fields | Why |
|---|---|---|
| **Material** | `vendor_id`, `amount`, `currency`, `gl_account`, `cost_center`, `department`, `project`, `po_number`, `payment_terms`, `cadence`, `day_of_period`, `start_date`, `end_date`, `entity_id` | who is paid, how much, in what, against which budget line or PO, on what schedule, and which legal entity owes it |
| **Cosmetic** (`COSMETIC_EDIT_FIELDS`) | `name`, `description`, `notes`, `variance_tolerance_pct` | a label on the template. `variance_tolerance_pct` is a detection band for *arrived* vendor invoices and never touches the invoice this template generates |

Three properties of the append matter:

- **Idempotent.** It is a set, not an append-only log — "who is implicated" is a
  membership question, and a list that grew on every save would make the column's
  size a function of how often one person edited. Stored sorted, so the JSON is
  stable and diffable.
- **It never drops an id, and never overwrites the author.** The two columns
  answer different questions and the predicate unions them.
- **Keyed off what actually changed.** A PATCH that re-sends a field with its
  existing value implicates nobody; otherwise opening a form and saving it would
  cost the saver their ability to approve.
- **Reassigns a new list rather than mutating in place.** The column is plain
  `JSONB` with no `MutableList`, so SQLAlchemy's dirty check compares object
  identity against the load-time baseline and an in-place `append` never reaches
  the database. Same trap as the `copy.deepcopy` in
  `approval_chain.advance_approval_chain`.

The `recurring_template.updated` audit row carries `material: [...]` beside
`changed: [...]`, so the trail says *which* edit widened the segregation block
rather than leaving a later reader with a wider refusal and no reason for it.

The **pause / resume / end** lifecycle endpoints implicate nobody. They change
whether the standing instruction is live, not what it instructs — resuming
someone else's template does not choose its vendor or its amount, so the second
pair of eyes the control wants is still a real one.

**Edits made before migration 0097 implicate nobody**, for the same reason
authors are not backfilled. `updated_at` records *that* a template was edited,
never who, and an older `recurring_template.updated` audit row does not say
whether the fields it lists were material under a classification that did not
exist when it was written. Every available proxy manufactures either a refusal or
an absolution, and a fraud control's inputs must be observed, not inferred.

### One template's failure never costs its siblings their work

Within a tenant the sweep mirrors `vendor_rescreen`: it selects template **ids**,
then re-reads each template by id, guards it, and **commits on its own**. The
loop used to run every template in one transaction with a single commit at the
end, so one template that raised aborted the tenant's whole tick *and discarded
the invoices already generated on it* — the identical shape fixed in
`vendor_rescreen` and `payment_erp_sync`. Re-reading by id is load-bearing: a
rollback expires the ORM objects a pre-loaded list would still hold, and
touching one from async SQLAlchemy is a `MissingGreenlet`, not a clean failure.

A template whose generation raises is counted as `template_failures` — a name
ending in `_failures`, so `sweep_health.failure_count` folds it into the sweep's
health signal (`GET /api/health/sweeps`) exactly like `vendor_rescreen`'s
`vendor_failures`.

### A skipped period is never silent

A template missing `amount` or `vendor_name` can't generate. The sweep's
defensive cursor advance rolls it past the period anyway, which is correct — a
stuck `next_run_on <= today` would re-select the same template on every tick
forever. What was missing was the other half: the template stayed `active`,
`generated_count` never moved, and the only trace was a log line, so a
subscription invoice a tenant believed was being raised every month simply
wasn't — and `GET /recurring/{id}/history` showed an empty run history
indistinguishable from "nothing due yet".

Every skip now:

1. **Persists a marker** on `RecurringInvoiceTemplate.meta.generation_skip`
   (`services/recurring_invoices.record_generation_skip`) — `reason`
   (`missing_amount` / `missing_vendor` / `missing_amount_and_vendor`),
   `period_key`, a `consecutive` count and `last_skipped_at`. Settings-JSON, so
   **no migration**. PII-free: reason codes and identifiers, never the vendor or
   the amount.
2. **Writes a `recurring_template.generation_skipped` audit row**, correlated on
   the template's own id so one template's generation problem reads as one
   thread.
3. **Surfaces as `last_skip`** on every `/api/recurring` template response, so
   the UI can tell "not generating" apart from "nothing due yet".

**Clearing the marker is not cosmetic, and every path that fixes a template
does it.** `record_generation_skip` counts *up from whatever it finds*, so a
stale count left behind after a manual fix makes the NEXT single miss trip the
three-miss auto-pause — and a healthy template goes on reporting "Not
generating" to the operator who just fixed it, which is the original bug
inverted. The rule is "clear it wherever the code has just established the
reason no longer holds":

| Path | Clears via |
|------|-----------|
| `generate_one` (the shared primitive — sweep generate *and* `generate-now`) | `clear_generation_skip`, immediately after its generatable guard passes |
| the sweep's already-generated no-op (never reaches `generate_one`) | `clear_generation_skip` |
| `POST /{id}/generate-now`'s idempotent 200 branch (short-circuits before `generate_one`) | `clear_generation_skip` |
| `PATCH /{id}` — the edit that fills in the missing field | `clear_generation_skip_if_resolved` |
| `POST /{id}/resume` — putting an auto-paused template back in service | `clear_generation_skip_if_resolved` |

The `_if_resolved` variant is for the two callers that change a template
*without* generating from it: a template resumed while still missing its vendor
**keeps** its marker, because the reason it names is still true (and it will
re-trip the pause). Leaving the clearing to the sweep alone was a real defect
caught in review — `tests/test_recurring_invoices.py` pins each row above,
including that one miss after a manual fix does NOT pause.

Past `MAX_CONSECUTIVE_SKIPS` (3) consecutive misses the sweep **pauses** the
template — the shape `services/scheduled_reports` already uses for its
auto-disable — and writes a `recurring_template.paused` row with
`actor_id = NULL`, `source: "sweep"` and the reason. An unfixable schedule stops
claiming to be live, and `POST /{id}/resume` re-anchors `next_run_on` from today
so nothing back-fires the periods it slept through.

The skip count is deliberately **not** named `*_failures`: a template missing a
vendor is a tenant configuration problem, not a broken sweep, and folding it
into `sweep_health` would leave the sweep permanently `degraded` for something
no platform operator can fix. It is surfaced per-template instead, and bounded
by the auto-pause.

`not_generatable_reason(template)` is the one condition `generate_one`, the
sweep and `POST /{id}/generate-now`'s 422 all read, so a manual generate-now can
never disagree with what the sweep decided about the same template.

`generate_recurring_invoices_once(today=…)` (or the per-tenant inner helper
`_sweep_tenant`) is callable directly for a single sweep (CLI / tests) without
the loop.

Disabled by default. Env vars:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_RECURRING_INVOICES_ENABLED` | `false` | Master switch for the generation sweep. Off in local dev/tests; flip on in deployed envs. |
| `FEOH_RECURRING_INVOICES_INTERVAL_SECONDS` | `3600` | Sweep interval. |
| `FEOH_RECURRING_INVOICES_MAX_PER_SWEEP` | `200` | Per-tick cap on invoices generated per tenant (backlog guard). |

## Variance signal

A recurring template promises a fixed amount, but the *arriving* bill from the
vendor can drift (a rate change, an extra seat, a one-off line). When an invoice
from a recurring vendor deviates from the template `amount` beyond tolerance,
it's flagged via `invoice_warnings` — **reusing the data-enrichment
price-variance approach** rather than blindly trusting the schedule. Tolerance
resolves per-template (`variance_tolerance_pct`) → falls back to the org /
platform default. The flag is advisory (a warning surfaced on the invoice), not
a hard block — the reviewer decides.

## API surface

Mounted at `/api/recurring`.

| Method + path | Purpose | RBAC |
|---------------|---------|------|
| `GET /recurring` | List (filters: `status`, `vendor_id`, `search`; paginated `page`) | read |
| `GET /recurring/summary` | Whole-set KPI rollup — `by_status` counts, per-currency **monthly-equivalent** spend (cadence normalised in exact Postgres numeric `amount`/{1,3,12}, the exact quotients summed per currency, and the SUM quantised once to `ROUND_HALF_UP` 2dp — not rounded per template, which would drop up to half a cent a row: three 100.00 annual templates are 25.00 a month, not 3 x 8.33 = 24.99. The frontend used to divide floats), and the soonest upcoming `next_run_on` across active templates. Shares `_recurring_list_filters` with the list. Groups are ordered by currency code, and each page headlines the first one with the rest on a sub-line — so which currency headlines is alphabetical, never largest-total. | read |
| `POST /recurring` | Create a template | mutate |
| `GET /recurring/{id}` | Detail | read |
| `PATCH /recurring/{id}` | Update (status excluded — the lifecycle endpoints own it) | mutate |
| `DELETE /recurring/{id}` | Delete — refused `409` once any invoice has been generated (`generated_count > 0`); pause/end instead | mutate |
| `POST /recurring/{id}/pause` | `active` → `paused` | mutate |
| `POST /recurring/{id}/resume` | `paused` → `active` | mutate |
| `POST /recurring/{id}/end` | → `ended` (terminal; nulls `next_run_on`) | mutate |
| `POST /recurring/{id}/generate-now` | Generate this period's invoice on demand (idempotent on the DB unique index — a no-op if this period already generated) | mutate |
| `GET /recurring/{id}/upcoming-schedule?count=` | Projected upcoming generations (no invoice created) — `period_key` + `run_on` + `amount`/`currency` per occurrence | read |
| `GET /recurring/{id}/history` | The invoices generated from this template (links back via `recurring_template_id`) | read |

Every template response carries `last_skip` — `null` normally, otherwise the
sweep's persisted `{reason, period_key, consecutive, last_skipped_at}` marker
for the last due period it could not generate. See § A skipped period is never
silent.

### RBAC

- **read** = `admin`, `ap_manager`, `ap_clerk`, `cfo`
- **mutate** (create / update / delete / pause / resume / end / generate-now) =
  `admin`, `ap_manager`

Every read/write is **entity-scoped** (the `X-Entity-ID` header narrows to a
subsidiary, like the other business tables).

## Audit actions

Every mutation writes an `audit_log` row via `dispatch_audit` (append-only at
the DB layer). `entity_type` is `recurring_template`:

| Action | Emitted by |
|--------|-----------|
| `recurring_template.created` | `POST /recurring` |
| `recurring_template.updated` | `PATCH /recurring/{id}` (only when fields changed) |
| `recurring_template.paused` | `POST /{id}/pause`; also the sweep's auto-pause after `MAX_CONSECUTIVE_SKIPS` non-generatable periods (`actor_id` NULL, `details.source = "sweep"`) |
| `recurring_template.generation_skipped` | the sweep, when a due period can't generate; `details` carries the reason code, period_key and consecutive count |
| `recurring_template.resumed` | `POST /{id}/resume` |
| `recurring_template.ended` | `POST /{id}/end` |
| `recurring_template.deleted` | `DELETE /recurring/{id}` |
| `recurring_template.generated` | a generated invoice (sweep or `generate-now`); `details` carries the invoice id + period_key |

Audit `details` never carry PII / banking data.

## Frontend route

`/recurring`, under the **Billing** nav group (`$lib/nav.ts`). Template CRUD via
a `<Modal>`, status `FilterChips` (`active` / `paused` / `ended`), a KPI row
(active templates, monthly committed, generated this period, next run), and — in
the detail modal — an **upcoming-schedule preview** (from
`GET /{id}/upcoming-schedule`) and the **generated-invoice history** (from
`GET /{id}/history`). Read roles see the page; mutate actions are gated to
admin / ap_manager (`auth.isManager`). See `frontend/CLAUDE.md`.

## Migration

- **0046_recurring_invoices** — creates `recurring_invoice_templates` and adds
  `invoices.recurring_template_id` + `invoices.recurring_period_key` plus the
  partial unique index `uq_invoice_recurring_period`. Tenant DB only (gated on
  the `invoices` table → no-ops on the control plane, fans out to every tenant
  via `scripts/migrate_all_tenants.py`). Idempotent (`CREATE TABLE/INDEX IF NOT
  EXISTS`, `ADD COLUMN IF NOT EXISTS`). Mirrors the ORM model exactly so a fresh
  tenant built by `tenant_provisioning._create_tenant_tables` (`create_all`)
  matches a migrated one.

## Local-first

No new external dependency and no new `pnpm` script. The generation sweep is
disabled by default (`FEOH_RECURRING_INVOICES_ENABLED=false`), so `pnpm dev` runs
the whole feature — template CRUD, upcoming-schedule preview, generated-invoice
history, and on-demand `generate-now` — with no background loop and no cloud
credential. Flip the switch on in deployed envs to enable scheduled generation.

## Seed data

`scripts/seed_extras.py` adds a couple of idempotent recurring templates per
tenant (a monthly SaaS subscription, a monthly office lease, a quarterly
service, plus a paused one) so the `/recurring` page isn't empty on a freshly
seeded tenant. Additive + skip-if-exists, like the rest of `seed_extras`.

## Tests

- `frontend/tests-e2e/recurring/recurring.spec.ts` — UI list render + KPIs +
  status filter chips; an API-driven template create surfaced in the table;
  open a template, view its upcoming-schedule preview, trigger `generate-now`,
  and confirm the generated invoice appears in the history (or the invoice
  queue). Selectors are by accessible name / aria-label / `data-testid` — no
  brittle CSS, no arbitrary sleeps.
- Backend `pytest` covers the model, the schemas, the router (CRUD + lifecycle +
  the `409`-once-generated delete guard), the period_key derivation, and the
  idempotency invariant (a double generate for one period creates exactly one
  invoice). (Owned by the backend worker.)
</content>
</invoke>
