# Multi-currency reporting

How the app rolls multi-currency invoices up into one **reporting (base)
currency** for analytics + dashboards, and how it tracks **unrealized** FX
gain/loss on open foreign-currency invoices.

This sits **on top of** the payment-level FX machinery — it does not replace it:

| Layer | Lives in | What it does | When the rate is locked |
|---|---|---|---|
| **Payment FX** (existing) | `services/international_payments.py`, `services/fx_adapters/` | Locks an FX rate on the `Payment` row at submission; computes **realized** gain/loss (`realized_fx_gain_loss_for_settlement`) when a foreign invoice settles, and records it on the settlement audit row. | At payment submission |
| **Reporting FX** (this doc) | `services/currency_conversion.py` | Converts each invoice's `amount` into the org reporting currency, materializes the rate on the invoice row, rolls multi-currency volume into one total, computes **unrealized** gain/loss on open invoices. | When the invoice is created / mutated |
| **Expense FX** | `services/expense_currency.py` | Converts each expense line into its **report's** currency (`expenses.converted_*`) so a report total isn't a cross-currency sum, and the report total into the **org reporting** currency (`expense_reports.reporting_*`) so the CFO threshold compares like with like. | Line: on attach / amount-or-currency edit / report-currency change. Report: at submit |
| **Expense-policy thresholds** | `services/expense_policy.py` | Compares an expense against a policy's money thresholds **in the policy's `threshold_currency`**, so a €200 expense isn't judged against a USD 100 limit as bare numbers. | Never — it locks nothing. It **reuses** the line's existing lock (row above) and fails closed when none bridges the two currencies. |
| **Payment CFO threshold** | `services/payment_controls.py` (`cfo_approval_decision`) | Expresses a payment run's / standalone payment's credit-netted total in the **org reporting** currency before comparing it against `settings.payments.cfo_approval_above`, so a GBP 9,000 run (USD 11,400) can't slip under a USD 10,000 gate. | Never — it locks nothing. It **reuses** the invoice's existing reporting lock via `reporting_amount_at_locked_rate` and fails closed when none proves the row's currency pair. See `payments.md` § The threshold is denominated in the org's REPORTING currency. |

Same three rules across every layer that locks a rate — **`Decimal` only**,
**rate locked and persisted on the row**, **never re-fetched at read time**. See
`international-payments.md` for the payment side and `expense-management.md`
§ Multi-currency reports for the expense side.

The fourth row is the exception that proves the rule: an expense **policy** is a
standing rule, not a transaction, so there is no honest moment at which to lock
a rate onto it (one locked when the rule was written would be stale for every
expense it ever judges). It therefore locks nothing and never calls an FX
provider — it re-expresses the expense using the conversion a write path already
locked onto the row, and when nothing bridges the two currencies it treats the
threshold as **breached** rather than guessing. Details + the per-threshold
fail-closed table: `expense-management.md` § Threshold currency.

## Reporting (base) currency

Every org has one reporting currency. It is resolved by
`currency_conversion.resolve_reporting_currency(org.settings)` in this order:

1. `Organization.settings.reporting_currency` — the explicit setting.
2. `Organization.settings.payments.home_currency` — legacy field the payment
   path already reads (so existing orgs keep working with no settings change).
   Read via `international_payments.configured_home_currency`, the one owner of
   that setting, which answers `None` for an unset value so this rung falls
   through to 3 and 4 rather than short-circuiting them with the platform
   default. See `international-payments.md` § The org's home currency has ONE
   normaliser.
3. `Organization.settings.invoice_defaults.currency`.
4. `FEOH_REPORTING_CURRENCY_DEFAULT` (config default `USD`) — platform last resort.

The result is always an uppercase ISO 4217 code; a misconfigured org degrades to
the default rather than 500-ing a dashboard.

### Setting it

`PATCH /api/organization` with `{"settings": {"reporting_currency": "EUR"}}`
(admin-only, merged into existing settings).

### Reading it — server-resolved, every role

`GET /api/organization` serves the already-resolved code as its own top-level
field, `resolved_reporting_currency` — the exact output of this function, all
four rungs included. It sits outside the role-projected `settings` block (see
`services/org_settings_view.py`), so every role gets the same answer, admin or
not; there is nothing here a non-admin projection needs to strip, since the
first three rungs are already admitted to every role by
`NON_ADMIN_SETTINGS` for the client-side fallback described next.

Before this field existed, a client could only re-derive the first three rungs
from `settings` — the fourth, `FEOH_REPORTING_CURRENCY_DEFAULT`, is operator
config no client can read — so an org configured only through that operator
default left the web `orgCurrency` store and mobile's `OrgCurrencyStore`
answering `null` even though the server denominated every rollup in a known
code (`docs/decisions.md` §119, §160, §200, followups (c)). Both stores now
read `resolved_reporting_currency` first and keep their own three-rung
resolution only as a fallback for a cached or pre-upgrade response that omits
the field.

## Materialized conversion on the invoice

Four nullable columns persist the conversion **at the time it is computed** so a
later market move never silently rewrites historical reporting numbers
(migration `0025_multicurrency`, tenant DBs only):

| Column | Type | Meaning |
|---|---|---|
| `invoices.reporting_currency` | `VARCHAR(3)` | Currency the row was converted into (snapshotted). |
| `invoices.reporting_amount` | `NUMERIC(15, 2)` | `amount` expressed in `reporting_currency`. |
| `invoices.reporting_fx_rate` | `NUMERIC(18, 8)` | Rate applied (invoice currency → reporting currency); `1` for same-currency. |
| `invoices.reporting_source_currency` | `VARCHAR(3)` | **Which** currency the rate was fetched FOR (migration 0086). `NULL` on a row locked before it. |
| `invoices.reporting_fx_locked_at` | `TIMESTAMPTZ` | When the rate was locked. |

These are written by `currency_conversion.materialize_reporting_amount(...)`,
invoked best-effort from `invoice_warnings.refresh_warnings` — the existing
chokepoint that already runs after every extraction and on every invoice
mutation. Behaviour:

- **Same currency** → rate `1`, no FX adapter call, `reporting_amount == amount`.
- **Idempotent** → a row whose lock still *describes it* is left untouched; the
  locked rate is **never** re-fetched for such a row (historical stability).
- **The lock must keep describing the row.** `amount` and `currency` are both
  editable on `PATCH /api/invoices/{id}` right up to approval, and
  `refresh_warnings` re-runs materialization afterwards — so the persisted
  set can stop matching the row it was derived from. Two checks
  (`_lock_is_self_consistent`), both answerable from the row alone:
  - *the rate still describes this row's currency pair* (`_lock_pair_matches`).
    Exact when `reporting_source_currency` is set: it records **which** currency
    the rate was fetched for, so ANY currency edit — including one between two
    foreign currencies — is caught. On a row locked before migration 0086 the
    column is `NULL` and it falls back to the old inferential heuristic (a
    same-currency lock is exactly `1`, a cross-currency lock is not), which sees
    a currency edit that *crosses* the reporting currency but not one between
    two foreign ones. That residual blind spot exists on legacy rows only and
    closes the first time such a row re-materializes.
  - *the figure reconciles* — `amount × rate == reporting_amount`. If only the
    amount moved, the row is **re-scaled at the already-locked rate, with no FX
    call**: the liability was accrued at the booking rate, and correcting a
    mis-extracted figure does not retroactively re-price it. (It also means an
    amount correction fixes the reporting number even while the FX provider is
    down.)

  It re-locks (fresh rate) when the org's reporting currency changes, when the
  row was never materialized, or on the currency-pair mismatch above.

  **Why a column and not more inference.** `EUR → GBP` on a USD-reporting org
  with the amount unchanged passes both inferential checks — the figure still
  reconciles and the rate still *looks* like a cross-currency rate — so the
  stale EUR-derived figure kept being reported as a converted, trustworthy
  number. Migration 0086 is deliberately **not backfilled**: `currency` is the
  invoice's currency *now*, so copying it in would assert the rate was fetched
  for the current pair, which is precisely the unevidenced claim the column
  exists to prevent.
- **Fail-soft** → an FX outage leaves the columns `NULL` and never blocks saving
  the invoice. Rollups treat a `NULL` reporting amount as "not yet converted"
  (see below).

Money is always `Decimal` / `Numeric` and quantizes to 2 dp (`ROUND_HALF_UP`);
the rate to 8 dp. Never `float`. The persisted lock is **self-consistent by
construction** — `reporting_amount` is derived from the *stored* (8 dp) rate,
not the provider's full-precision one — so an auditor can re-derive it and the
staleness check above can rely on the identity.

## Roll-up into the aggregates

`currency_conversion.rollup_to_reporting_currency(rows, reporting_currency=...)`
collapses a list of currency-tagged amount rows into one reporting total + a
per-currency breakdown, **without** any FX call:

- Rows with a rate-locked `reporting_amount` matching the current reporting
  currency use that locked figure (stable against market moves).
- Same-currency rows convert 1:1.
- Foreign rows with no usable lock fall back to face value and are counted in
  `unconverted_count`, so the UI can surface "N rows pending conversion" instead
  of silently mixing currencies.

Surfaced in:

- **`GET /api/dashboard`** → `reporting` block: `reporting_currency`,
  `total_amount`, `total_count`, `unconverted_count`, `by_currency[]`. The legacy
  `total_amount` (naive cross-currency `SUM`) is unchanged for back-compat;
  `reporting.total_amount` is the figure to trust for a multi-currency book.
  The same pattern now also covers the aging buckets (`aging_reporting`,
  alongside the legacy `aging`), the monthly trend
  (`monthly_trend[].reporting_amount`, alongside `.amount`), and the upcoming-7-days
  total (`upcoming_total_amount_reporting` + `upcoming_unconverted_count`,
  alongside `upcoming_total_amount`) — all previously naive cross-currency
  `SUM(Invoice.amount)`s with no converted counterpart at all.
  `total_paid` / `total_pending` gain `total_paid_reporting` /
  `total_pending_reporting` (+ their own `*_unconverted_count`), resolved via
  `payment_reporting_amount_sql` (below) rather than a raw `SUM(Payment.amount)`
  — the same resolver `GET /api/payments/summary` already used for its own
  `total_paid`/`total_pending`.
  **The dashboard's unconverted counts follow two opposite rules**, so a client
  must never word them as one: `reporting.unconverted_count`,
  `aging_reporting.unconverted_count` and `upcoming_unconverted_count` count
  rows added at FACE value (`invoice_reporting_amount_sql` /
  `reporting_amount_for_row` fall back), so those figures mix currencies; the
  two payment counts count rows LEFT OUT (`payment_reporting_amount_sql`
  refuses to fall back), so Paid / Pending are floors. The web KPI row renders
  one line per rule (`docs/decisions.md` §200).
- **`GET /api/analytics/cfo`** → `reporting_spend` block (same shape, scoped to
  the period window) plus `unrealized_fx` (below). `accounts_payable_balance`
  gains a `reporting_accounts_payable_balance` block (same shape as
  `reporting_spend`) — the naive cross-currency AP-balance `SUM` had no
  converted counterpart even after `reporting_spend` was added beside
  `total_spend`. `avg_daily_outflow` / `working_capital_impact_5_days` gain
  `reporting_avg_daily_outflow` / `reporting_working_capital_impact_5_days` (+
  `reporting_avg_daily_outflow_unconverted_count`), resolved from
  `payment_reporting_amount_sql` rather than a raw `SUM(Payment.amount)` over
  the period's completed payments.
- **`GET /api/analytics/by-entity`** → each entity row (and the consolidated
  row) gains `reporting_outstanding_amount` / `reporting_currency` /
  `reporting_outstanding_unconverted_count` beside the legacy naive
  `outstanding_amount`, using the same rollup `reporting_accounts_payable_balance`
  uses so the two can't disagree for the consolidated row — and
  `reporting_total_spend` / `reporting_total_spend_unconverted_count` beside the
  naive `total_spend`, the same population and rollup as `/cfo`'s
  `reporting_spend`. `open_po_amount` has no counterpart: `PurchaseOrder` carries
  no currency, so it is served, and rendered, with none.

### Per-vendor rollups

Grouping spend **by vendor** (rather than one org-wide total) needs a second
helper: `currency_conversion.vendor_rollup_to_reporting_currency(rows,
reporting_currency=...)`. It groups currency-tagged rows by `vendor` first,
then runs `rollup_to_reporting_currency` **within each vendor's own rows**
before summing — a vendor that billed in both USD and EUR is converted
invoice-by-invoice and then added, never combined with a naive cross-currency
`SUM(amount)` (a vendor with a $1000 USD invoice and a €1000 invoice used to
report "$2000" — the face-value sum — instead of the correctly-converted
total). Returns a list of `VendorSpendEntry(vendor, amount, invoice_count,
currencies)` sorted by `amount` descending; `currencies` lists every distinct
original invoice currency that rolled into that vendor's total, so a
mixed-currency row stays auditable.

Surfaced everywhere a per-vendor spend total appears — all four now share this
one helper so they can never disagree with each other:

- **`GET /api/dashboard`** → `vendor_spend` (top 10)
- **`GET /api/analytics/cfo`** → `supplier_concentration` (top 50 feed the
  concentration math)
- **`GET /api/analytics/drill/spend_concentration`** → per-vendor drill-through
  rows (limited to `?limit=` **after** the per-vendor rollup, not at the SQL
  row level — limiting first would cut off a vendor's rows before they're
  fully summed)
- **`vendor_spend` CSV export / scheduled report** → `total_amount` +
  `currencies` columns (see `docs/analytics.md` § CSV export)

## Unrealized FX gain/loss

`currency_conversion.compute_unrealized_fx_gain_loss(open_invoices, ...)` marks
open (approved-but-unpaid) **foreign-currency** invoices to **today's** rate and
reports the difference vs the booked (rate-locked) reporting amount.

- One FX call per **distinct** foreign currency (not per row).
- Same sign convention as the realized figure: a **positive** number is a gain —
  the open liability is worth **less** in reporting terms than when booked.
- Same-currency invoices carry no FX exposure and are skipped.
- It becomes **realized** only when the invoice is paid, which is what
  `international_payments.realized_fx_gain_loss_for_settlement` measures — this is the
  open-position companion.

### A row with no locked rate is excluded and counted, never booked at face value

The booked leg is what the invoice was **recorded at** in the reporting
currency. `reporting_amount_for_row`'s face-value fallback returns the amount in
the row's *own* currency — right for a spend rollup (an approximate total with a
caveat beats a blank panel) and **wrong here**, because the mark-to-market leg
then converts the same original amount at today's rate and the arithmetic
reports the *conversion itself* as a gain or loss.

Dropping that helper's `unconverted` flag made a single EUR 1 000 open invoice
whose materialization had failed once produce an **$87 unrealized loss on an
exposure that never moved**. `invoice_warnings._refresh_reporting_amount` is
best-effort and documents leaving the fields NULL on an FX blip, and the `/cfo`
query applies no `IS NOT NULL` filter, so this is a live path.

Such a row is now left out of **both** legs and counted (`unconverted_count`, on
the result and per currency) — `decisions §35`, the sixth instance of the same
shape. A currency where *every* open row is unconverted still appears, carrying
its count with zeroed money, and no rate is fetched for it: an FX outage must
not be what decides whether the omission is reported. The count renders on the
`/cfo` FX card, so the number and its caveat are never in different places.

Open statuses considered: `approved`, `sending_to_erp`, `sent_to_erp`,
`posted_in_erp`, `payment_scheduled`.

Surfaced on `GET /api/analytics/cfo` → `unrealized_fx`:
`reporting_currency`, `total_unrealized_gain_loss`, `by_currency[]`
(`open_original_amount`, `booked_reporting_amount`, `current_reporting_amount`,
`unrealized_gain_loss`, `unconverted_count`), `unconverted_count`, and
`available` (false when the FX lookup failed — the CFO dashboard degrades rather
than 500-ing).

## FX provider

Reuses the existing `services/fx_adapters/` registry (`mock` default,
`openexchangerates`) via `get_fx_adapter(org.settings.fx)` — no new provider.
Local dev / tests use the deterministic `mock` adapter, so multi-currency
rollups work with no cloud account (local-first).

## Config

| Env var | Default | Purpose |
|---|---|---|
| `FEOH_REPORTING_CURRENCY_DEFAULT` | `USD` | Platform last-resort reporting currency when an org sets none. |

## Tests

`tests/test_currency_conversion.py` (service: resolution, conversion, rate
locking/idempotency, rollup, per-vendor rollup, unrealized gain/loss),
`tests/test_dashboard_aggregations.py` (the wired-up dashboard reporting
rollup), `tests/test_analytics_rejected_exclusion.py` (realdb end-to-end
coverage for the per-vendor rollup agreeing across the dashboard and CFO
concentration tile for a vendor billing in more than one currency),
`tests/test_expense_currency.py` (the expense line/report layers),
`tests/test_expense_policy.py` + `tests/test_expense_approval.py` (the
policy-threshold layer, incl. the fail-closed cases), and
`tests/test_multicurrency_kpi_reporting.py` (realdb: aging / monthly-trend /
upcoming-total on `GET /api/dashboard`, AP-balance / working-capital on
`GET /api/analytics/cfo`, and outstanding-amount on `GET /api/analytics/by-entity`
each correctly converting a mixed USD/EUR invoice or payment set). All
deterministic against the mock FX adapter.
