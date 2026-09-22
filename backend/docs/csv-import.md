# CSV import — Day-0 data migration

New tenants rarely start from a blank slate. They bring an existing
vendor list and a stack of open AP from whatever tool they're replacing.
The CSV importers let you load both in a few minutes instead of
hand-keying them or building a throw-away Bill.com → Better-AP ETL.

Two endpoints, both `admin` / `ap_manager` only:

| Endpoint | What it does |
|---|---|
| `POST /api/vendors/import-csv` | Bulk-create vendors. Dedup by `code` > case-insensitive `name`. |
| `POST /api/invoices/import-csv` | Bulk-create invoices. Unknown vendors get auto-created stubs. Dedup by `(vendor, invoice_number)`. |

Both accept `multipart/form-data` with a single `file` field. Response is
JSON with `imported`, `skipped`, and a per-row `errors` list. Files must
be UTF-8.

Each import writes one PII-free summary audit row (`vendor.imported_from_csv`
/ `invoice.imported_from_csv`, keyed on the org, counts only — never a vendor
name or bank detail) so a bulk load of the tenant's ledger is on the
append-only trail (invariant #3).

## Vendor CSV columns

Only `name` is required. Unknown columns are ignored — hand us the raw
export from the customer's existing system.

| Column | Required | Notes |
|---|---|---|
| `name` | yes | Vendor display name |
| `code` | no | Internal vendor code / ERP ID. Used for dedup first. |
| `email` | no | AP contact email |
| `phone` | no | |
| `address` | no | |
| `tax_id` | no | TIN / EIN / VAT number |
| `payment_terms` | no | e.g. "Net 30" |
| `accepts_virtual_cards` | no | `true` / `false` / `yes` / `no` / `1` / `0` |

Example:

```csv
name,code,email,payment_terms,accepts_virtual_cards
Acme Supplies,ACME,ap@acme.com,Net 30,true
Globex Corp,GLBX,billing@globex.com,Net 15,false
```

Newly-created vendors land with `status='unverified'` — the AP Manager
should review them on the Vendors page before paying any invoices.

## Invoice CSV columns

| Column | Required | Notes |
|---|---|---|
| `invoice_number` | yes | |
| `vendor_name` | either | Resolved case-insensitive against existing vendors |
| `vendor_code` | either | Tried first; falls back to name |
| `amount` | yes | Accepts `1234.56`, `"$1,234.56"`, `"1,000"` (quote if it contains commas!) |
| `currency` | no | Default `USD` |
| `invoice_date` | no | `YYYY-MM-DD`, `MM/DD/YYYY`, `DD/MM/YYYY`, `YYYY/MM/DD` |
| `due_date` | no | Same formats |
| `po_number` | no | |
| `description` | no | |
| `gl_account` | no | A code that exists **only in another entity's chart** is refused per row (before its vendor is resolved, so the row leaves nothing behind) — it would resolve against the wrong chart. A code in no chart at all still imports: history carries retired accounts. See `services/gl_chart.py`. |
| `cost_center` | no | |
| `status` | no | Default `done`. Only `new`, `done`, `paid`, `rejected` are importable — a live pipeline stage (`approved`, `ready_for_review`, `payment_scheduled`, …) is rejected per row (issue #174). |

Example:

```csv
invoice_number,vendor_name,amount,invoice_date,due_date,status,po_number
INV-1001,Acme Supplies,1250.00,2025-12-01,2026-01-01,done,PO-9001
INV-1002,Globex Corp,"$2,500.50",2026-01-15,2026-02-15,paid,
```

## Choosing the right status

For a Day-0 historical load (invoices that already existed and were
already paid in the old system), use `done` or `paid` so they don't
re-enter the approval pipeline. For open AP that still needs to be paid,
import it as `new` — it enters the normal approval pipeline and gets a real
audit trail, segregation check, and approval signature when a human approves it.

**Importable statuses are restricted to `new`, `done`, `paid`, `rejected`.** A
CSV import bypasses the workflow engine, so landing an invoice directly at a
live pipeline stage (`approved`, `ready_for_review`, `payment_scheduled`, the
ERP-send states, …) would drop a fabricated, payable invoice into the queue
with no audit row and no second approver — so those statuses are rejected per
row (issue #174). Never try to import open AP as `approved`; import it as `new`.

## The importer is recorded as the uploader

Every invoice the importer creates carries `Invoice.uploaded_by_id` — the AP
user who ran the import — exactly as a hand-keyed invoice
(`POST /api/invoices`) and a file upload (`POST /api/workflow/upload`) do.

That column is what segregation of duties is keyed on:
`services/approval_chain.violates_segregation` compares it to the approver and
returns **False — no breach — when it is NULL**, because a NULL uploader means
"no employee created this row" (system ingestion, a supplier, a sweep). The
importer never set it, so an imported invoice read as one of those, and the AP
manager who imported a payable at `new` could turn round and approve it — while
the same person doing the same thing through `POST /api/invoices` got a 403.
The status allowlist above forces open AP through approval; the stamp is what
makes that approval an actual second pair of eyes.

Consequences worth knowing:

- **A single-operator tenant now needs the escape hatch it always had.** Set
  `require_segregation: false` on the approval step config — the same explicit
  opt-out every other creation path already required.
- **The stamp lands on every row of a batch**, not just the first, so a long
  import cannot smuggle an unattributed, self-approvable payable in behind an
  attributed one.
- It is independent of the `invoice.imported_csv` audit row, which records the
  same actor for a different purpose (who did it, for the auditor) —
  `uploaded_by_id` is the value a *control* reads at approval time.

## Import provenance — `meta["imported"]`

Every invoice row the importer creates is stamped, on the existing
`Invoice.meta` JSONB bag (no migration, no new column):

```json
"imported": { "at": "2026-09-05T11:02:44.918273+00:00", "source": "csv_import" }
```

* **Presence of the key is the marker.** Nothing parses the value, so a
  truncated or hand-edited `at` still reads as "imported" rather than quietly
  flipping the row back to native.
* **A nested object, not a flat `imported_at`**, so later provenance fields
  (batch id, importing user) extend it without colonising more top-level keys
  in a bag shared with `audit_summary` (`services/audit_summary`) and
  `archived_at` (`services/retention_sweep`). Both of those writers merge into
  a copy of the existing dict, so the marker survives them.
* **`source` names the writer**, so a future importer (an ERP backfill, a
  migration tool) can mark rows the same way and stay distinguishable.
* One stamp per batch — every row in a single import shares the instant the
  import ran, which is the fact being recorded.

**Why it exists.** An imported invoice is history migrated in from whatever the
tenant used before; the workflow engine never ran on it. Metrics that describe
*this platform's* automation therefore have to exclude it, and status cannot
identify it — `done`, `paid` and `rejected` are each reachable both by import
and natively. The first consumer is the dashboard's touchless rate, which
subtracts marked rows from **both** its legs; see
`backend/docs/analytics.md` § Imported rows are outside the metric.

`imported_invoice_clause()` / `native_invoice_clause()` in
`services/csv_import` are the SQL predicates — use them rather than
re-deriving the key, and note that the `?` operator returns NULL (not false)
on a SQL-NULL `meta`, which is why the positive clause carries an
`IS NOT NULL` guard.

**No backfill.** Rows imported before the marker shipped carry no key and are
read as native. That is deliberate: absence of the marker means "we do not
know", and inventing provenance for a historical row is exactly the guessing
the marker exists to avoid.

## Audit trail

Both importers write an append-only audit row for **every row they create**,
from inside `services/csv_import` (which is why neither endpoint carries a
`dispatch_audit` of its own — the cross-module chokepoint is recorded in
`tests/test_audit_append_only.py::_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT`):

| Action | Written for | `details` |
|---|---|---|
| `vendor.imported_csv` | Each vendor created — by the vendor importer, and by the invoice importer when it mints a stub for an unknown supplier | `name`, `code`, `source` |
| `invoice.imported_csv` | Each invoice created | `invoice_number`, `status`, `vendor_id`, `source` |

Why it matters: a CSV import **bypasses the workflow engine entirely** and can
land an invoice directly at `paid` or `done` — a state a native invoice can only
reach through an approval, a segregation check and an approval signature.
Without these rows such an invoice had a completely empty audit trail, which is
precisely the row a SOX auditor most wants attributed. The same goes for a
vendor: an import mints **payee** rows, and `POST /api/vendors` has always
written `vendor.created` for the single-row path.

Two properties worth knowing:

- **The invoice row is keyed on the invoice's own `correlation_id`.**
  `GET /api/audit/invoice/{id}` and `GET /api/invoices/{id}/audit-log` resolve an
  invoice's trail by joining `AuditLog.correlation_id` to `Invoice.correlation_id`,
  so a row keyed any other way would be invisible on the very invoice it
  describes. Both ids are column defaults populated by the INSERT, so the
  importer flushes the batch *before* writing its audit rows.
- **Skipped and errored rows write nothing.** Nothing was created, so there is
  nothing to attribute — a duplicate that dedups out leaves no evidence behind.

`details` is PII-free by construction: `name` / `code` for a vendor (the same
two fields `vendor.created` records) and `invoice_number` / `status` for an
invoice. The `tax_id`, `email`, `phone` and `address` columns the vendor CSV
accepts are written onto the vendor row but **never** into the trail, which is
append-only, undeletable (migration 0022) and shipped to a WORM store.

The importing user rides the row as `actor_id`; the API handlers pass it in.
A direct caller of `import_vendors_csv` / `import_invoices_csv` that omits
`actor_id` produces system-attributed rows.

## What the import does NOT do

- **No file attachment.** The importer creates the AP records; it does
  not upload the original PDF. If you need the file stored, upload it
  through the normal invoice upload endpoint after the row lands.
- **No extraction.** No AI is run on imported rows. Confidence is
  blank, line items are empty.
- **No ERP sync.** Imported invoices stay local until they flow through
  the ERP-export step like any other invoice.
- **No workflow instance.** Terminal-status imports (`done`, `paid`)
  skip the workflow engine — which is exactly why in-flight statuses are
  no longer importable (they'd bypass the approval controls). Land open AP
  as `new` and let it flow through the normal pipeline.

## Preparing a customer's export

Most AP tools can export to CSV directly. A few quick translations:

- **Bill.com**: Vendors and Bills both export from `Reports → Bills` /
  `Reports → Vendors`. Rename `Vendor Name` → `vendor_name`,
  `Amount` → `amount`, etc.
- **QuickBooks Online**: `Reports → Expenses by Vendor Summary` for
  vendors; `Reports → Bills and Applied Payments` for open AP.
- **Xero**: `Contacts → Export`; `Bills to Pay` view → export.
- **NetSuite**: Saved search → CSV export.

Customers with unusual exports can hand you their raw CSV and you
rename the columns. The importer tolerates extra unknown columns so
you don't have to strip them.

## Amount range

`amount` is written into a `Numeric(15, 2)` column — **13 integer digits**, i.e.
just under 10 trillion in the invoice's currency. A cell wider than that is
reported as `amount invalid: …` for that row and the rest of the import
proceeds.

That bound is enforced in `_parse_decimal` (`services/csv_import.py`) via the
shared `services/numeric_bounds.fits_numeric`. Without it an over-range cell
parsed cleanly and raised `NumericValueOutOfRangeError` at the flush — which is
worse than a rejected row, because the import had already written the rows
before it.

**Extra decimal places are rounded, not rejected.** `1234.567` imports as
`1234.57`, matching what Postgres does. A migration file from a customer's old
system may legitimately carry more precision than the column, and dropping whole
rows over a third decimal would lose more than it protects. (The JSON API is
stricter — it answers 422 there, because a client authored that one value and
can correct it. See `api-reference.md` § Money and quantity fields.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CSV must be UTF-8 encoded` | Re-save from Excel as "CSV UTF-8". Don't use the default `CSV (Comma delimited)` option on Windows. |
| `amount invalid: '99999999999999999999'` | The amount exceeds `Numeric(15, 2)`'s 13 integer digits. Check for a misplaced decimal separator or a column shift. |
| `status invalid: '2026-03-01'` | An amount had an unquoted comma and shifted all columns. Wrap comma-containing values in double quotes. |
| Many `vendor_name or vendor_code is required` errors | The vendor column name in the CSV doesn't match. Rename it before upload. |
| Duplicate vendors show up twice | Customer export has inconsistent casing. Add a `code` column to dedup cleanly. |
