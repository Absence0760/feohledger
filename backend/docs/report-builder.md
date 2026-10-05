# Custom (ad-hoc) Report Builder

Self-serve reporting: a user picks a data source, group-by dimensions, aggregate
measures, and filters, then runs / saves / exports the result. Complements the
fixed-shape Dashboard, CFO analytics, and scheduled reports (`docs/analytics.md`)
by letting finance users build their own reports without engineering.

- **API surface:** `app/api/reports.py` (`/api/reports`)
- **Security boundary + query engine:** `app/services/report_builder.py`
- **Schemas:** `app/schemas/report.py`
- **Model + migration:** `app/models/report_definition.py`, `alembic/versions/0071_report_definitions.py`
- **Tests:** `backend/tests/test_report_builder.py`

## Security model — whitelist-only query building

This is the load-bearing invariant. The client **never** sends a raw SQL
fragment, column name, or table name that reaches the database. It sends
**catalog keys** only. `report_builder.REPORT_SOURCES` is a hardcoded whitelist
mapping each key -> a real, server-defined SQLAlchemy column, plus the
aggregations / filter operators / date grains that key is allowed to use.

`compile_spec(spec)` validates every reference (data source, dimension, measure,
aggregation, filter, operator, date grain, sort key) against the catalog
**before any SQL is built**. Anything not in the catalog raises
`ReportValidationError`, which the router turns into a **422** — and is never
compiled into a query. Filter values are always bound as parameters (never
string-interpolated) and coerced to the column's Python type (bad value -> 422).

Every run also goes through the tenant `get_tenant` chokepoint (tenant
isolation) and honours the `X-Entity-ID` header via `apply_entity_scope` — the
same scoping as the rest of analytics. Money measures are serialized as **exact
decimal strings**, never floats.

## The catalog (`REPORT_SOURCES`)

Four sources ship. Each exposes group-by **dimensions**, aggregate **measures**,
and **filters**. `GET /api/reports/catalog` returns this shape for the frontend.

| Source | Dimensions | Measures | Filters |
|--------|-----------|----------|---------|
| `invoices` | vendor_name, status (enum), currency, gl_account, cost_center, department, project, payment_terms, invoice_date (date), due_date (date), created_at (date) | amount (money), tax_amount (money), id (count) | status, vendor_name, currency, gl_account, department, project, amount, invoice_date, due_date, created_at |
| `payments` | status, method, provider, corridor, currency (the invoice's), created_at (date), submitted_at (date), completed_at (date) | amount (money), id (count) | status, method, provider, amount, created_at, completed_at |
| `vendors` | status, source, risk_level, kyc_status, payment_terms, created_at (date) | id (count), risk_score (number: avg/min/max) | status, source, name, risk_level, kyc_status, created_at |
| `expenses` | category, status (enum), payment_method (enum), merchant, currency, expense_date (date), created_at (date) | amount (money), id (count) | status, category, payment_method, merchant, currency, amount, expense_date, created_at |

- **Dimensions** have a `type` of `string` | `date` | `enum`. Date dimensions
  support a `grain` of `day` | `month` | `quarter` | `year` (default `month`),
  bucketed via `date_trunc`. Enum dimensions carry `enumValues`.
- **Measures** declare their allowed `aggs` (subset of `sum` / `avg` / `count` /
  `min` / `max`) and a `type` of `money` | `number`. A `count` result is always
  a `number` regardless of the underlying column. The output column key is
  `<measure_key>_<agg>` (e.g. `amount_sum`, `id_count`).
- **Filters** declare their allowed `ops` (subset of `eq` / `ne` / `gt` / `gte`
  / `lt` / `lte` / `in` / `contains` / `between`); the server rejects any op not
  in a filter's list. `in` needs a non-empty list; `between` needs a `[low, high]`
  pair; `contains` is a parameterised `ILIKE`.

Adding a field is a one-line catalog entry pointing at a real column — never a
free-form column name from the client.

### A date filter means the calendar DAY, even on a timestamp column

Some catalog date filters sit on real `DATE` columns (`invoice_date`,
`due_date`, `expense_date`); the rest sit on `TIMESTAMP`s (`created_at`,
`submitted_at`, `completed_at`). Binding a bare date onto a timestamp column
resolves to that day's **midnight**, which answers a question nobody asked:
`created_at <= 2026-06-30` dropped every row recorded after 00:00:00 on the
30th (i.e. essentially the whole day), `created_at = 2026-06-30` matched only
rows recorded exactly at midnight, and `created_at > 2026-06-30` swept the 30th
back *in*.

`report_builder._build_where` therefore translates a `date`-typed filter over a
timestamp column into half-open `[day, day+1)` bounds — `eq` → `>= day AND <
day+1`, `lte` → `< day+1`, `gt` → `>= day+1`, `between [lo, hi]` → `>= lo AND <
hi+1` (inclusive of BOTH end days), and so on. Day boundaries are **UTC**, the
same day the rest of the app reports on (`utils/dates.utc_today`), so the answer
doesn't move with the database session's `TimeZone`. The translation is
sargable — no per-row `::date` cast — so an index on the column still applies.
Filters on real `DATE` columns are untouched. Guarded by
`test_date_filter_on_timestamp_column_covers_the_whole_day` /
`test_date_filter_on_real_date_column_is_unchanged`.

**Date buckets use the same UTC day.** A date dimension over a timestamp column
is bucketed as `date_trunc(grain, timezone('UTC', col))`. It used to be
`date_trunc(grain, col::timestamp)`, and that cast converts a `TIMESTAMPTZ`
through the session's `TimeZone` — so on an Auckland-zoned session a row
recorded at 2026-01-31 20:00 UTC was admitted by a January `created_at` filter
and then reported in the **February** bucket. A `DATE` column (`invoice_date`)
has no instant and is bucketed as-is. Guarded by
`test_date_bucket_ignores_the_session_timezone` (runs under
`SET LOCAL TIME ZONE` for three zones) and
`test_date_bucket_on_a_real_date_column_is_unshifted`.

### A money aggregate is never a sum across currencies

Every source with a money measure names its **currency dimension**
(`Source.currency_dimension`): `invoices.currency`, `expenses.currency`, and for
`payments` the **invoice's** currency, reached by a catalog-defined outer join —
`Payment.amount` is denominated in its invoice's currency and the payment row
carries none of its own (`currency_conversion.payment_reporting_amount_sql`).

Whenever a spec selects a money aggregate (`sum` / `avg` / `min` / `max` of a
money measure) and does not already group by that dimension, `compile_spec`
appends it to the group-by. The result has one row per currency per group, the
currency is a visible `dimension` column, and each money measure column carries
`currency_key: "<that dimension's key>"` so a client labels every cell with the
code on **its own row**. Before this, "Spend by vendor" over a vendor billing in
USD and EUR returned one row reading `150.00` for USD 100 + EUR 50 — a figure in
no currency. A `count`-only report is not split (a count has no currency), and a
spec that already groups by currency is left as written.

Converting into the reporting currency instead was considered and not taken:
`invoice_reporting_amount_sql` falls back to face value for an unconverted
invoice, which re-creates the mixture under a reporting-currency label, and
payments have no face-value fallback at all. Splitting is exact and needs no
rate. Reasoning: `docs/decisions.md` §228. Guarded by
`test_money_sum_is_split_by_currency_when_not_grouped_by_it`,
`test_payment_sum_is_split_by_its_invoices_currency` and the catalog drift guard
`test_every_money_measure_has_a_currency_dimension`.

## Endpoints (`/api/reports`, JWT + tenant-gated)

| Method + path | Roles | Purpose |
|---|---|---|
| `GET /catalog` | all four | The catalog (sources + dimensions/measures/filters). |
| `GET /` | all four | List saved definitions (entity-scoped). |
| `POST /` | admin/ap_manager/cfo | Save a spec as a named `ReportDefinition` (201). Validates against the catalog. |
| `GET /{id}` | all four | A saved definition. |
| `PATCH /{id}` | admin/ap_manager/cfo | Update a saved definition (re-validated). |
| `DELETE /{id}` | admin/ap_manager/cfo | Delete (204). |
| `POST /run` | all four | Run an ad-hoc spec (paginated), not saved. |
| `POST /{id}/run` | all four | Run a saved spec. **`page` / `page_size` are QUERY params here**, unlike the ad-hoc sibling which takes them in the body — a client that sends them in the body gets page 1 every time, silently. |
| `GET /{id}/export?format=csv\|pdf` | all four | Branded download. |

Every mutation (`POST` / `PATCH` / `DELETE`) writes a PII-free audit row
(`report.created` / `report.updated` / `report.deleted`, `entity_type =
report_definition`, details = `{name, data_source}`).

## JSON shapes

`ReportSpec`:

```json
{
  "data_source": "invoices",
  "dimensions": [ { "key": "vendor_name", "grain": null } ],
  "measures":   [ { "key": "amount", "agg": "sum" }, { "key": "id", "agg": "count" } ],
  "filters":    [ { "key": "status", "op": "in", "value": ["approved","paid"] },
                  { "key": "invoice_date", "op": "between", "value": ["2026-01-01","2026-06-30"] } ],
  "sort":       [ { "key": "amount_sum", "dir": "desc" } ],
  "limit":      null
}
```

- `POST /run` body = `ReportSpec` + `{ page?, page_size? }` (default `page=1`,
  `page_size=100`, cap `1000`). `page` has a floor (`>= 1`) but no ceiling; a
  page starting past the last matching row short-circuits to an empty `rows`
  with the true `total_rows`, and never issues the query — which is both
  equivalent and the thing that keeps a huge `page` from overflowing the int64
  `OFFSET` bind into an asyncpg `DataError` (a 500).
- `POST /` body = `ReportSpec` + `{ name, description? }`.
- `sort[].key` is a measure output key (`<measure_key>_<agg>`) or a dimension key
  that is actually selected — otherwise 422.

`ReportResult`:

```json
{
  "columns": [ { "key": "vendor_name", "label": "Vendor", "kind": "dimension", "type": "string" },
               { "key": "currency",    "label": "Currency", "kind": "dimension", "type": "string" },
               { "key": "amount_sum",  "label": "Sum of Amount", "kind": "measure", "type": "money",
                 "currency_key": "currency" },
               { "key": "id_count",    "label": "Count", "kind": "measure", "type": "number" } ],
  "rows":    [ { "vendor_name": "Acme", "currency": "USD", "amount_sum": "12345.67", "id_count": 42 },
               { "vendor_name": "Acme", "currency": "EUR", "amount_sum": "800.00", "id_count": 3 } ],
  "total_rows": 137,
  "page": 1, "page_size": 100
}
```

Money values in `rows` are exact decimal **strings**; counts are ints; date
dimensions are ISO date strings. A money column's `currency_key` names the
dimension column holding that row's currency; the web `ResultTable` renders each
cell through `<Money currency={row[currency_key]}>` (`types/reports.cellCurrency`),
bare when the row carries no code.

A money measure is `null` (an empty cell in the CSV/PDF) when the group had
nothing to aggregate — `min` / `max` / `avg` over a column that is NULL on every
row of the group. `sum` and `count` still report `"0.00"` / `0` there, because a
total of nothing is meaningfully zero; a *minimum* or *average* of nothing is
not, and answering `"0.00"` would put a money figure nobody recorded in front of
a CFO. Guarded by `test_empty_money_aggregate_is_blank_not_zero`.

`ReportDefinition` = `ReportSpec` + `{ id, name, description, created_by_user_id,
created_at, updated_at }`.

## Persistence

`ReportDefinition` is tenant-scoped + entity-scoped (`EntityMixin`), spec
fragments stored as JSONB (`dimensions` / `measures` / `filters` / `sort`), the
row limit as `row_limit` (the JSON key is `limit`). Migration
`0071_report_definitions` creates the table gated on the `invoices` table
existing, so it no-ops on the control plane and fans out to every tenant DB via
`scripts/migrate_all_tenants.py`. Fresh tenants get the table from `create_all`
in `tenant_provisioning`.

## Export

`GET /{id}/export?format=csv|pdf` runs the saved spec **bounded to
`_EXPORT_MAX_ROWS` = 1000 rows** (`app/api/reports.py`) and renders:

- **CSV** — a `#`-comment brand-provenance header (`report_export.brand_provenance_header`)
  followed by a column-positional grid (column labels as the header row). Data
  cells pass through the shared `report_export.csv_safe_cell` formula-injection
  guard (CWE-1236) — dangerous-prefixed strings get a `'` prefix; decimal money
  strings and non-string cells pass through byte-exact.
- **PDF** — the branded analytics-report renderer
  (`services/analytics_report_pdf.render_analytics_report_pdf`), the same
  white-label chrome as the analytics export surface.

Both reuse the shared helpers so the brand treatment matches every other export.

The download filename is `<report name>_<YYYY-MM-DD>.<ext>`, and the report name
is user-chosen free text — so the header is built by the shared
`utils/http.content_disposition_attachment` (RFC 6266: a sanitized ASCII
`filename=` fallback plus a percent-encoded UTF-8 `filename*=`), never
interpolated raw. A raw f-string had two failure modes: a non-latin-1 name
("报表", an emoji) raised `UnicodeEncodeError` when Starlette latin-1-encoded the
header value — an unhandled 500 on every export of that report — and a name
containing `"` or `\` broke out of the quoted-string form so clients saved the
file under a truncated name. Guarded by
`test_export_filename_survives_an_awkward_report_name`.

### Row-cap truncation is surfaced in the file, not silent

`run_report`'s `total_rows` is the full matching-row count (only capped by the
spec's own `limit`, if the saved definition set one); `rows` is bounded by
`_EXPORT_MAX_ROWS`. When a report matches more rows than the export cap
returns, `export_report` treats that as `total_rows > len(rows)` and marks the
export truncated — the file itself says so instead of quietly ending at row
1000:

- **CSV** — one extra trailing row after the data grid: `NOTE: Results
  truncated at 1000 rows (showing 1000 of <total> matching rows) — refine your
  filters or export in batches.` (written through the same `safe_csv_writer`
  as every other row).
- **PDF** — the same note rendered as a footer line below the table
  (`AnalyticsReportContext.note`, `analytics_report_pdf.render_analytics_report_pdf`).

A result that fits within the cap gets no note at all — the indicator only
ever appears when rows were actually cut. There's no server-side pagination on
the *export* itself; a report exceeding the cap should be refined (narrower
filters / date range) or exported in batches via saved variants.
