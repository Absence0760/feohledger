# Vendor Risk & Sanctions Screening

Screens vendors against sanctions / PEP / adverse-media lists, blocks payments
to sanctioned entities, re-screens on a schedule, and rolls sanctions + fraud +
payment-history signals into a composite vendor risk score. Roadmap:
*Priority — Sanctions & Vendor Risk Screening*.

Local-first: the default `mock` sanctions adapter returns `clear` for everything
that isn't an obvious fixture, so a fresh clone screens vendors with no cloud
account and no key. Deployed envs point
`Organization.settings.compliance.sanctions.provider` at a real provider.

## Pieces

| Concern | Where |
|---|---|
| Screening providers (adapters) | `services/sanctions_adapters/` — `mock`, `complyadvantage`, `dowjones`, `refinitiv` |
| Hit-category taxonomy (incl. adverse media) | `services/sanctions_categories.py` |
| Screen-one-vendor primitive | `services/vendor_screening.py::screen_vendor_record` |
| Screen on create / update + manual re-screen + review queue + block | `api/vendors.py` |
| Pre-payment gate (refuse blocked vendors + per-payment screen) | `services/compliance.py::check_payment_compliance` |
| Periodic re-screen sweep | `services/vendor_rescreen.py` (background loop) |
| Composite risk scoring | `services/vendor_risk_scoring.py` + `api/vendor_risk.py` |
| Screening trail (append-only) | `sanctions_checks` table (`models/sanctions_check.py`) |
| Denormalised current state | `vendors` columns (migration 0042) |

## Data model

`sanctions_checks` (migration 0018) is the append-only trail — one row per
screen, with `check_type` (`initial` | `periodic` | `manual` | `pre_payment`),
`result` (`clear` | `match` | `review_required`), `risk_score`, `matched_list`
(the list NAME), and `raw_response` (JSONB; raw provider match details live
**only** here, never in logs / HTTP bodies — invariant #7).

### Hit categories (and adverse media)

A provider reports not just a verdict but **what kind of hit** it was. The
adapters normalise their own vocabulary into a fixed taxonomy on
`ScreeningResult.categories` — `sanctions` / `pep` / `adverse_media` /
`high_risk_country`:

| Adapter | Where the taxonomy comes from |
|---|---|
| `mock` | simulated for the fixture names in `_DEFAULT_ADVERSE_MEDIA`, overridable via `compliance.sanctions.mock_adverse_media` |
| `refinitiv` | World-Check `categories[].name`; `ADVERSE-MEDIA` → `adverse_media` |
| `dowjones` | `match-type`; `adverse-media` → `adverse_media` |
| `complyadvantage` | the hit `doc.types` set it already computes for the verdict. It **asks** for `adverse-media` in `filters.types` — a control that never requests the signal it claims to screen for is a false assurance — and carries an unmapped CA type (`warning`, `fitness-probity`) through with hyphens normalised rather than dropping it |

Requesting adverse media widens what comes back to `review_required`, never to
a block: the verdict for a non-`sanction` hit is unchanged, and an adverse-media
hit means *review the relationship*.

**Adverse media is negative-news screening**: press coverage of fraud or
corruption that has not reached a formal watchlist. It is a different
instruction to a reviewer than a list match — *review the relationship*, not
*stop the payment* — which is exactly why the bare verdict alone was not enough.

`services/sanctions_categories.py` owns the persisted half, and all three
consumers go through it so they cannot drift:

| Consumer | What the taxonomy does there |
|---|---|
| `compliance.check_payment_compliance` | adds its own `ComplianceDecision` reason on top of the verdict — on `review_required`, on `match`, **and on `clear`**, where the extra reason turns the verdict into a `hold`. A provider that reports negative news on a counterparty not yet on any list must not be auto-allowed. |
| `vendor_screening.screen_vendor_record` | folds the labels into the persisted `sanctions_checks` row and names them in the PII-free `vendor.screened` audit row; `ScreenOutcome.categories` / `.adverse_media` carry them to the caller. |
| `vendor_risk_scoring` | reads them back off the persisted row (it is compute-on-read and never calls an adapter), floors an adverse-media hit above a bare `review_required`, and names it in `Vendor.risk_factors.sanctions`. |

**No migration.** The labels ride `sanctions_checks.raw_response` under the
reserved key `_screening_categories` — the column is already JSONB, the
taxonomy is small and additive, and a dedicated column would fan a schema
change out to every tenant DB to store an enum list. The merge never mutates
the provider's own payload (an auditor still replays exactly what was
returned), and a `clear` screen's payload is left byte-identical. Reads are
tolerant: a row written before the taxonomy shipped, or one whose JSONB holds
something unexpected, reads as "no categories" rather than raising — a
screening-trail row must never be able to 500 the risk endpoint.

**The labels are PII-free by construction** — a fixed vocabulary, never
provider free text — which is what makes them safe on an audit row, in an API
response (`SanctionsCheckResponse.categories` / `.adverse_media`,
`ScreeningReviewItem.latest_categories` / `.adverse_media`) and in a UI badge,
while `raw_response`'s provider payload stays confined to the JSONB column and
is never serialized out (invariant #7).

Migration 0042 denormalises the current state onto `vendors`:

| Column | Meaning |
|---|---|
| `screening_status` | `unscreened` \| `clear` \| `review` \| `match` (mirrors the latest check) |
| `last_screened_at` | timestamp of the most recent screen |
| `payments_blocked` | hard payment block (sanctions match or manual) |
| `payments_blocked_reason` / `payments_blocked_at` | why / when (reason carries the list NAME only) |
| `risk_score` | composite 0–100 (`Numeric(5,2)`) |
| `risk_level` | `low` \| `medium` \| `high` \| `critical` \| `unknown` |
| `risk_factors` | per-signal breakdown JSONB (counts / scores / list NAMES only) |
| `risk_scored_at` | when risk was last computed |

## Screening flow

`vendor_screening.screen_vendor_record(db, *, vendor, organization_id,
org_settings, check_type, actor_id=None, correlation_id=None,
sanctions_adapter=None)` is the single primitive. It runs the adapter, appends a
`sanctions_checks` row, denormalises `screening_status` / `last_screened_at`,
sets `payments_blocked` on a `match`, and writes a PII-free `vendor.screened`
audit row. It mutates the session but does **not** commit — the caller owns the
transaction (mirrors `check_payment_compliance`). Returns a `ScreenOutcome`.

Call sites:

- **Vendor create / update** (`api/vendors.py`) — `check_type="initial"`. Update
  re-screens only when an identity field changed (`name`, `tax_id`,
  `bank_details.country`, beneficial owners). Best-effort: a screening failure
  never blocks the vendor write. Gated by `FEOH_VENDOR_SCREENING_ENABLED`.
- **Other tax-ID writers** — `PATCH /api/tax/vendors/{id}/w9`, the
  `POST /api/tax/vendors/{id}/tin-verify` override, and ERP vendor sync
  (`services/vendor_sync.py`, for an existing row whose `name` or `tax_id` the
  ERP changed) — `check_type="initial"`, best-effort. Every `tax_id` write goes
  through `services/vendor_tax_id.rekey_tax_id`, which reports whether the
  number actually changed; that, not the field's presence in the request, is
  what triggers the screen. ERP-*created* rows are not screened inline: they
  start with a NULL `last_screened_at`, so the periodic sweep picks them up on
  its first tick, and the pre-payment screen backstops them before any money
  moves.
- **Manual re-screen** — `POST /api/vendors/{id}/screen` (`check_type="manual"`).
- **Approved bank-detail change** (`api/vendors.py::approve_change_request`) —
  `check_type="bank_change"`. The dual-control gate catches the *approval* of a
  staged `bank_details` change (the BEC / bank-redirect fraud tail); once the new
  coordinates are applied, the vendor is re-screened against them (so a redirect
  to a high-risk jurisdiction surfaces as `review`, and a name that has since
  landed on a list hard-blocks) **and** every in-queue payable invoice for the
  vendor gets a de-duped `fraud_flag` payment hold. Best-effort — a screening
  failure never blocks applying the approved change. Gated by
  `FEOH_VENDOR_SCREENING_ENABLED`.

  **"Dual control" here means two humans, and that now includes the
  portal-submitted case.** The approve path's segregation check compared only
  `requested_by_user_id`, which is NULL for every supplier-portal submission —
  so an `ap_manager` (who holds `vendor.manage`, `vendor.bank_change.approve`
  and `payment.execute` by default) could invite a portal user at an address
  they controlled, sign in as it, stage the redirect and approve it alone. It
  now also refuses when `requester_provisioned_by_user_id` — frozen at staging
  from `vendor_users.provisioned_by_user_id` — equals the approver. The
  `fraud_flag` above is a *signal*, not the second control: exception
  resolution has no segregation check of its own. See
  [`supplier-portal.md`](supplier-portal.md) § Credential provenance and the BEC
  dual control.
- **Approved tax-ID change** (`api/vendors.py::approve_change_request`) —
  `check_type="tax_id_change"`. `tax_id` is an identity field the adapters
  screen on (`vendor_tax_id`), so applying a staged one re-screens exactly as an
  AP edit of the field does. It used to apply silently, which made the
  supplier-portal change request the one route that could re-key a vendor's
  identity with no sanctions check. Best-effort, gated by
  `FEOH_VENDOR_SCREENING_ENABLED`.
- **Periodic sweep** — `services/vendor_rescreen.py` (`check_type="periodic"`).
- **Pre-payment** — `check_payment_compliance` keeps its own
  `check_type="pre_payment"` screen (different verdict contract).

## Blocking payments

`check_payment_compliance` refuses a payment up front when
`vendor.payments_blocked` is set — before FX lock and before any payment
adapter is touched. A live sanctions `match` (from the pre-payment screen or a
prior screen) keeps the vendor blocked until an admin explicitly unblocks via
`POST /api/vendors/{id}/unblock`. `POST /api/vendors/{id}/block` is the manual
override.

## Review queue

`GET /api/vendors/screening/review-queue` lists vendors whose
`screening_status` is `match` or `review` (backed by the partial index on
`sanctions_checks(result)` + the `vendors(screening_status)` index).
`GET /api/vendors/{id}/screening-history` returns the full trail for one vendor.

**The queue is paginated** on the canonical `page` / `page_size` envelope
(`items` / `total` / `page` / `page_size`), ordered `last_screened_at DESC NULLS
LAST, id ASC`. It used to return a bare unbounded list, which cost one extra
`sanctions_checks` lookup PER flagged vendor with no ceiling. The `.id`
tie-break is load-bearing: `last_screened_at` is not unique (a bulk re-screen
stamps a whole batch inside one transaction, and it is NULL for a never-screened
vendor), so without it Postgres may order equal-keyed rows differently between
the `offset=0` and `offset=N` queries and a vendor is duplicated onto two pages
or skipped entirely.

**The page's headline counts do not come from the queue.** "Sanctions matches"
and "Needs review" were derived by filtering the LOADED queue, which was correct
only while the endpoint returned every row AND was selected on exactly those two
statuses — a construction accident, not a stated property, and paginating would
have turned both into silent page-scoped undercounts. They now read
`by_screening_status` from `GET /api/vendors/counts`, computed on the SAME
single aggregate pass as `by_status` and `payments_blocked` so the three can
never describe differently-filtered populations. Same rule as the "Payments
blocked" KPI before it: a tally has to come from a query that asks the tally's
own question (`docs/decisions.md` §48).

`/api/vendors/counts` is `admin` / `ap_manager` / `cfo` while the queue also
admits `ap_clerk`, so a clerk sees an em-dash for all three KPIs rather than a
number. That is deliberate and unchanged from how the blocked tally already
behaved: with the queue paginated, a clerk-side client derivation would be a
page-scoped lie, and "we don't know" beats a wrong figure.

**Frontend surface.** The dedicated review-queue page lives at
`frontend/src/routes/vendors/screening/+page.svelte` (sidebar link
**Screening**, admin / ap_manager / ap_clerk / cfo — the same four roles
`screening_review_queue` accepts). It lists the flagged
vendors with their screening pill (`ui/ScreeningBadge.svelte`), risk score, and
last-screened date over `getScreeningReviewQueue()`, and a detail modal opens
the per-vendor screening-history timeline plus **block / unblock** and
**re-screen** actions. An adverse-media hit renders as an amber **Negative
news** pill on the same `ScreeningBadge` (a `adverseMedia` prop reusing the
existing calibrated tone — no new colour), reads as a *Hit categories* row in
the detail modal, appears on each history entry, and is searchable (the queue's
client-side filter matches the formatted category labels, so typing "negative
news" narrows to them). Block/unblock is gated in the UI on the granular
permission `vendor.block` (`auth.can('vendor.block')`) — not a role check — so
the control is hidden for a non-holder; re-screen is gated on
`auth.isManager`. The backend enforces both regardless. (The vendor LIST page
`/vendors` also surfaces the same screening/risk actions per-vendor via
`VendorModal`.)

### The "Payments blocked" KPI is not derived from the queue

The page's KPI row carries three whole-set figures. Two of them — *Sanctions
matches* and *Needs review* — are counted off `items`, which is legitimate:
the review queue is unpaginated and is **exactly** `screening_status IN
('match','review')`, so the array IS the population those two describe. (If
that endpoint ever paginates, both become the same defect described below.)

*Payments blocked* is not, and cannot be. `POST /api/vendors/{id}/block` sets
`Vendor.payments_blocked` and deliberately never touches `screening_status`, so
a vendor AP blocks while it is screening-`clear` belongs to no bucket the queue
selects on. Counting `items.filter((it) => it.payments_blocked)` therefore could
not see it — not past a page boundary, but *ever*, at any queue size. A headline
claiming to count blocked payments read `0` while payments were blocked.

The tally now comes from a query that asks the tally's own question:
`GET /api/vendors/counts` returns `payments_blocked` alongside `by_status`,
computed as `count(*) FILTER (WHERE payments_blocked)` **inside the same
aggregate** as the status buckets. That is what makes it un-driftable — it
shares the one `_vendor_list_filters` call and the one `apply_entity_scope`
call with the buckets, so a filter can never apply to one and not the other,
and a subsidiary's blocks can never leak into a sibling's figure
(`docs/decisions.md` §48).

Three consequences worth knowing:

* `payments_blocked` is an **orthogonal axis, not a slice of `by_status`**. A
  blocked vendor is still `active` (or `unverified`, …) and is already counted
  in its own bucket, so `total` stays `sum(by_status)` and the blocked figure
  must never be added to it.
* The figure spans **all vendors, not the queue**, and the card says so in its
  sub-label. The page's search box is a client-side filter over the queue rows
  and matches different columns (matched list, hit categories) than the API's
  `search` does (name / code / email), so wiring one to the other would make the
  KPI claim to describe a search it does not.
* `GET /api/vendors/counts` is gated exactly like `GET /api/vendors`
  (admin / ap_manager / cfo) because §48 requires a tally's RBAC to match its
  list's in both directions. The screening queue admits `ap_clerk` as well, so
  for a clerk the call 403s and the card renders an em-dash plus *Count
  unavailable*. It deliberately does **not** latch back to the queue-derived
  number — that number is the bug, and showing it to exactly one role would
  reinstate the defect where it is hardest to notice.

Guards: `backend/tests/test_vendor_blocked_counts.py` (the tally, its filters,
its entity scope and its RBAC) and
`frontend/tests-e2e/vendors/screening-blocked-kpi.spec.ts` (the KPI renders the
endpoint's figure over a queue in which nothing is blocked, and renders the
unavailable state rather than a wrong number on a 403).

## Bulk operations and list sort (issue #328)

`GET /api/vendors` accepts `sort=name|code|status|created_at&order=asc|desc`
(`api/sorting.py`'s shared allowlist — an out-of-list value is a 422, never
silently ignored) with `.id` always appended as the final tie-break so
OFFSET/LIMIT pagination stays deterministic regardless of which column is
picked. The frontend's `SortableHeader` persists the choice to the URL
(`?sort=&order=`).

`/vendors` was, along with `/contracts`, one of the two primary list pages
shipping zero bulk actions. It now mirrors the invoices/expenses bulk shape:

- `GET /api/vendors/ids` — every vendor id matching the caller's list
  filters (capped at `MAX_SELECT_ALL_IDS`), backing "select all N matching"
  the same way `GET /api/invoices/ids` does.
- `POST /api/vendors/bulk/status` (`{ids, status: "active"|"rejected"}`,
  `vendor.manage`) — bulk verify/reject, routed through the identical status
  writes + audit actions (`vendor.verified`/`vendor.rejected`) the
  single-row `POST /{id}/verify`/`/reject` endpoints use. Each id is
  resolved independently; a vendor outside the legal starting status for
  the target, or an id that doesn't resolve, is skipped-and-reported
  (`{updated, skipped: [{id, reason}]}`) rather than failing the whole
  batch — the same partial-success contract as
  `api/invoices.py::bulk_status_change`.
- `POST /api/vendors/bulk/screen` (`{ids}`, admin/ap_manager) — bulk
  re-screen against the configured sanctions provider (same
  `screen_vendor_record` call as the single-row `POST /{id}/screen`); a
  per-vendor provider failure is skipped-and-reported rather than aborting
  the rest of the batch.
- `POST /api/vendors/bulk/export` (`{ids}`) — CSV of the selection.
  Deliberately narrow columns (name/code/email/phone/status/source/
  created_at) — never `bank_details` or the raw `tax_id`.

See `backend/tests/test_vendor_bulk_ops.py` and
`backend/tests/test_list_sorting.py`.

## Periodic re-screening

`services/vendor_rescreen.py` is a background loop (same shape as
`contract_renewal`): every `FEOH_VENDOR_RESCREEN_INTERVAL_SECONDS` it sweeps every
tenant, re-screens active vendors whose `last_screened_at` is NULL or older than
`FEOH_VENDOR_RESCREEN_AFTER_DAYS`, and notifies AP managers when a vendor newly
flips to `match` / `review`. Disabled by default (`FEOH_VENDOR_RESCREEN_ENABLED`).

**Each vendor is screened and committed on its own**, and a vendor whose screen
raises is logged (exception CLASS only), counted in `RescreenResult.vendor_failures`,
and skipped — the tenant's remaining vendors carry on.

That is a correctness requirement, not a nicety. The loop previously had no
per-vendor guard and committed once per tenant, and `screen_vendor_record` has
no internal `except`. So one vendor whose screen raised (adapter transport
error, a bad credential, one malformed row) aborted the whole tenant sweep
*before* the commit: not even the vendors already screened on that tick got
their `last_screened_at` advanced. Every one of them stayed due, the same poison
vendor was re-selected on the next tick, and the sweep made **zero progress
forever** — a sanctions-compliance control silently not running, with nothing
surfacing staleness (`GET /api/vendors/screening/review-queue` only *orders* by
`last_screened_at`). `vendor_failures` is counted apart from `failures`
(whole-tenant sweep aborts) so the two can't be confused in the sweep's log line.

## Risk scoring

`services/vendor_risk_scoring.py` blends three PII-free signals into a 0–100
composite + a bucket: **sanctions** (latest check result / score / hit
categories), **fraud** (open `fraud_flag` exceptions on the vendor's invoices),
and **payment history** (trailing volume / count / failed-or-voided runs).
Compute-on-demand, no external calls.

The sanctions sub-score reads the persisted row's hit categories (above). An
**adverse-media** hit floors the sub-score at `_ADVERSE_MEDIA_FLOOR` (65),
above the `_REVIEW_FLOOR` (60) a category-less `review_required` scores — the
`mock` adapter scores negative news 50, so without the floor it would rank
*below* a generic jurisdiction flag, inverting the two signals. The floor
applies outside the `review_required` branch so a `clear` verdict carrying
negative news still moves the score, matching the compliance gate, which holds
that payment. A `match` still dominates at 100. Rows written before the
taxonomy shipped score exactly as they did before.

### Payment volume is a reporting-currency figure

The payment-history signal ramps its exposure sub-score linearly to 100 at
`_VOLUME_FULL_EXPOSURE` (100,000) — **a bare number in the org's reporting
currency**. `Payment.amount`, however, is denominated in the *invoice's*
currency (`international_payments.prepare_international_payment` sets
`amount=invoice.amount` and puts the home-currency debit on `source_amount` /
`source_currency`), so a raw `SUM` mixed currencies: one ordinary
¥10,000,000 invoice added 10,000,000 to a USD ramp and pinned the sub-score at
100. Stacked on a `review_required` screen that read 48/`medium` where the
truthful score is 33/`low`.

`_payment_history` therefore resolves each payment through the SAME
`currency_conversion.payment_reporting_amount_sql` the 1099 report uses — the
locked `source_amount` when the payment carries a home-currency leg, else
`amount` when the invoice is already in the reporting currency. A payment
neither rung can establish is left OUT of the volume and counted on
`risk_factors.payment_history.unconverted_payments`; it still counts toward
`payment_count`, so the vendor never reads as untouched. Nothing is converted
at read time — a rate fetched on a read would make the score move under the
reader (`../../docs/decisions.md` §18). The factor breakdown carries the
`currency` alongside the figure so the comparison is legible rather than
implicitly USD.

`GET /api/vendors/{id}/risk` reads the persisted score;
`POST /api/vendors/{id}/risk/recompute` recomputes + persists (it resolves the
org's reporting currency from `get_tenant`'s `Organization.settings`);
`GET /api/vendors/risk/summary` returns the org-wide distribution by bucket.

## Providers

`mock` (local-first default), `complyadvantage`, `dowjones`, `refinitiv`.
Selected per-org via `Organization.settings.compliance.sanctions.provider`;
real keys live in sops, never committed. Adverse-media and other hit kinds
surface through `ScreeningResult.categories` — see
[Hit categories](#hit-categories-and-adverse-media) for how they reach the
verdict, the trail and the risk score.

### A named provider we have no adapter for fails closed

`get_sanctions_adapter` resolves `mock` only when **nothing** is configured
(the local-first default). A `provider` that names an adapter this deployment
doesn't have raises `UnknownSanctionsProviderError` — it is never substituted
with `mock`, which clears every name outside its own three-entry fixture list.
One typo (`"worldcheck"` for the registry's `refinitiv`) used to screen a whole
tenant's vendor book against nothing and record `clear` / risk 0, on the
control that exists to keep money away from a sanctioned party. The dispatcher
docstring claimed a compensating warning surfaced by the compliance service;
no such code existed. Same call as `erp_adapters` / `payment_adapters` /
`fx_adapters` / `financing_adapters`.

Both consumers absorb the raise rather than 500:

| Consumer | Behaviour on an unresolvable provider |
|---|---|
| `compliance.check_payment_compliance` | returns `hold` with the reason `sanctions screening could not run: no adapter for configured provider '<name>'`. The payment waits in `pending_compliance` and the caller opens the usual `payment_compliance_hold` exception. Never `allow`. |
| `vendor_screening.screen_vendor_record` | writes a `sanctions_checks` row with `provider="unconfigured"`, `result="review_required"`, `matched_list="provider_not_configured"` (the requested name rides `raw_response`, PII-free), and denormalises `vendors.screening_status="review"`. The vendor lands on `GET /api/vendors/screening/review-queue` instead of reading `clear`. No payment block is set — a misconfiguration is not a match. |

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `FEOH_VENDOR_SCREENING_ENABLED` | `true` | Screen on vendor create / update (mock-safe, local-first). |
| `FEOH_VENDOR_RESCREEN_ENABLED` | `false` | Master switch for the periodic re-screen sweep. |
| `FEOH_VENDOR_RESCREEN_INTERVAL_SECONDS` | `86400` | Sweep interval. |
| `FEOH_VENDOR_RESCREEN_AFTER_DAYS` | `7` | Re-screen vendors whose last screen is older than this. |
