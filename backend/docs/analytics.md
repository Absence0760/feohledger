# Analytics & Reporting

Operational + CFO-grade analytics for AP. The dashboard at
`/api/dashboard` is the AP-clerk surface (pipeline, aging, recent
payments, processing-time metrics). The CFO surface at
`/api/analytics/cfo` adds finance-leader metrics (DPO, cash
conversion cycle, accruals, supplier concentration, rebate yield,
forecast variance). Both are computed by pure functions in
`services/analytics.py`; SQL lives in the API layer.

## Layers

| Layer | File | Purpose |
|---|---|---|
| Compute | `app/services/analytics.py` | Pure functions — DPO, processing time, supplier concentration, ... |
| Operational dashboard | `app/api/dashboard.py` | Pipeline / aging / processing time / approval bottleneck / discount capture (AP clerk + manager + CFO) |
| CFO dashboard | `app/api/analytics.py::get_cfo_analytics` | DPO + trend, CCC, accruals, working-capital impact, supplier concentration, fraud-rate trend, rebate yield (admin + CFO only) |
| Drill-through | `/api/analytics/drill/*` | Per-metric "show me the rows" endpoints (spend_concentration, dpo) |
| Cash-flow forecasting | `/api/analytics/{cashflow_forecast,cashflow_whatif,cash_position}` | Predictive AP outflow buckets, payment-timing what-if, running cash position with bank-balance auto-sync (admin + CFO only). Web dashboard at `/cfo`. |
| Cash-position thresholds | `/api/analytics/cash-position-settings` (GET/PUT) | Persisted per-org low-balance alert threshold on `settings.cashflow` (no migration). |
| CSV export | `app/services/report_export.py` + `/api/analytics/export/{report}` | invoice_register, vendor_spend, payment_register, aging_snapshot, cashflow_forecast, expense_register |
| Scheduled delivery | `app/services/scheduled_reports.py` + migration 0020 | Per-tenant cron-like subscriptions; daily / weekly / monthly cadence; email via existing adapter |

## Money serialisation

**Every money field on every `/api/analytics/*` endpoint is an EXACT decimal
string** (`"1500.00"`), never a JSON number — the project's Decimal invariant
applied at the API boundary, so no figure a CFO reads has round-tripped through
a binary float. `app/api/analytics.py::_money` is the module's single
serialiser; route every new money field through it rather than calling `str()`
inline, so this file can't half-migrate the way it did when `/drill/dpo` was
the only corrected endpoint (`../../docs/decisions.md` §32). It formats
**fixed-point**, because `str(Decimal("1E+3"))` is `"1E+3"` — parseable in
Python, and exactly the value a downstream consumer's own parser fumbles.
Trailing zeros are preserved (`"0.00"` stays `"0.00"`); a `None` figure stays
JSON `null`, never the string `"None"`. A source scan in
`tests/test_analytics_money_serialization.py` fails on any bare `str(...)` in
the module, so a second serialiser can't quietly appear.

**What is deliberately NOT a string**, because it is not money and
stringifying it would be a bug wearing compliance's clothes:

| Kind | Fields |
|---|---|
| Day counts | `dpo_current`, `dpo_trend[].dpo`, `drill/dpo` `rows[].dpo`, `cash_conversion_cycle`, `weighted_avg_pay_date_days` |
| Percentages | `supplier_concentration.{top_10_share_pct,top_50_share_pct,largest_vendor_share_pct}`, `drill/spend_concentration` `rows[].share_pct`, `fraud_rate_trend[].rate_pct`, `rebate_yield.yield_pct`, `forecast_variance` `rows[].variance_pct` |
| Counts | every `count` / `*_count` / `invoice_count` / `exception_count` / `open_exceptions` |

A `null` money field stays JSON `null` (`cash_position.threshold`,
`cash_conversion_cycle`) — "not set" is not `"0"`.

**Consumers.** `frontend/src/lib/types/analytics.ts` types these as
`MoneyString`; render them with `<Money>` / `formatMoney` and get a number out
of one only via `parseMoneyForLayout` (chart geometry + ordering — see
`frontend/CLAUDE.md` § Money formatting). The Flutter app's
`mobile/lib/models/cash_flow.dart` keeps them as display strings through
`moneyToDisplay`, which passes a string through verbatim and still stringifies
a legacy JSON number, so an app build older than this change keeps rendering.

## Operational metrics (dashboard)

Existing fields stay: `pipeline`, `vendor_spend`, `aging`,
`monthly_trend`, `upcoming_payments`, `touchless_rate`, `total_*`.

- `vendor_spend` — top 10 vendors by spend, excluding `rejected` invoices and
  rows with no vendor name. Rolled up into the org's reporting currency — a
  vendor billing in more than one currency is converted before its invoices are
  summed, not aggregated with a naive cross-currency `SUM(amount)` (that used to
  add a USD invoice and a EUR invoice as if they were the same currency).
  Grouped in SQL, and ordered `(total DESC, vendor ASC)` — see § Vendor spend is
  grouped in SQL below. The equivalent per-invoice helper
  `currency_conversion.vendor_rollup_to_reporting_currency` still backs the CFO
  supplier-concentration tile, its drill-through, the `vendor_spend` CSV export,
  and the scheduled report — see `docs/multi-currency.md` § Per-vendor rollups.
- `total_amount` — a **naive sum across every invoice in the tenant,
  regardless of status or date** — no filter at all, and no currency either:
  it is kept for API back-compat, and the web "Total Amount" KPI
  (`routes/+page.svelte`) renders its currency-aware counterpart
  `reporting.total_amount` over the same population instead. This is a different population from the CFO
  `total_spend` below (windowed + excludes rejected), even though both read
  like "how much have we spent": a rejected invoice, or one still sitting at
  `new`, counts toward this figure but not toward `total_spend`, and this
  figure has no date bound while `total_spend` is windowed to `period_days`.
  The web label spells this out (`Total Amount (All Invoices)`) precisely so
  the two aren't misread as the same number. See
  `tests/test_analytics_rejected_exclusion.py` for a regression test pinning
  the contrast.
- `aging` — open-invoice exposure bucketed by **days past the due date**:
  `current` (not yet due), `days_30` (1-30), `days_60` (31-60), `days_90`
  (61-90), `days_90_plus` (90+). The same five buckets back the
  `aging_snapshot` CSV export and the emailed scheduled report. Covers the
  SAME population as the CFO `accounts_payable_balance` (F-4) — which has no
  `due_date` filter — so an open invoice with a null `due_date` buckets as
  `current` (unknowable, so not overdue) rather than being silently dropped;
  otherwise the bands stop summing to the balance the moment one open invoice
  is missing a due date.
- `touchless_rate` — straight-through-processing rate. **Definition: the share
  of invoices that PASSED REVIEW without a human touching them, out of every
  invoice that provably finished review**, excluding rows a CSV import
  planted. See § Touchless rate — what the number means below; that section
  also records the two times this definition changed and which way the number
  moved each time.
- `monthly_trend` — invoice count + amount (+ `reporting_amount`) per calendar
  month, for the **last six calendar months**. The window is anchored to the
  1st of the month five months back, not `today - 180 days`: a rolling day
  window against a calendar-month `GROUP BY` never lines up, so the oldest bar
  was a partial slice of a month — sometimes a seventh, stub bucket of a
  fortnight's data — that reads as a spend collapse and shifts every single day
  as the window slides.
- `upcoming_payments` — the (at most ten) unpaid invoices due within seven days
  or overdue. Each row's `amount` is the invoice's **face** amount, in the
  row's own `currency` (the invoice's) — not a reporting figure. Label each row
  with that code, never with `reporting.reporting_currency` or the org's: the
  row carried no code until `docs/decisions.md` §200, so the web dashboard
  labelled a EUR invoice with a USD-reporting tenant's `$`.
- `upcoming_total_amount` — server-computed total across the same rows behind
  `upcoming_payments` (summed in `Decimal`, converted to `float` exactly once
  at the response boundary). Callers (the mobile dashboard) must read this
  field directly rather than folding `upcoming_payments[].amount` themselves —
  summing already-serialized floats client-side accumulates rounding drift.

New keys added in a prior iteration:

- `processing_time` — avg / median / p95 days for (upload→approval)
  and (upload→paid). Sample size below 5 collapses to zeros
  (`count_*_leg` still reported) so a tiny tenant doesn't see
  noisy numbers driven by one outlier.
- `approval_bottleneck` — per-approver pending counts, oldest
  pending age, average age. Top 10 returned, sorted by
  pending_count descending. Unassigned steps roll under the
  synthetic `unassigned` approver_id — a non-zero bucket there
  is its own routing-broken signal.
- `discount_capture` — eligible / captured / **missed / pending** counts and
  amounts, with `capture_rate_pct`. Eligibility comes from
  `PaymentSchedule.discount_percent`; capture is determined by
  whether the matching completed Payment's `completed_at` ≤
  `discount_date`.

  Three buckets, not two. An eligible invoice is `missed` only once its
  discount window has **elapsed** without being captured; while the deadline is
  still ahead (or the schedule carries no `discount_date` at all) the row is
  `pending` — still fully on the table. This surface was the only one of the
  consumers of these economics that never applied the elapsed-window gate
  (`bucket_outflows`' discount-eligible amount, the `early` what-if scenario,
  `discount_optimizer.optimize` and `discount_offers._tier_achievable` all
  did), so every newly-scheduled discount landed straight in a growing pile of
  "forgone savings" that had not been forgone. The rule now has one owner,
  `analytics.discount_window_open`.

  `capture_rate_pct` is over the DECIDED population (`captured + missed`) and
  is **`null`, never `0`**, when nothing has been decided — with
  `insufficient_data: true` beside it. "We have not missed a discount yet" and
  "we captured none of the discounts we could have" are opposite facts, and 0%
  renders as the bad one (`docs/decisions.md` §34).

  Money: `captured_amount` / `missed_amount` / `pending_amount` are per-row
  FACE values (a share of `Invoice.amount`) and mix currencies the moment one
  eligible invoice is foreign. The figures to render are
  `*_amount_reporting`, denominated in the response's own
  `reporting_currency` and resolved by `currency_conversion.reporting_amount_for_row`;
  a row with no usable rate lock contributes face value and is counted on
  `unconverted_count` rather than dropped (`docs/decisions.md` §35).

  **Frontend surface**: the dashboard (`frontend/src/routes/+page.svelte`) —
  an `Early-payment discounts` card rendering all three buckets with their
  reporting-currency amounts, plus a `Discounts captured` KPI card. Typed as
  `DashboardDiscountCapture` in `frontend/src/lib/types/analytics.ts`.

  Three contracts the UI encodes, each of which would reintroduce a defect the
  backend fix removed if a later slice "simplified" it:

  - **`unconverted_count` renders WITH the figure, not in a tooltip.** A
    non-zero count means the amounts mix currencies, so the card carries a
    `role="alert"` line (`[data-testid="discount-capture-unconverted"]`) and
    the KPI card's `sub` qualifier carries the same fact — outranking the
    capture rate there, because a rate is context while an unconverted count
    means the headline is partial. Matches how `/cfo`'s cash-position card
    presents its own `unconverted_count`.
  - **The amounts are labelled with the payload's OWN `reporting_currency`**,
    via a local `fmtIn` — never the separately-fetched org-settings currency
    the other dashboard KPIs use, which would let the page print "no exchange
    rate into GBP" above a column of `$`. Same rule, same name, as `/cfo`.
  - **`insufficient_data` renders as its own sentence, never `0%`.** "No
    discount window has closed yet" and "we captured none of them" are
    opposite facts.

  E2E: `frontend/tests-e2e/dashboard/discount-capture.spec.ts` (stubs the
  dashboard response so the pending bucket and the partial-figure disclosure
  are actually on screen — a seeded tenant reliably produces neither).

  **`POST /api/analytics/forecast_variance` now renders the same disclosure**
  — see [Forecast vs actual — the entry surface](#forecast-vs-actual--the-entry-surface)
  below. The three surfaces (this card, `/cfo`'s cash position, `/cfo`'s
  forecast-variance panel) share ONE idiom on purpose: a `role="alert"` line
  placed above the amounts it qualifies, naming the count and the currency it
  could not be expressed in. There is no fourth treatment, and a tooltip is not
  one of them.

### Vendor spend is grouped in SQL

The top-vendor tile is one aggregate query, like the four blocks around it:

```sql
SELECT vendor_name, coalesce(sum(<rep_expr>), 0) AS total
  FROM invoices
 WHERE vendor_name IS NOT NULL AND vendor_name <> '' AND status <> 'rejected'
   AND <entity scope>
 GROUP BY vendor_name
 ORDER BY total DESC, vendor_name ASC
 LIMIT 10
```

`<rep_expr>` is the **same** `CASE` the whole-book `reporting` rollup, the
`aging_reporting` buckets and the `monthly_trend` reporting column use — the
row's persisted `reporting_amount` when it is locked *for the org's reporting
currency*, else face `amount`. There is one expression, built once in
`api/dashboard.py`, so the tile cannot drift from the rollup beside it.

It used to `SELECT` five columns of **every** non-rejected invoice — all time,
no `LIMIT` — and fold them in Python through
`vendor_rollup_to_reporting_currency`. Two problems, both structural:

- the fold was a synchronous per-row loop inside an `async def`, so it occupied
  the event loop for its whole duration — the shape the project invariant
  *"blocking work does not run on the event loop"* forbids, on the landing page;
- it grew linearly with the invoice table, while its four neighbours stayed one
  aggregate each.

Measured on a scratch tenant of exactly 190 000 invoices across 400 vendors
(171 073 non-rejected rows scanned), local Postgres 16, mean of 5 warm runs:

| | DB | Python fold | total |
|---|---|---|---|
| Select-all + Python fold | 235.4 ms | **270.3 ms** | 505.7 ms |
| `GROUP BY … LIMIT 10` | 28.5 ms | — | **28.5 ms** |

17.7× faster end to end, and the 270 ms of event-loop-blocking CPU is gone
outright. The fold is the part that scaled worst: at 20 000 invoices it was
24.9 ms (48.1 ms total) against the aggregate's 8.3 ms — a 10.9× fold cost for
9.5× the rows, versus 3.4× for the aggregate.

**The ordering rule got stricter, deliberately.** The Python fold sorted on the
converted total alone, and Python's sort is stable, so equal-spend vendors came
back in DB scan order: which of two took rank 10 — and which fell off the
`[:10]` — could differ between two identical requests. The SQL breaks ties on
`vendor_name ASC`, so the tile is reproducible. Nothing else about the answer
changed; `tests/test_dashboard_vendor_spend.py` is the equivalence proof.

**Unconvertible rows fold at face value, and every block says so.** A foreign
invoice with no usable rate lock falls through at its face `amount` — the
flagged fallback `reporting_amount_for_row` documents. Three blocks used to
apply it with nowhere in their shape to report it; all three now carry a
count:

| Field | Grain | Why that grain |
|---|---|---|
| `vendor_spend[].unconverted_count` | per vendor | the tile RANKS vendors against each other, and an unconverted total is not comparable to a converted one |
| `aging_reporting.unconverted_count` | one per band set | `AgingBuckets` is a single object holding five sums; the actionable fact is the same either way, and five more fields would sit on a schema two response keys share |
| `monthly_trend[].unconverted_count` | per month | a trend is read bar against bar — a whole-series count would not say which step in the line not to trust |

The bare `aging` deliberately carries none. It is a face-value cross-currency
sum in its **entirety**, so "N rows could not be converted" would understate it
rather than describe it — the honest statement about `aging` is the one already
made by `aging_reporting` existing beside it.

All of them read `currency_conversion.invoice_reporting_amount_sql`. This
endpoint used to spell the two CASEs itself, which is how the whole-book
`reporting` rollup and the tile beside it could have started disagreeing about
what a foreign invoice is worth. Adopting the helper also fixed a
three-valued-logic hole in the local copy: it spelled the lock test as
`reporting_amount IS NOT NULL AND UPPER(reporting_currency) = tgt`, and for a
NULL currency that comparison is NULL, so the test was NULL, `NOT NULL` is
NULL, and the `unconverted` CASE fell to its `ELSE` — reporting such a row as
converted while its face value was what got added. The money figures were
always the face amount and did not change.

`api/payments.py::_payment_queue_rollup` carried the same inline copy with the
same missing leg, and adopted the helper in the same round — verified against
real Postgres, where the old expression reported `unconverted: 0` on a row the
new one counts as `1`. Two surfaces independently reaching that bug from the
same hand-written CASE is the argument for the shared expression, stated
better than any comment could.

### Discount capture is grouped in SQL too — by a CASE, not a currency

`discount_capture` was the last unbounded fold on this page, and it was left
that way deliberately: unlike its five neighbours it needs a per-ROW verdict —
captured, missed, or still-capturable — rather than a per-currency sum, so a
plain `GROUP BY <currency>` could not express it. The reasoning was sound and
the conclusion was wrong. The verdict is **two comparisons**, so it groups by a
`CASE`:

```sql
SELECT bucket,
       count(*),
       coalesce(sum(discount_amount), 0),
       coalesce(sum(discount_amount_reporting), 0),
       coalesce(sum(unconverted), 0)
  FROM (
    SELECT CASE
             WHEN paid_at IS NOT NULL AND discount_date IS NOT NULL
                  AND (paid_at AT TIME ZONE 'UTC')::date <= discount_date THEN 'captured'
             WHEN discount_date IS NOT NULL AND discount_date < :today  THEN 'missed'
             ELSE 'pending'
           END AS bucket,
           …                              -- the per-row figures, below
      FROM invoices
      JOIN payment_schedules ON …
      LEFT JOIN payments ON … AND payments.status = 'completed'
     WHERE discount_percent IS NOT NULL AND <entity scope>
     GROUP BY invoices.id, …, discount_percent, discount_date
  ) eligible
 GROUP BY bucket
```

Three rows come back where the query used to return one per discount-scheduled
invoice — `WHERE discount_percent IS NOT NULL`, no `LIMIT`, no date bound —
each fed through a synchronous Python projection inside an `async def`. It is
the same structural problem the vendor-spend tile had: event-loop-blocking CPU
on the landing page, growing with the table.

Three details are load-bearing:

- **`round(…, 2)` per row, not per bucket.** The Python fold quantized each
  invoice's discount to cents and then summed; the SQL does the same, in the
  same place. What changed is the tie-break: Postgres `round(numeric, 2)` goes
  away from zero where `Decimal.quantize`'s default context went to even, so a
  discount landing on exactly `x.xx5` now rounds up. That is the project's
  documented money convention and what `discount_offers.discount_savings`
  already uses for this exact quantity (base × percent ÷ 100) — this tile was
  the one place computing it the other way.
- **The `AT TIME ZONE 'UTC'` is not decoration.** `payments.completed_at` is
  `timestamptz`, and asyncpg handed Python a UTC-aware datetime whose `.date()`
  was therefore the UTC date. A bare `::date` reads the Postgres *session's*
  TimeZone, so a payment at 23:30 UTC on the deadline would miss its own
  discount window on any server configured east of UTC — the same class of bug
  `utils/dates.utc_today` exists to prevent.
- **The inner `GROUP BY` is the fold's, unchanged.** An invoice carrying two
  payment schedules on different terms was two rows before and is two rows now.

What stays in Python is what was never per-row: the three-bucket vocabulary,
the `captured / (captured + missed)` rate, and the `insufficient_data` state.
`analytics.discount_capture_from_grouped_rows` is that reducer;
`compute_discount_capture` remains as the readable statement of the rule and as
the reference implementation an equivalence test drives both halves against —
the same pairing `currency_conversion.rollup_from_grouped_rows` uses. It also
joins the drift guard's `_COLLAPSING_REDUCERS`, so a caller under `app/` cannot
quietly reintroduce the fold.

### Touchless rate — what the number means

`touchless_rate` is a claim about **how much work the machine did instead of a
person**. That makes its numerator's *population* — not just its arithmetic —
part of the metric's meaning, so it is stated here and encoded once, in
`services/analytics` (`TOUCHLESS_CLEARED_STATUSES` /
`TOUCHLESS_REVIEW_EVIDENCE_STATUSES` / `TOUCHLESS_BOUNCED_STATUSES` /
`compute_touchless_rate`). The hand-written copy that used to live in
`api/dashboard` had already drifted once — `sending_to_erp` is reachable ONLY
from `approved`, yet it appeared in neither leg, so an invoice sitting in the
ERP export hop dropped out of a metric it had already earned a place in,
understating the rate on exactly the tenants whose ERP export is slow.

**The definition: "passed review without human touch."**

| Leg | Statuses | Rule |
|---|---|---|
| Cleared (numerator + denominator) | `approved`, `sending_to_erp`, `sent_to_erp`, `posted_in_erp`, `payment_scheduled` | Status alone is proof — every `VALID_TRANSITIONS` edge into these originates at `approved`, and every writer of `approved` stamps `Invoice.approval_date`. |
| Ambiguous (`TOUCHLESS_REVIEW_EVIDENCE_STATUSES`) | `done`, `paid`, `failed` | Counts as cleared **only** with the durable `Invoice.approval_date` stamp. Without it, the invoice is in NEITHER leg. |
| Bounced (denominator only) | `rejected` | A human sent it back. Cannot be evidence-gated — nothing ever writes an approval stamp on a rejection, and the rejected row IS the evidence a human touched it. |

**And one exclusion that cuts across every row of that table:** an invoice
carrying the `meta["imported"]` provenance marker is subtracted from whichever
leg its status would have put it in — numerator and denominator alike. See
§ Imported rows are outside the metric below.

Why the ambiguous three need evidence:

- **`done`** — `new → done` is a legal transition that skips approval outright,
  and it is the default landing status of the Day-0 CSV importer
  (`services/csv_import`), which bypasses the workflow engine entirely.
- **`paid`** — normally only reachable from `payment_scheduled`, but
  `csv_import._IMPORTABLE_INVOICE_STATUSES` allows it too, for the same Day-0
  historical migration.
- **`failed`** — `VALID_TRANSITIONS` reaches it BOTH from `pending` (extraction
  failed, never reviewed) and from `sending_to_erp` (approved, then the ERP
  export blew up). This leg predates the others and is unchanged.

#### Why "passed review", not "reached a terminal state"

The alternative reading — *reached a terminal state without human touch* —
would keep counting the `new → done` shortcut and the imported historical rows,
because nobody touched those either. It was rejected because the metric is read
as an automation KPI: it is quoted to leadership as evidence the platform is
doing the approving. An invoice that never entered review is not evidence the
machine approved it; it is evidence the invoice was never approved at all.
Counting it inflates precisely the figure being trusted, and it inflates it
hardest for the tenant that just migrated ten thousand historical invoices on
day one — the tenant with the *least* automation to show.

The symmetric mistake is also avoided: a never-reviewed invoice is out of the
**denominator** too, not parked in the bounced leg. Counting it as
"finished review and did not clear" would deflate the rate just as dishonestly.
This is the same rule `failed` has always followed.

#### Imported rows are outside the metric — both legs

The evidence gate above fixes the NUMERATOR. The denominator had the mirror of
the same problem, and evidence cannot fix it: a CSV-imported `rejected` row
sits in the bounced leg as though a reviewer *here* had sent it back, deflating
the rate exactly as imported `done` rows used to inflate it — but nothing ever
writes an approval stamp on a rejection, so gating that leg would zero the
bounced population outright rather than exclude the imports.

**Provenance settles it instead of status.** `services/csv_import` stamps
`meta["imported"] = {"at": …, "source": "csv_import"}` on every invoice row it
creates (see `backend/docs/csv-import.md` § Import provenance). The dashboard
counts marked rows per status and passes them to `compute_touchless_rate` as
`imported_pipeline`, which subtracts them from the cleared, ambiguous and
bounced legs alike; the evidence query excludes them too. Status could never
have done this job — `done`, `paid` and `rejected` are each reachable both by
import and natively.

The reasoning is the same one that put never-reviewed invoices in neither leg:
the metric describes work **this platform** did. A migrated historical row is
evidence neither for nor against that, in either direction.

`imported_pipeline` is a REQUIRED keyword argument for the same reason
`review_cleared_count` is — a caller still on the old signature raises
`TypeError` rather than quietly publishing a rate whose denominator is padded
with somebody else's migrated history.

##### What this does NOT fix

Rows imported **before** the marker shipped carry no key and stay in the
population, on both legs. There is no **automatic** backfill, and there will not
be one: absence of the marker means "we do not know", and stamping a historical
row on an inference (its status, its creation date, the absence of a workflow
instance) is precisely the guessing this change exists to replace. What the data
cannot supply, a person can — see the operator-run tool in the next section.

Nor does the marker claim to catch every non-native row: it covers the CSV
importer, which is the only bulk path that plants invoices around the workflow
engine today. A future backfill tool (ERP history, a migration script) must
stamp the same key — `IMPORT_PROVENANCE_KEY`, with its own `source` — or its
rows will read as native.

##### Backfilling the marker for pre-marker imports — the operator asserts the cutover

`backend/scripts/backfill_import_provenance.py` closes the gap above the only
way it can be closed honestly: **an operator who knows when the migration ran
asserts the cutover, and the tool stamps what falls before it.** Nothing is
inferred from the rows.

```bash
# from backend/ — dry run, the default: reports, writes nothing
python scripts/backfill_import_provenance.py --tenant acme --cutover 2026-09-05

# stamp it
python scripts/backfill_import_provenance.py --tenant acme --cutover 2026-09-05 --apply

# a full ISO-8601 instant works too, when midnight UTC is the wrong boundary
python scripts/backfill_import_provenance.py --tenant acme \
    --cutover 2026-09-05T18:30:00+00:00 --apply
```

It writes the importer's own reserved key so every existing reader
(`imported_invoice_clause`, `native_invoice_clause`, the dashboard's
`imported_pipeline`) sees it with no code change:

```json
{"at": "2026-09-05T00:00:00+00:00",
 "source": "operator_backfill",
 "asserted": true,
 "stamped_at": "2026-09-14T11:02:41+00:00"}
```

`source` names this writer rather than `csv_import`, and `asserted: true` says
`at` is what somebody **declared**, not what was observed — so a later reader can
always tell an asserted marker from one the importer wrote at the moment of
import.

Safety properties, each enforced in the tool rather than left to the runbook:

| Property | How |
|---|---|
| Dry run is the default | `--apply` is the only mutating switch; without it the session is rolled back and no audit row is written |
| One named tenant | `--tenant <slug>` is required and resolves a single org; there is no "all tenants" mode |
| Date-bounded | `Invoice.created_at < cutover`, **strictly** before — a row created exactly on the cutover instant is native |
| Never stamps a provably-native row | Only statuses `csv_import` can actually land (`_IMPORTABLE_INVOICE_STATUSES` — `new` / `done` / `paid` / `rejected`, read from that module, not restated) are stampable. A pre-cutover row sitting in `ready_for_review` or `sending_to_erp` could only have got there through the workflow engine **here**, so it is reported as skipped, with its status, and left alone |
| Idempotent | Candidates are selected through `csv_import.native_invoice_clause()`, so an already-marked row is excluded in SQL (and re-checked in Python); a re-run stamps 0 |
| Never overwrites | An existing marker is left byte-for-byte alone, whoever wrote it — the tool only ever adds the key to a row that lacks it |
| Cutover sanity | A cutover in the future is refused: a migration that has not run cannot have produced historical rows, and that date would stamp the tenant's live invoices |
| Audited | A stamping run appends one `invoice.import_provenance_backfilled` row to the tenant trail — counts, statuses, the asserted cutover and the created-at range only, never an invoice number, vendor or amount |
| PII-free output | The console report is counts and statuses; the same discipline as the audit manifest |

The "never stamps a provably-native row" guard is not the rejected "identify
imports by status" inference in disguise: it never *marks* a row, it only refuses to. A refusal leaves the row
exactly as it reads today — unmarked, counted as native — which is the
documented default direction to be wrong in. It exists because a date bound
alone over-captures: on a dev tenant, a cutover of today proposed 90 rows, 58 of
them live pipeline invoices the importer could not have created.

**The caveat, stated plainly: the operator's assertion is the source of truth,
and a wrong date mis-stamps rows.** A cutover later than the real migration
stamps native invoices as imported and shrinks the metric's population; an
earlier one leaves imported rows counted as native. Neither is detectable from
the data — that is the whole reason the assertion has to come from a person.
Check the date against the migration runbook, run the dry run first (its
created-at range and per-status counts are there to be sanity-checked against
what you remember migrating), and only then pass `--apply`. Stamping is not
reversible by the tool.

#### The provenance exclusion MOVES it again — direction depends on the mix

The evidence gate moved `touchless_rate` **downward** (next section). The
provenance exclusion moves it again for any tenant that has imported since the
marker shipped, and the **direction depends on that tenant's mix**: dropping
imported `rejected` rows pushes the rate UP, dropping imported `done` / `paid`
rows that had somehow been counted pushes it DOWN, and a tenant whose whole
population is imported falls to the zero-safe `0.0` — the same answer a brand
new tenant gets, which is the honest one.

Both moves are **definition changes, not automation changes**. No workflow,
auto-approval threshold or routing rule changed, and no invoice moved. A
tenant that has never run a CSV import sees no change at all from this half.
Read a dashboard delta accordingly.

#### The evidence gate MOVED a previously reported number — downward

Before the evidence gate, `done` and `paid` counted as cleared on status alone. Any
tenant that uses the `new → done` shortcut, or that migrated history through
the CSV importer, will see `touchless_rate` **drop** the first time the
dashboard is loaded after deploy. The drop is a **definition change, not a
regression in automation** — no workflow, auto-approval threshold or routing
rule changed, and no invoice moved. A tenant whose invoices all travel the
normal `ready_for_review → approved → …` path sees no change at all, because
every one of those rows carries the approval stamp.

If a dashboard delta needs explaining, the honest sentence is: *the metric
stopped counting invoices that never went through approval.*

## CFO metrics (`GET /api/analytics/cfo`)

Query params: `period_days` (default 365, range 30–730).

Every money field below is an exact decimal string; `dpo_current`,
`dpo_trend[].dpo`, `cash_conversion_cycle`, every `*_pct` and every count are
JSON numbers. See [Money serialisation](#money-serialisation).

Response:
- `total_spend` — invoices dated within the trailing `period_days` window,
  excluding `rejected`. **Not the same population as the dashboard's
  `total_amount`** ("Total Amount (All Invoices)") — see that field's
  description above before wiring a UI that shows both side by side.
- `accounts_payable_balance`, `avg_daily_outflow`
- `dpo_current` + `dpo_trend` (the last 6 **closed** months — the loop walks
  back from the 1st of the current month, so the newest point is the month that
  just ended). See § DPO trend + drill-through below. The `/cfo` chart names
  this window (`CfoMetrics.svelte`'s `cfoMetrics.dpoTrend.hint`, under the
  chart title) so "no data for this month yet" doesn't read as a bug.
- `cash_conversion_cycle` (NULL when DSO/DIO not available — the
  AP-only product can't compute it)
- `accruals.by_currency[]` — **the accruals figure to render.** One row per
  currency that appears in any leg: `{currency, open_po_amount,
  received_amount, unposted_invoice_amount, total_accrual}`, each leg in that
  currency and `total_accrual` netted **within** it. Every PO is grouped by its
  own `purchase_orders.currency`, every unposted invoice by its own, and a
  receipt is worth its PO's money in its PO's currency. `currency: null` is the
  slice nobody recorded a currency for (POs only — an invoice always carries
  one): kept apart and rendered bare, never folded into a real currency's
  figure. Ordered by code with that row last. The four flat
  `accruals.{open_po_amount, received_amount, unposted_invoice_amount,
  total_accrual}` fields beside it stay the **naive sums across currencies**,
  kept for API back-compat like `total_spend` — a EUR purchase order added to a
  USD one is denominated in nothing (`docs/decisions.md` §197).
- `accruals.{open_po_amount, received_amount, unposted_invoice_amount, total_accrual}`
  (`received_amount` values goods physically received but not yet
  invoiced — the GR/IR accrual leg. The 3-way match is fanned out per
  PO: each receipted PO contributes `po_total × min(1, gr_qty/po_qty)`,
  the same received-fraction the PO matcher computes. The open-PO leg counts
  only POs that are still live — `models/procurement.DEAD_PO_STATUSES`
  (`cancelled` / `closed` / `voided`) are excluded, the same roster the budget
  commitment leg uses; it used to sum every PO the tenant had ever raised, so a
  cancelled order stayed a commitment forever. POs with no
  quantified lines but a booked receipt count as fully received;
  receipts with no PO link can't be priced and are excluded. Pure math
  in `analytics.value_received_goods`; SQL fan-out in
  `api/analytics._received_amounts`, which returns the whole-book figure and the same valuation grouped by each PO's currency.)
- `working_capital_impact_5_days` — `avg_daily_outflow × 5`
- `supplier_concentration.{top_10_share_pct, top_50_share_pct, largest_vendor, largest_vendor_share_pct, flagged}` — `flagged=true` iff the largest vendor **reaches or** exceeds 25% (configurable; the boundary is inclusive on purpose — a risk flag that stays dark at exactly the configured limit is the wrong direction to be wrong in). **Every share is computed against the whole period's spend**, never a top-N subtotal: `compute_supplier_concentration` derives its denominator from the list it is handed and takes its own `[:10]`/`[:50]` cuts, so the caller must pass the full vendor set and slice only for display. Passing a pre-sliced top-50 made `total_spend` the top-50 subtotal, inflated `top_10_share_pct` / `largest_vendor_share_pct` (and with them `flagged`), and pinned `top_50_share_pct` at exactly `100.0` on any tenant with 50+ vendors. The same rule governs `/drill/spend_concentration`, whose `total_spend` and `share_pct` are computed before `?limit=` is applied — otherwise `limit` silently rebased both and the drill disagreed with the tile it was opened from. Excludes `rejected` invoices (never real spend) — the SAME population its drill-through and the `vendor_spend` export/scheduled report use, so clicking from the tile into either agrees with the number the CFO started from. Also the SAME reporting-currency rollup as the dashboard's `vendor_spend` (see above) — a vendor's multi-currency invoices are converted before summing, never naively added across currencies
- `supplier_concentration.unconverted_count` — invoices folded into `total_spend`, and therefore into every share above and into `flagged`, at **face value** because no locked exchange rate bridged them into the reporting currency. A count, not money; `0` on a single-currency tenant. See § Per-vendor spend is one query below
- `fraud_rate_trend` — exceptions / invoices × 100 per month. **`rate_pct` is
  `null` (with `insufficient_data: true`) for a month that booked no
  invoices** — an empty denominator makes the rate not computable, and
  returning `0` reported the most reassuring value on the chart for the one
  shape carrying no information at all. It did that hardest in the case that
  most warrants a look: zero invoices booked, exceptions raised anyway. Same
  treatment `cash_conversion_cycle` gets for an unknown leg and the adaptive
  feedback surface gets under its minimum sample; `docs/decisions.md` §34. The
  `/cfo` chart renders those months as `—` with a reason line, and draws no bar.
- `rebate_yield.{yield_pct, annualised_rebates, ...}` — **windowed to the same
  trailing `period_days` as `total_spend`**, which is the denominator it divides
  by and the span `months_in_period` describes. The rebate sum carried no date
  predicate at all, so the numerator was every rebate the tenant had ever
  booked: `yield_pct` was lifetime-over-this-window and `annualised_rebates`
  multiplied a multi-year total by 12. A three-year-old tenant with $36k of
  rebates and $100k of spend in the last 30 days reported a 36% yield and a
  $432k annual run-rate against a truth of ~1% and ~$12k. Filtered on
  `CardRebate.created_at` (when the rebate was booked); the `period` column is a
  display label, not a filter key.

### Per-vendor spend is one query, and it says what it could not convert

Five surfaces answer "what did we spend, by vendor":

| Surface | Route / module | Population |
|---|---|---|
| CFO supplier-concentration tile | `GET /api/analytics/cfo` | not `rejected` |
| its drill-through | `GET /api/analytics/drill/spend_concentration` | not `rejected` |
| CSV export | `GET /api/analytics/export/vendor_spend` | not `rejected` |
| emailed scheduled report | `services/scheduled_reports.py` | not `rejected` |
| assistant `get_vendor_spend` tool | `services/assistant/tools/vendor_spend.py` | `_COMMITTED_STATUSES` |

The four AP surfaces run **one** builder,
`currency_conversion.vendor_spend_grouped_select`, and reduce its output with
`vendor_rollup_from_grouped_rows`:

```sql
SELECT vendor_name,
       upper(coalesce(currency, :tgt))                  AS currency,
       coalesce(sum(amount), 0)                         AS original_amount,
       coalesce(sum(<rep_expr>), 0)                     AS reporting_amount,
       count(*)                                         AS count,
       coalesce(sum(<unconv_expr>), 0)                  AS unconverted_count
  FROM invoices
 WHERE invoice_date >= :period_start
   AND vendor_name IS NOT NULL AND vendor_name <> '' AND status <> 'rejected'
   AND <entity scope>
 GROUP BY vendor_name, upper(coalesce(currency, :tgt))
```

`<rep_expr>` / `<unconv_expr>` come from
`currency_conversion.invoice_reporting_amount_sql` — the SQL statement of the
rule `reporting_amount_for_row` applies a row at a time, so the two cannot
drift. Grouping by `(vendor, currency)` rather than vendor alone is what keeps
the CSV's `currencies` column and the per-vendor `unconverted_count` derivable
without a second query; the extra rows are *vendors × currencies*, never
invoices.

**No `LIMIT`, deliberately.** `compute_supplier_concentration` derives
`total_spend` and every `*_pct` from the list it is handed, so a pre-sliced
query would rebase every share onto the slice and pin `top_50_share_pct` at
`100.0` by construction. Slicing happens after the rollup, for display only.

**The assistant tool shares the grouping, not the population.** It answers
about COMMITTED spend, so it runs the tier below —
`vendor_currency_rollup_select`, which `vendor_spend_grouped_select` is itself
built on — and adds its own `WHERE`. What must not be respelled per surface is
the grouping and the conversion; the population is a real difference and stays
each surface's own. It passes `include_vendor_id=True`, which is load-bearing
there and nowhere else: the tool RETURNS the vendor id, and two distinct vendor
records can carry the same `vendor_name` (an unmatched extraction beside a
verified master record), so grouping by name alone would attribute one
supplier's spend to the other and hand back an id that does not own the number
beside it. The four AP surfaces group by name alone because they only label.

**What this replaced.** Each of the four selected five columns of every invoice
in the period and folded them in Python through
`vendor_rollup_to_reporting_currency` — a synchronous per-row loop inside an
`async def`, the shape the *"blocking work does not run on the event loop"*
invariant forbids and the one `docs/decisions.md` §103 removed from the
dashboard's own top-vendor tile. Materially less bad than the dashboard's
version (date-bounded by `period_days` rather than all-time) but identical in
kind, and one of the four runs on a background sweep where nothing bounds it.

Measured on a scratch tenant of exactly 190 000 invoices across 400 vendors,
local Postgres 16, `period_days=365` (159 476 in-period non-rejected rows), mean
of 5 warm runs:

| | rows transferred | DB | Python | total |
|---|---|---|---|---|
| Select-all + Python fold | 159 476 | 415.2 ms | **547.1 ms** | 962.3 ms |
| `GROUP BY (vendor, currency)` | 1 600 | 53.4 ms | 7.6 ms | **61.0 ms** |

15.8× end to end, and the 547 ms of event-loop-blocking CPU is gone outright —
which is the part that matters most, because during it *every other request on
that worker waits*. The transferred-row count is the reason it scales: 1 600 is
`vendors x currencies` and moves only when the tenant adds a vendor or a
currency, while 159 476 grows with every invoice booked.

The row-at-a-time helpers are kept as the readable statement of the conversion
rule and as the reference the grouped path is equivalence-tested against, so
the guard in `tests/test_analytics_rollup_sql.py` is on their CALLERS.

**That guard checks the shape, not a name.** Its first version scanned for
`vendor_rollup_to_reporting_currency` callers, and that is precisely why the
assistant's tool had to be found by hand: it never called that helper — it
hand-rolled the same fold out of `reporting_amount_for_row` and a dict. It now
fails on both shapes:

1. any call to a **collapsing reducer** (`rollup_to_reporting_currency`,
   `vendor_rollup_to_reporting_currency`) — both take a list of per-invoice
   dicts and return totals, so calling either means the rows were transferred
   and reduced in Python;
2. `reporting_amount_for_row` inside a loop or comprehension whose body
   accumulates into a **per-key subscript** — the hand-rolled `GROUP BY`, in
   either spelling (`totals[k] += x` or `totals[k] = totals.get(k, 0) + x`).

A **projection** — one output row per input row — is deliberately not matched,
because three legitimate call sites are projections: `_commitment_rows`' cash
commitments, the dashboard's ten upcoming payments, and its per-row discount
economics all need the converted value on each row, not a total. Neither is a
bounded scalar `total += x`. The detector is itself tested against synthetic
source for both matching shapes and both non-matching ones, so deleting its
body cannot leave a green suite.

**Unconvertible rows are disclosed, not absorbed.** `VendorSpendEntry` carries
an `unconverted_count`, and both API surfaces publish it: the tile as one
whole-population count beside the shares it distorts, the drill-through both
per row and — like `total_spend`, and for the same reason — for the whole
period *before* `?limit=` is applied. A fallback nobody reports is just a wrong
number (`docs/decisions.md` §35). The `/cfo` UI should render it through the
shared `role="alert"` skipped-rows note the cash-position and AP-balance cards
already use (`CfoMetrics.svelte`); the field is already declared on
`frontend/src/lib/types/analytics.ts`'s `CfoSupplierConcentration`. It needs its
own message — `cfoMetrics.apBalance.unconverted` says "Excluded ... the balance
above is a floor", and these rows are **included** at face value, so reusing it
would state the opposite of what happened.

The CSV export carries it too, as its own column — see § CSV export. That
surface needed it most: it has no sibling field and no page to carry a note,
and the file outlives the tenant context that produced it.

Three sibling rollups on the same handler moved to the same shape in the same
change, through `currency_conversion.invoice_currency_rollup_select`:
`reporting_spend`, `reporting_accounts_payable_balance`, and
`/analytics/by-entity`'s `reporting_outstanding_amount` — the last of which ran
its four-columns-of-every-open-invoice fold **once per entity**.

### DPO trend + drill-through — one population, one calculation

`dpo_trend` (the chart) and `GET /api/analytics/drill/dpo?months=N` (the
drill-through a CFO opens to explain a spike in it) answer the same question at
two resolutions, so they must never disagree. They now share both halves:

- **The snapshots** come from `api/analytics.py::_monthly_dpo_snapshots` — the
  only place the monthly `{month, accounts_payable, cogs}` SQL lives. It states
  the `rejected` exclusion once (matching the headline `total_spend`, so the
  COGS proxy isn't inflated relative to the DPO computed from it) and takes the
  open-AP population from the canonical `OPEN_AP_STATUSES`, not a hand-copied
  status list.
- **The arithmetic** comes from the pure `services/analytics.py::compute_dpo_trend`.

They used to be two hand-written copies of the same loop, and the copies had
already drifted: the chart excluded `rejected` invoices from COGS, the
drill-through summed every status — so a $9 000 rejected invoice next to a
$1 000 approved one made the drill-through report 3.0 days where the chart it
was opened from showed 30.0. Same failure shape as the supplier-concentration
tile vs. its drill-through (issue #126). Pinned by
`tests/test_analytics_rejected_exclusion.py`.

`/drill/dpo` serializes `accounts_payable` and `cogs` — money — as **exact
decimal strings**, and `dpo` as a JSON number, because it is a day count. That
split is now the whole module's rule, not this endpoint's exception: see
[Money serialisation](#money-serialisation). (`/drill/dpo` was corrected first,
in isolation, because it had no shipped consumer — `../../docs/decisions.md`
§32.)

Drill-through (money as exact decimal strings here too — see
[Money serialisation](#money-serialisation)):
- `GET /api/analytics/drill/spend_concentration?period_days=N&limit=N` —
  `rows[].amount` + `total_spend` are money; `share_pct` is a percentage and
  `invoice_count` a count, so both stay JSON numbers.
- `GET /api/analytics/drill/dpo?months=N`
- `POST /api/analytics/forecast_variance` — body `{"months":
  [{"month": "YYYY-MM", "forecast": "100000"}, ...]}`. Server
  fills in `actual` from completed payments and returns
  `{reporting_currency, rows: [{month, forecast, actual, variance,
  variance_pct, unconverted_count}, ...]}` — the three amounts money,
  `variance_pct` a percentage, `unconverted_count` a count.
  Forecasts are NOT persisted — the CFO pastes from their FP&A
  tool.
  - **`actual` is resolved into the org's reporting currency** via
    `currency_conversion.payment_reporting_amount_sql`, not summed off raw
    `Payment.amount` — that column is denominated in the INVOICE's currency, so
    one foreign payment turned the month's actual into a two-currency mixture
    and then compared it against a forecast typed in a single currency. A
    payment neither rung can express is EXCLUDED and counted on
    `unconverted_count`, never added at face value: a variance is a number
    someone acts on, so it must read as the floor it is (`decisions.md` §35).
    Same resolver the payments summary, the AML trailing-spend gate and the
    1099 filing use.
  - **A malformed `month` is a `422`, not a `500`.** The guarded parse used to
    cover `int(...)` but not the `date(year, mon, 1)` it fed, so a
    caller-supplied `"2026-13"` (or `"2026-00"`) escaped as a bare `ValueError`.
    Both halves are now inside one `try`, and every month-shape rejection on
    this route answers `422` rather than a mix of `400` and a crash.

#### Forecast vs actual — the entry surface

`frontend/src/routes/cfo/ForecastVariancePanel.svelte`, rendered on `/cfo`
**outside** the cash-flow `{#if}` (the reasoning the budget rollup and the
scheduled-reports panel already follow: it takes none of that page's controls,
issues its own request, and a failed cash-flow load must not hide the only
surface that renders this endpoint's disclosure). Role-gated `admin | cfo`
inside the panel, matching `_CFO_ROLES` on the endpoint and the route's own
gate.

**It needs a form because the endpoint is a POST with a body.** The forecast is
the CFO's own figure set, pasted from their FP&A tool, and nothing is
persisted — so there is no saved forecast to `GET` and every visit starts from
an empty editor. A `Modal` holds one row per month: an `<input type="month">`
(which yields exactly the `YYYY-MM` the route parses, so the month half needs
no repair) and a free-text amount.

- **The amount is raw decimal text, validated ONCE at submit and refused with a
  toast** — never `parseFloat`ed per keystroke, never repaired. `$1,200.50`,
  `1 200`, `1.2e5` and a trailing `.` are all refused rather than stripped down
  to a figure the CFO never typed; a month typed without an amount refuses the
  whole submit instead of being silently sent as `0` (which would make the
  variance equal the entire actual outflow and report a fabricated `0%`). The
  shape check is `utils/moneyInput.ts::normalizeMoneyInput` — a regex, never
  `Number` — so the string on the wire is the string that was typed.
- **The disclosure renders with the figures.** `unconverted_count` is folded
  across months (a COUNT may cross currencies; the amounts beside it may not)
  into a `role="alert"` line, `[data-testid="forecast-variance-unconverted"]`,
  placed ABOVE the table it qualifies and naming the response's own
  `reporting_currency`. The month carrying the exclusion also says so on its own
  row. Identical treatment to `/cfo`'s `unconverted-outflows` and the
  dashboard's `discount-capture-unconverted`.
- **`variance_pct` is never rendered as `0%` where it is not computable.** The
  route emits `Decimal("0")` whenever `forecast <= 0`, which on screen reads as
  "we landed exactly on plan" — the most reassuring statement available over the
  one row carrying no information. `variancePctLabel` returns `null` for a
  non-positive forecast and the cell renders its own not-applicable state, the
  same rule as the fraud-rate trend's `insufficient_data` and the budget
  rollup's `null` utilization (`../../docs/decisions.md` §34).
- Pure derivations live in `frontend/src/routes/cfo/forecastVarianceSummary.ts`
  (`collectForecastEntries` / `unconvertedTotal` / `variancePctLabel` /
  `varianceTone`), unit-tested beside the route like `budgetRollupSummary.ts`;
  the request helper is `frontend/src/lib/api/analytics.ts`. No money
  arithmetic happens client-side — the backend owns `variance` and its
  percentage, in `Decimal`.

E2E: `frontend/tests-e2e/cfo/forecast-variance.spec.ts` (stubs the POST so the
disclosure is actually on screen — a seeded tenant reliably produces no
unconvertible payment, which is the state that let this ship with no consumer;
also asserts the typed decimal string reaches the wire verbatim and that an
unreadable amount sends no request at all).

**Frontend surface**: `frontend/src/lib/components/analytics/CfoMetrics.svelte`,
embedded in `/cfo` below the forecast/what-if/cash-position panels (a
self-fetching component mirroring `ByEntityBreakdown` — its own `GET
/api/analytics/cfo?period_days=` call, own loading/error state). Renders a KPI
row (DPO current, cash conversion cycle, AP balance, rebate yield %), the DPO
6-month trend as a bar chart, an accruals breakdown (each of the four boxes
one line per currency, from `accruals.by_currency` — never the flat sums —
through `ui/MoneyByCurrency.svelte`, with a note when POs recording no currency
are among them), supplier concentration
(with the flagged-vendor banner), the fraud-rate trend, and the unrealized-FX
table when available. `/forecast_variance` has its own surface on the same route
(`routes/cfo/ForecastVariancePanel.svelte` — see above); the two remaining
drill-throughs (`/drill/spend_concentration`, `/drill/dpo`) are not yet wired
into the UI — that stays a future slice. Filed as #236.

## Consolidated reporting across entities (`GET /api/analytics/by-entity`)

Read-only, admin + CFO only. A side-by-side **per-entity AP rollup PLUS a
consolidated total** — the multi-entity "consolidated reporting across
entities" view. Every other endpoint in `analytics.py` reports either ONE
entity (the `X-Entity-ID` selection) or the fully-merged consolidated total;
this one returns **all active entities at once** so subsidiaries can be
compared side by side.

Unlike its neighbours it **intentionally ignores `X-Entity-ID`** — it doesn't
depend on `get_entity_id`. Instead it lists active entities directly
(`is_default` first, then by name — matching the entity switcher's order) and
calls the shared `_entity_metrics(entity_id=...)` helper once per entity, then
once more with `entity_id=None` for the `consolidated` block. Because every row
and the consolidated block run the same entity-scoped query shapes used by
`/analytics/cfo` (total spend, open-payables balance, invoice count,
open-exception count, open-PO accrual via `_open_po_amounts_by_currency`), the
consolidated block is a true sum-across-entities cross-check.

Query params: `period_days` (default 365, range 30–730).

Every money field below is an exact decimal string; `dpo_current`,
`dpo_trend[].dpo`, `cash_conversion_cycle`, every `*_pct` and every count are
JSON numbers. See [Money serialisation](#money-serialisation).

Response:
- `period_days`, `period_start`
- `entities[]` — one row per active entity:
  `{entity_id, entity_name, entity_slug, currency, is_default, total_spend,
  reporting_total_spend, reporting_total_spend_unconverted_count,
  outstanding_amount, reporting_outstanding_amount, reporting_currency,
  reporting_outstanding_unconverted_count, invoice_count, open_exceptions,
  open_po_amount}`
- `consolidated` — the same metric block computed with `entity_id=None`
  (equals the sum across `entities[]`)

Money fields are **string-Decimal** (never floats). A single-entity tenant still
returns a coherent one-row breakdown whose row equals the consolidated block.

**Which figure is in which currency — and which has none.**

| Field | Denominated in |
|-------|----------------|
| `reporting_total_spend`, `reporting_outstanding_amount` | `reporting_currency` (the org's, same on every row). A foreign invoice with no locked rate is counted at **face value**, and the matching `*_unconverted_count` says how many. The consolidated spend figure is the same population and rollup as `/cfo`'s `reporting_spend`, so the two cannot disagree. |
| `total_spend`, `outstanding_amount` | **Nothing** — naive `SUM`s across currencies, kept for API back-compat. Never render them. |
| `open_po_amount` | **Nothing** — a naive `SUM` across the PO currencies, kept for back-compat. Never render it: `open_po_by_currency` is the figure. |
| `open_po_by_currency[]` | Each `{currency, amount}` in its OWN `currency` — the PO's `purchase_orders.currency` (migration 0099). `currency: null` is the entry for POs that record none; it renders bare and is never merged into another. Ordered by code with that entry last, the same order `/cfo`'s `accruals.by_currency` uses, and the consolidated row equals the sum of the entity rows per currency. |
| `currency` (entity row) | Not a denomination: the entity's configured currency, `NULL` meaning "the org's reporting currency". It labels no figure. |

The web surface is the `By entity` table on `/cfo`
(`frontend/src/lib/components/analytics/ByEntityBreakdown.svelte`), which
self-hides for single-entity tenants (mirrors the entity switcher). It renders
Spend and Outstanding from the `reporting_*` fields in `reporting_currency` on
every row, Open POs as one line per PO currency (`ui/MoneyByCurrency.svelte`),
and one face-value disclosure line per column whose consolidated count is
non-zero, plus a note when any PO records no currency at all. It used to label
each entity row's naive `total_spend` / `outstanding_amount` / `open_po_amount`
with the entity's `currency`, falling back to the org's — a mixed-currency
figure wearing one currency's symbol (`docs/decisions.md` §196) — and then,
once Open POs went bare, to show one unlabelled sum across PO currencies (§197).

## Predictive cash-flow forecasting (`/api/analytics/{cashflow_forecast,cashflow_whatif,cash_position}`)

Read-only, admin + CFO only. All three resolve the tenant DB through
`get_tenant_db`. Source rows: open invoices `LEFT JOIN payment_schedules`
on `invoice_id`, timed on the schedule's `due_date` (falling back to
`Invoice.due_date`), bounded to `[today, today + horizon_days]`. Money is
`Decimal` end-to-end and serialises as an **exact decimal string** (see
[Money serialisation](#money-serialisation)). No writes, no audit rows, no new
migration — pure aggregate math, like `forecast_variance`.

**Committed vs pending status sets** (the rest — `rejected`, `paid`,
`done`, `failed` — are excluded):

- **committed** (firm AP commitment): `approved`, `sending_to_erp`,
  `sent_to_erp`, `posted_in_erp`, `payment_scheduled`.
- **pending** (in-flight pipeline, lower-certainty projection): `new`,
  `pending`, `ready_for_review`. Drop with `include_pending=false`.

`GET /cashflow_forecast?granularity=day|week|month&horizon_days=N&include_pending=bool`
— per-period `{scheduled_amount, committed_amount, pending_amount,
discount_eligible_amount, count, unconverted_count}` plus a `totals` rollup.
Weeks are Monday-anchored. Default `granularity=week`, `horizon_days=90`.

### One commitment row per invoice, whatever its schedule count

`_commitment_rows` outer-joins `payment_schedules` for the authoritative due
date + early-pay discount terms. That join was **un-deduped**: it returns one
row per schedule, and the loop appends a commitment row for each — so an invoice
carrying two `PaymentSchedule` rows was counted **twice, at its full amount**.
Because this one query feeds the forecast, the cash-position curve, the what-if,
every copilot planning tool and the `plan_id` hash, a single double-count
overstates projected outflow on all of them at once, with nothing on any surface
to show it.

It is unreachable today only because nothing but `scripts/seed.py` constructs a
`PaymentSchedule` — an accident of the current feature set, not a guarantee.

The query now takes `DISTINCT ON (invoice_id) … ORDER BY invoice_id,
created_at DESC, id DESC` — the LATEST schedule, mirroring
`discount_auto_trigger._resolve_due_date`'s existing `ORDER BY created_at DESC
LIMIT 1` on the same table, so the discount engine and the cash forecast cannot
disagree about an invoice's due date. `created_at` defaults to
`transaction_timestamp()`, so rows written in one commit share a timestamp and
"latest" is genuinely undefined between them; the `id` tiebreak only makes that
case **deterministic** (a copilot `plan_id` must not flap between reads), it
does not invent an ordering. Guard:
`tests/test_cashflow_forecast_api.py::test_two_payment_schedules_do_not_double_count_the_invoice`.

### `unconverted_count` — the outflow-side currency caveat

Every commitment row is expressed in the org's **reporting** currency via the
rate locked on the invoice (`_commitment_rows` → `reporting_amount_for_row`).
A foreign invoice with no usable lock is included at **face value** — dropping
it would understate the outflow — and flagged. That flag is now surfaced as
`unconverted_count`: per period on `/cashflow_forecast` and `/cash_position`,
and as a total on all three cash endpoints (and on the copilot's
`get_cashflow_forecast` / `get_cash_position` / `run_payment_whatif` results).

It matters because the face value is a *different currency's* number sitting
in a reporting-currency total. One unconverted ¥10,000,000 invoice drags a
$250,000 opening balance to a projected −$9.75M — and the shortfall sweep
emails that to the finance leaders. The flag was computed on every row and read
by nobody, so the number arrived with nothing to contradict it. Non-zero means
the totals mix currencies and the curve is not a figure to act on until the
conversion is resolved; it is the outflow-side twin of `cash_position`'s
`opening_balance_provider_skipped: "currency_mismatch"`, which already guarded
the opening-balance half of the same equation.

It is a **count**, not money — a JSON number, listed in
`tests/test_analytics_money_serialization.py`'s `NUMERIC_FIELDS`.

`GET /cashflow_whatif?granularity=…&horizon_days=N&grace_days=N` —
compares three payment-timing scenarios, each with `total_outflow`,
`total_discount_captured`, amount-weighted `weighted_avg_pay_date_days`,
and bucketed `periods`. `weighted_avg_pay_date_days` is a day count, not
money, and stays a JSON number:

- `on_time` — pay on `due_date`, full amount.
- `early` — pay on `discount_date` when that window is **still open**
  (`discount_date >= today`), net of `discount_percent` (the captured
  discount is reported separately); rows without a discount — and rows whose
  window has already **elapsed** — fall back to the due date at full amount.
- `late` — pay `due_date + grace_days`, full amount, discount forfeited.

The elapsed-window guard matters because the source rows are bounded on their
**due** date only: an in-horizon invoice on `2/10 net 60` terms routinely
carries a `discount_date` weeks in the past. Claiming it overstated
`total_discount_captured`, timed the outflow on a date before `today` (buckets
entirely inside a period that has already closed, and a **negative**
`weighted_avg_pay_date_days`), and invited a payment run funded against savings
that were no longer available. The rule matches
`services/discount_optimizer.optimize`, which has always treated an elapsed
deadline as not capturable — so the what-if card and the optimizer agree about
which discounts are still on the table.

`GET /cash_position?granularity=…&horizon_days=N&opening_balance=STR&min_balance_threshold=STR&seed_balance=bool`
— running balance carried forward per period
(`closing = opening − outflow`; receivables/inflows aren't modelled in an
AP-only product). Periods that close below the effective threshold are flagged
(`below_threshold`) and collected in `breaches[]` with the `shortfall`. Money
params are parsed as `Decimal` strings (never floats); garbage → 400. A
`threshold` of `null` on the response means none is set — deliberately not
`"0"`, which would read as "alert on any positive balance".

**Opening balance — resolution order** (first hit wins; the chosen source is
echoed as `opening_balance_source`):

1. `opening_balance` query param — explicit bring-your-own override →
   `"explicit"`.
2. **Bank-balance auto-sync** — pulled from the org's configured
   payment/banking provider via the optional `PaymentAdapter.get_balance`
   capability → `"provider"` (with `opening_balance_currency`). Only attempted
   when the org has a `settings.payments` provider configured and
   `seed_balance` is true (default). **Best-effort** — an adapter that doesn't
   implement the capability, or a fetch that fails, silently falls through to
   the next source; the dashboard never 500s on a bank-link outage, and nothing
   logs the balance figure or any account number. The local-first `mock` adapter
   returns a deterministic figure (250000.00) so `pnpm dev` needs no real bank
   credential. Pass `seed_balance=false` to skip the provider call.
3. Persisted `Organization.settings.cashflow.opening_balance` → `"settings"`.
4. `0` with `opening_balance_source: "none"` so the UI prompts for one.

**Threshold** — the `min_balance_threshold` query param when supplied, else the
org's persisted `settings.cashflow.min_balance_threshold` (see below).

`get_balance` is an OPTIONAL adapter capability (`services/payment_adapters/base.py`
→ `BalanceResult`): the base-class default reports `available=False`, so existing
adapters that don't implement it are unaffected. The best-effort fetch +
fallback chain live in `services/cashflow.fetch_provider_balance` (returns `None`
on unsupported / failure — never raises).

### Persisted cash-position thresholds (`/api/analytics/cash-position-settings`)

Per-org alert thresholds persisted on `Organization.settings.cashflow` (JSON —
**no migration**), so the CFO sets them once instead of passing
`min_balance_threshold` on every request. CFO + admin (same surface as the
cash-position view); the PUT is audited (`organization.cash_thresholds_updated`,
PII-free).

- `GET /cash-position-settings` → `{ "min_balance_threshold": "STR" | null }`.
- `PUT /cash-position-settings` body `{ "min_balance_threshold": "STR" | null }`
  — money is an exact `Decimal` serialised as a JSON string (never a float);
  `null` clears it; a negative value → 422. The write preserves any other
  `cashflow` keys (e.g. a manually set `opening_balance`).

The resolver/store helpers (`services/cashflow.resolve_cash_thresholds` /
`store_cash_thresholds`) tolerate a missing/malformed `cashflow` block by
returning "no threshold" rather than raising.

## CSV export

`GET /api/analytics/export/{report}?period_days=N` returns
`text/csv` with a Content-Disposition header. Supported reports:

| `{report}` | Columns |
|---|---|
| `invoice_register` | invoice_id, invoice_number, vendor_name, amount, currency, status, invoice_date, due_date, created_at, po_number |
| `vendor_spend` | vendor_name, invoice_count, total_amount, currencies, unconverted_count |
| `payment_register` | payment_id, invoice_id, invoice_number, vendor_name, amount, currency, method, status, provider, reference, submitted_at, completed_at |
| `aging_snapshot` | as_of_date, current, days_30, days_60, days_90, days_90_plus, total. `as_of_date` defaults to `utc_today()` — neither caller passes `snapshot_date`, and both bucket against a UTC `today`, so a local-time default labelled the file with one date while the buckets were computed as of another |
| `cashflow_forecast` | period, period_start, period_end, scheduled_amount, committed_amount, pending_amount, discount_eligible_amount, count |
| `expense_register` | date, merchant, category, amount, currency, gl_code, payment_method, status, report_number |

`cashflow_forecast` is forward-looking — it takes `granularity` +
`horizon_days` (not `period_days`) and runs the same forecast query as the
JSON endpoint.

`vendor_spend`'s `total_amount` is the org's reporting-currency rollup (see
above), not a raw `SUM(amount)`. Two columns make it auditable rather than a
figure to take on trust: `currencies` lists every distinct original invoice
currency that rolled in (e.g. `"EUR, USD"`), and `unconverted_count` says how
many of those invoices were summed at **face value** because no locked rate
bridged them.

That second column matters more here than on any JSON surface. Every other
spend surface can put the disclosure beside the figure — a `role="alert"` note,
a sibling field. A CSV is opened later, in a spreadsheet, by someone with no
way back to the tenant that produced it, so a total that quietly mixes
currencies has no channel to say so at all. It is **appended** after
`currencies`, so a consumer reading by header is unaffected and one reading by
position keeps working for the four columns it already knew.

A row from a caller that exposes no `unconverted_count` (the legacy positional
tuple / attribute-only shapes) exports **blank**, not `0` — such a caller has
made no claim about convertibility, and `0` would read as "everything
converted". Same zero-versus-unknown rule as `docs/decisions.md` §34. A rollup
row that genuinely converted everything exports `0`.

### `Row` is not a tuple — duck-type the joined exporters

`payment_register` and `expense_register` take **joined** rows, and every real
caller (this endpoint and the scheduled-report runner) hands them `rows.all()`
— a list of `sqlalchemy.engine.row.Row`. A `Row` implements `Sequence` but is
**not** an `isinstance(tuple)` in SQLAlchemy 2.x, so an exporter branching on
`isinstance(row, (tuple, list))` silently misses every production call and
falls into its attribute-access fallback, where a `Row` has none of the ORM
attributes and `getattr(..., default)` swallows each miss. That shipped twice:
`vendor_spend` exported a blank `total_amount`, and `payment_register` exported
a header plus one **all-blank row per payment** (a hardcoded `USD` its only
content). Both now duck-type on `__getitem__`, so `Row` and `tuple`/`list`
share the positional path. Any new joined exporter must do the same, and its
test must exercise a genuine `Row` (`tests/test_report_export.py::_sqlalchemy_rows`
builds one with no database) — plain-tuple fixtures cannot catch this.

Column order is pinned by `tests/test_report_export.py` — finance
imports rely on column position; a reorder breaks downstream
pipelines.

The pin is a **drift guard, not a freeze**. Adding a column is allowed when it
carries something the file cannot otherwise say (as `unconverted_count` does
for `vendor_spend`), on two conditions: it goes at the **END**, so nothing a
consumer already reads by position moves; and the pin is updated in the same
commit, which is what makes the change deliberate instead of silent. A column
that is inserted, renamed, reordered or removed is the case the pin exists to
stop.

### Formula-injection guard (CWE-1236)

Every CSV export surface runs string cells through the shared
`report_export.csv_safe_cell` helper: a string whose first character is `=`,
`+`, `-`, `@`, tab or CR is prefixed with a single quote so Excel /
LibreOffice treat it as literal text instead of executing it as a formula (a
vendor name comes from AI extraction, i.e. from the attacker's own invoice
PDF). Non-string cells and signed numeric strings (`-12.34` —
Decimal-formatted amounts) pass through untouched, so money columns are
byte-identical. Applied in the `report_export` exporters (analytics export,
scheduled reports, expense export), the audit export
(`/api/audit/export?format=csv`), the report builder
(`/api/reports/{id}/export?format=csv`), the invoice bulk export
(`POST /api/invoices/bulk/export`), and the single-invoice workflow export
(`GET /api/invoices/{id}/export?format=csv`). The **Positive Pay** files are
deliberately excluded — fixed-format bank-machine uploads where a `'` prefix
would break the bank's payee exact-match (see `docs/positive-pay.md`).
Pinned by `tests/test_csv_injection.py` + `tests/test_bulk_export_csv_injection.py`.

**Dispatch is exhaustive by construction**: `export_report`'s branch-per-report
`if/elif` ends in an `else` that raises rather than falling through to any one
report's query — every key in `EXPORTERS` must have its own branch above it.
Before this was fixed (issue #120), `expense_register` had no branch and
silently fell into the `aging_snapshot` query, feeding its bucket dict into
`export_expense_register` and returning a 200 with a corrupted CSV (blank real
columns, bucket-key characters landing in `report_number`/`gl_code`). The
scheduled-report materializer (`services/scheduled_reports._materialise_rows`)
had the identical bug and mirrors the same exhaustive-dispatch-with-raise
shape.

## Scheduled report delivery

Migration 0020 adds `scheduled_reports`. Rows:

- `name` — display label
- `report_type` — one of the keys in `report_export.EXPORTERS`
- `cadence` — `daily` | `weekly` | `monthly`
- `recipients` — JSONB list of email addresses
- `period_days` — window the report covers
- `next_run_at` — when the runner picks it up
- `last_run_at` / `last_run_status` / `last_run_error`
- `enabled`

The runner (`services/scheduled_reports.run_scheduled_reports_loop`
— wired into `main.lifespan`, gated by `FEOH_SCHEDULED_REPORTS_ENABLED`,
disabled by default so local dev / tests never email) ticks every
`FEOH_SCHEDULED_REPORTS_TICK_SECONDS`. Each tick `run_scheduled_reports_once`
fans out across every tenant DB (`_sweep_tenant`), calls `list_due_schedules`,
then `execute_schedule` per due row — one tenant's failure never halts the
sweep.

**Delivery is per-recipient**, and `last_run_status` has three values:

| Status | When | `next_run_at` | Counts toward auto-disable |
|---|---|---|---|
| `success` | every recipient took the report | bumped by the cadence | — (clears the chain) |
| `partial` | some took it, some didn't | **bumped** | no |
| `failure` | generation failed, no recipients configured, or NOBODY took it | untouched → next tick retries | yes (`[retry N]`) |

A `partial` still advances `next_run_at` because the alternative is worse: a
retry redelivers to everyone who already has the report. The whole loop used to
sit inside one `try`, so a bad address at position 2 of 5 skipped positions 3-5
entirely *and* held `next_run_at`, re-sending to position 1 on each of the five
retry ticks before the auto-disable — while 3-5 never received it once and then
lost the schedule. Each address is now attempted independently, and a `partial`
is deliberately **not** a strike: disabling the schedule over one unreachable
recipient punishes the ones it is still reaching. The durable fix for a
persistently bad address is an operator correcting or removing it — the failure
count on `last_run_error` is what tells them to.

After five consecutive `failure`s the row is auto-disabled so the queue doesn't
loop forever — an operator re-enables it after fixing the underlying issue, via
`PATCH /api/analytics/scheduled-reports/{id}` (§ CRUD below). The tenant sweep
counts a `partial` as a failed run for `sweep_health`
(`GET /api/health/sweeps`), so an undelivered recipient shows up there rather
than rounding to "healthy".

Email-adapter exceptions never leak provider-side details into
`last_run_error` (invariant #7); only the exception class name — and, for a
partial, the failed/total **counts** — are stored.

### `next_run_at` advances from the DUE slot, not the tick

`execute_schedule` used to set `next_run_at = compute_next_run(cadence, now)` —
from the moment the sweep happened to run it. The sweep ticks hourly
(`FEOH_SCHEDULED_REPORTS_TICK_SECONDS`), so a "daily 09:00" report was typically
picked up at 09:37 and rescheduled for 09:37 tomorrow, then 10:14, then 11:02:
every run landed later than the last and the report walked all the way around
the clock inside a month.

`advance_next_run(cadence, scheduled_for=…, now=…)` anchors on the slot the run
was **due** at, so 09:00 stays 09:00 no matter when the tick fires. A missed
window (process down, tenant unreachable, the schedule disabled and re-enabled)
is caught up in **whole cadence steps** to the first slot strictly after `now` —
never one send per skipped period. That is deliberate: the report is a periodic
snapshot of *current* state, so a backlog burst would deliver N identical
copies. The schedule simply resumes on its own grid.

`compute_next_run` remains, for seeding a brand-new schedule (one step, no
catch-up). Both go through `known_cadences()` / `_step`, which is also what the
CRUD surface validates against, so there is no second list.

**A month is counted in months, not in 30 days.** `monthly` used to step by
`timedelta(days=30)`, and anchored on a late-month day that walks the grid off
the calendar: 31 Jan → 2 Mar → 1 Apr → 1 May → 31 May → 30 Jun. February
received **no report at all**, and the day of month slid five days inside half a
year — on a schedule whose entire contract is "once a month". `monthly` now
lives in its own `_CADENCE_MONTHS` registry and steps through the shared
`billing.period.add_months` (the tested owner of clamped month arithmetic on a
`datetime` — reused rather than re-implemented a fourth time), so every calendar
month gets exactly one run and the time of day still holds.

The day of month clamps to the target month's length (31 Jan → 28/29 Feb), and
because the next anchor is the stored slot, a 31st schedule settles on the 28th
after its first February and stays there. Holding the 31st would need an anchor
column `scheduled_reports` does not carry; a stable day is a much better answer
than the drift it replaces. Catch-up for `monthly` counts whole months from the
anchor — a schedule dormant for three years still resolves to a single next
slot, not one send per skipped month.

### `vendor_spend` runs the same query the API does

`_materialise_rows`' `vendor_spend` branch calls
`currency_conversion.vendor_spend_grouped_select` — the identical builder behind
the CFO tile, its drill-through and `GET /api/analytics/export/vendor_spend` —
so the emailed CSV, the export a CFO downloads and the tile they clicked from
agree about the population (no `rejected`), the conversion rule, and the tie
order. It passes `entity_id=None` deliberately: `ScheduledReport` has no entity,
so the emailed CSV is whole-tenant by construction, like every other branch here.

It used to stream every in-period invoice into a Python fold. That mattered more
here than on the API sites: this runs on a background sweep against every tenant
in turn, where no request timeout bounds it. Nothing else about the branch moved
— recipients, `next_run_at` and the five-strike auto-disable are untouched.

### "Today" is UTC

`_materialise_rows` slices each report's `period_days` window — and the
`cashflow_forecast` / `aging_snapshot` branches their `today` — from
`app.utils.dates.utc_today()`, not `date.today()`. `date.today()` resolves in
the *server's* local timezone; the `/analytics` export endpoints and the
cash-flow copilot resolve UTC. On a UTC container they agree, so the mixture was
latent — but on any other host an emailed snapshot and the API export of the
same report disagree at the day boundary, which is a reconciliation problem, not
a cosmetic one.

The rule is no longer local to this stack. `utc_today()` is the one definition
for the backend, and the AP surfaces converged on it too — the early-pay
discount cutoff, the past-due and future-invoice-date fraud flags, the
recurring-template period key (which IS that sweep's idempotency key), and the
regulated `Invoice.approval_date` on all three approval paths.
`tests/test_utc_today.py` holds the allowlist of converged modules and
AST-scans each one, failing on `date.today()`, `datetime.today()`,
`datetime.date.today()` **and** `datetime.now().date()` — the last is naive-now,
so a local date under a name that reads deliberate, and a purely `today`-shaped
scan would never see it. Adding a module to the allowlist is how a conversion
lands; the guard is what stops one sliding back.

### CRUD — `/api/analytics/scheduled-reports`

`app/api/scheduled_reports.py`. The runner's input surface; before it existed a
row could only be created by direct SQL, `list_due_schedules` returned `[]` on
every tick forever, and the 5-strike auto-disable was a one-way door.

| Method | Path | Roles |
|---|---|---|
| `GET` | `/api/analytics/scheduled-reports` | admin, cfo |
| `POST` | `/api/analytics/scheduled-reports` | **admin only** |
| `GET` | `/api/analytics/scheduled-reports/{id}` | admin, cfo |
| `PATCH` | `/api/analytics/scheduled-reports/{id}` | **admin only** |
| `DELETE` | `/api/analytics/scheduled-reports/{id}` | **admin only** |

Mutations are admin-only, above the `admin`/`cfo` gate the rest of `/analytics`
uses: a schedule is a standing instruction to email a CSV of the tenant's AP
spend to an arbitrary address on a recurring basis, with no review of any
individual send. That is a data-egress control, not a reporting preference.

- **Validation is against the runner's own registries** (`app/schemas/
  scheduled_report.py`): `report_type` ∈ `report_export.EXPORTERS`, `cadence` ∈
  `known_cadences()`. Neither is restated. An out-of-registry `report_type`
  raises on every tick and burns the auto-disable without ever sending; an
  unknown `cadence` silently falls back to daily, so a "yearly" row would have
  emailed 365 times a year.
- **Recipients** are shape-checked (the same permissive regex `api/signup.py`
  uses — no `email-validator` dependency), case-insensitively de-duped (a
  duplicate would double-send), and bounded at 20 with at least one required.
- `next_run_at` is optional; omitted it defaults to *now*, so the schedule fires
  on the next tick and the operator sees it work. Because `advance_next_run`
  then holds that time-of-day, pass an explicit value to pin e.g. 09:00. A value
  in the past is legitimate and is not rejected — the row is immediately due,
  then catches up in whole steps.
- `PATCH {enabled: true}` on a row the 5-strike rule disabled also **clears the
  `[retry N]` marker**. `_mark_failure` reads that prefix to count consecutive
  failures, so re-enabling without clearing it means the next failure lands at
  retry 6 and disables the row again immediately — indistinguishable, to the
  operator, from the re-enable not having worked.
- **Tenant-scoped via `get_tenant_db`, deliberately NOT entity-scoped.**
  `ScheduledReport` has no `entity_id`, and `_materialise_rows` applies no
  entity filter to any of its six report types — the emailed CSV is whole-tenant
  by construction. Stamping an entity on the schedule row would advertise a
  scope the delivered file does not honour. Making it real means entity-filtering
  the materializer *and* a migration; that is its own slice.
- Every mutation writes a PII-free audit row (`scheduled_report.created` /
  `.updated` / `.deleted`) carrying the recipient **count**, never the
  addresses — the trail is append-only and WORM-shipped, so a corrected
  distribution list could never be redacted out of it.

Tests: `tests/test_scheduled_reports_api.py` (CRUD, RBAC, tenant isolation,
validation, PII-free audit) and `tests/test_scheduled_reports.py` (the runner +
the cadence anchoring).

## Known gaps

- **Cash conversion cycle**: AP-only; DSO + DIO need data we
  don't carry. Returns NULL; the UI renders "needs receivables
  data" rather than a misleading 0.

## Tests

| File | Coverage |
|---|---|
| `tests/test_analytics.py` | Every compute_* function: DPO formula, CCC None-on-missing-legs, working-capital monotonicity, supplier concentration flag threshold, fraud-rate **not-computable** on a zero-invoice month (`None`, never `0`), rebate annualisation, forecast-variance sign convention, processing-time min-sample collapse, approval-bottleneck rollup + unassigned bucket, discount-capture three-way split (open window is `pending`, not `missed`) and its no-decided-rows `None` rate; and `discount_capture_from_grouped_rows` — the reducer `api/dashboard` actually calls — driven against `compute_discount_capture` over the same population across six bucket mixes, plus its reporting-amount / `unconverted_count` pass-through and its refusal of a bucket name outside the vocabulary |
| `tests/test_report_export.py` | Registry pins all six reports; per-report header column-order pinned (one shared `_VENDOR_SPEND_HEADER` constant, so the three `vendor_spend` cases cannot disagree about it); enum-status reads `.value`; missing fields emit empty (not "None"); orphan payment-with-null-invoice still emitted; `vendor_spend`'s `unconverted_count` — a real `0` from a fully-converted rollup row, a real count from a part-converted one, and **blank** from a legacy caller that exposes no such attribute, so "we did not ask" never renders as "none" |
| `tests/test_scheduled_reports.py` | 20 cases — cadence delta math; unknown-cadence fallback; happy-path generates → emails every recipient → updates next_run_at; generator-error / empty-recipients / email-adapter-error all persist a failure marker without raising; PII guardrail (no SMTP transport details in `last_run_error`); five-consecutive-failures disables the row, first failure leaves enabled alone; **per-recipient delivery** — one bad address doesn't block the ones after it, a partial advances `next_run_at` and isn't a strike, a total failure holds `next_run_at` and still attempts every address; **cadence anchoring** — 30 late ticks in a row leave the 09:00 slot at 09:00, a dormant fortnight catches up to ONE next slot rather than 14 sends, an exactly-due slot still moves forward (no busy-loop), a future slot takes exactly one step, a naive `scheduled_for` reads as UTC |
| `tests/test_scheduled_reports_api.py` | 23 cases — CRUD round-trip; the created row is what `list_due_schedules` picks up; `report_type` / `cadence` validated against the runner's own registries (create AND patch); recipient list shape-checked / de-duped / bounded / non-empty; our validator message names no address; RBAC (mutations admin-only, reads admin+cfo, ap_manager/ap_clerk refused); tenant isolation (list, get, patch); PII-free audit rows carrying the recipient COUNT; re-enabling a 5-strike-disabled row clears the stale `[retry N]` marker |
| `tests/test_utc_today.py` | Drift guard — `utc_today()` is the UTC calendar date; an AST scan fails on any `date.today()` / `datetime.today()` / `datetime.date.today()` reappearing in the modules that have converged on it (the cash-flow stack, plus the AP surfaces: discounts, portal, dashboard, payments queue, recurring, workflow, review, extraction, invoice warnings, 1099, Positive Pay, the exporters). The scanner itself is a tested helper — the module-attribute spelling `datetime.date.today()` slipped past the first version, which is how two Positive Pay modules could have been listed as converged while still reading local time, and naive `datetime.now().date()` isn't spelled `today` at all |
| `tests/test_import_provenance_backfill.py` | The operator-run pre-marker backfill (`scripts/backfill_import_provenance.py`): cutover parsing (date vs ISO instant, naive-as-UTC, future and unparseable refused, no default — the operator must assert it); the marker's shape and its `asserted` flag; and on real Postgres — dry run is the default and writes neither marker nor audit row, `--apply` stamps only un-marked rows created strictly before the cutover (a row exactly on it is native), a re-run stamps 0 and leaves an existing `csv_import` marker byte-for-byte alone, a pre-cutover row in a status the importer cannot produce is reported and never stamped (and that status set is read from `csv_import`, not restated), the manifest audit row carries counts but no invoice number or vendor, and the stamped rows leave the touchless population through `imported_invoice_clause` itself |
| `tests/test_touchless_rate_population.py` | The touchless numerator's POPULATION (as opposed to its arithmetic): a `new -> done` shortcut and a CSV-imported `paid` are in neither leg, a genuinely approved `done` still is, a rejection is denominator-only exactly as before, and the never-reviewed rows leaving the denominator are the ONLY denominator movement. Plus structural guards re-derived from `VALID_TRANSITIONS` and `csv_import._IMPORTABLE_INVOICE_STATUSES`, so a new legal edge or a newly-importable status cannot quietly re-widen the metric |
| `tests/test_dashboard_aggregations.py` | Existing — extended through the new branches via the try/except absorption pattern |
| `tests/test_dashboard_vendor_spend.py` | The top-vendor tile's SQL `GROUP BY` (§ Vendor spend is grouped in SQL) — equivalence against the Python fold it replaced, over three seeds of a book whose vendor / amount / status / date / currency / rate-lock are drawn **independently** (correlated generators hide aggregation bugs, `decisions.md` §82), each covering all four rate-lock cases and an excluded blank-vendor row; the ordering rule at a tie that straddles the rank-10 cutoff (alphabetical, inserted in reverse so a total-only sort surfaces) and its stability across repeated requests; multi-currency conversion incl. the face-value fallback for an unconvertible row and a lock denominated in a third currency (with the whole-book `unconverted_count` pinned alongside, so the silent fallback stays a known trade-off); `X-Entity-ID` narrowing; rejected exclusion; empty tenant; the `unconverted_count` disclosure on all three blocks — per vendor (compared against the Python fold's own counts over the same three randomised seeds), the `aging_reporting` band set, and per `monthly_trend` bar — plus the lock-amount-without-a-lock-currency row that the endpoint's old inline CASE reported as converted |
| `tests/test_dashboard_aggregates.py` | Real-Postgres guards for the four aggregates that were each wrong in their own way: `total_paid` vs its converted `total_paid_reporting` counterpart under mixed currency; `discount_capture`'s elapsed-window gate (open window → `pending`, elapsed → still a `missed`), its reporting-currency amounts + `unconverted_count`, all three buckets partitioning correctly in one request with several rows each (the shape that catches the `CASE`/`GROUP BY` port going wrong), and a payment at 23:30 UTC on the deadline itself still capturing; `touchless_rate` counting `sending_to_erp` and an approval-stamped `failed` while ignoring an extraction-failed one, and excluding a `done`/`paid` row that reached its terminal status without ever being approved; `monthly_trend` returning six WHOLE calendar months with no partial oldest bar and no seventh stub bucket (both window shapes pinned against a frozen `utc_today`) |
| `tests/test_analytics_trend_insufficient_data.py` | The two "reported a comfortable number where there was none" surfaces: `compute_fraud_rate_trend` returning `None` + `insufficient_data` for a zero-invoice month (including the zero-invoices-with-exceptions shape) while still reporting a genuine 0%, and end-to-end `null` on the `/cfo` wire; `/forecast_variance` resolving `actual` into the reporting currency under mixed currency, excluding-and-disclosing an unexpressible payment, and answering `422` (not `500`) for `2026-13` / `2026-00` / `2026-99` / `0000-01` / `2026/07` |
| `tests/test_cashflow_balance.py` | Unit — `get_balance` capability (base-class default unsupported; mock deterministic + config override + simulated-unsupported); `fetch_provider_balance` best-effort (mock balance, None on unsupported, swallows adapter error); persisted-threshold resolve/store round-trip + garbage tolerance + key preservation/clear |
| `tests/test_cashflow_forecast_api.py` (cash-position additions) | API — auto-seed opening balance from the mock provider (`source: provider`); `seed_balance=false` skips it; query param beats provider; provider-unsupported falls back to `settings`; persisted threshold applied without a query override; `cash-position-settings` GET/PUT round-trip; negative → 422; RBAC (ap_clerk 403, admin/cfo 200) |
| `tests/test_analytics_money_serialization.py` | Money serialisation — a **structural** guard over `/cfo`, the cash-flow trio, both drill-throughs, `/forecast_variance` and `/by-entity`: every response is walked and any JSON *number* whose key isn't in the declared day-count / percentage / count roster fails, so a new money field added as a float can't land silently. Plus the zero-population `/cfo` response (where `0` and `"0"` both read as "nothing here"), the `null` cash-position threshold, and the exact seeded figures surviving the round trip |
| `tests/test_analytics_rollup_sql.py` | The per-vendor spend `GROUP BY` (§ Per-vendor spend is one query) — equivalence against the row-at-a-time `vendor_rollup_to_reporting_currency` over three seeds of an independently-randomised book covering all four rate-lock cases, a rejected-and-blank-vendor exclusion and the vendor-name tiebreak at a tie; the `unconverted_count` disclosure per vendor, on `/cfo`'s `supplier_concentration` (non-zero AND zero-on-a-clean-book, so the field can distinguish), and on `/drill/spend_concentration` both per row and whole-period-before-`?limit=`; plus two structural guards — an AST scan failing any `app/` caller that reintroduces the per-invoice fold, and shape assertions that the builder carries no `LIMIT` and does its conversion inside the aggregates; the assistant tool's own two cases (same-named vendors kept apart by `vendor_id`, and convert-before-summing); and the drift guard, which checks the SHAPE — a collapsing reducer called from `app/` (now including `compute_discount_capture`, the dashboard's discount-tile fold), or `reporting_amount_for_row` folded into a per-key subscript — with a second test proving the detector fires on both and stays quiet on a projection and a scalar total |
| `tests/test_analytics_by_entity.py` | `/by-entity` — per-entity spend/invoice-count scoping for two entities; `consolidated` equals the cross-entity sum; open-exceptions scope per entity; single-entity tenant returns a coherent one-row breakdown; RBAC (ap_clerk/ap_manager 403, cfo 200); the endpoint ignores `X-Entity-ID` |
