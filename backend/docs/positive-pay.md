# Positive Pay / Payment Fraud File

Positive Pay is a bank-side **fraud control**. We hand our bank a file of the
cheques we actually *issued* (a `check_issue` file, generated per payment run) —
or, for ACH debit-block, the list of accounts authorized to debit us (a
standalone `ach_authorization` file). When an item is later presented for
payment, the bank matches it against our issued file and **refuses to clear**
anything that doesn't line up — an altered amount, a cheque we never wrote, or
an unauthorized ACH originator. Without it, a forged or altered cheque clears
silently; with it, the bank stops it and asks us first.

This is the feature behind roadmap **Positive Pay / Payment Fraud File**. It's a
natural extension of the existing `checkeeper` check-printing + payment-rail
adapters, and a frequent enterprise-AP procurement requirement.

## PII invariant (read this first)

This feature is squarely in PII scope. The rule:

- **The rendered file legitimately contains full account / routing numbers** —
  that *is* its purpose (the bank needs them to match). It lives **only** in
  MinIO under `file_key`, behind the read-role gate, and is never logged.
- **Everything else carries no full account number.** The DB row stores
  `account_last4` only; audit `details`, log lines, and HTTP error bodies carry
  masked or no account data. The pure return classifier
  (`classify_presented_items`) never sees an account number at all — only check
  numbers + amounts.

A `logger.info(...)` or audit `details` containing a full account / routing
number is a `Critical` PII-invariant violation.

**The file does not live forever.** It used to: nothing expired it and no erasure
request could reach it (issue #425). Two triggers now delete it — see
[File expiry and erasure](#file-expiry-and-erasure) below.

## Data model

One tenant-scoped table in `app/models/positive_pay.py`
(`PositivePayFile(Base, EntityMixin, TimestampMixin)`), migration
`0048_positive_pay` (+ `0070_positive_pay_currency` adds `currency`). Money is
exact: `total_amount` is `Numeric(18, 2)` — never float.

### `PositivePayFile` — `positive_pay_files`

| Field | Type | Notes |
|-------|------|-------|
| `id` | uuid PK | |
| `organization_id` | uuid | Indexed. |
| `payment_run_id` | uuid FK → payment_runs (nullable) | Set for `check_issue` (the run the cheques came from); NULL for `ach_authorization` (org-wide). Not separately indexed — `uq_positive_pay_run_format` leads on it; see § Indexes. |
| `file_type` | varchar(20) | `check_issue` \| `ach_authorization`. Indexed. |
| `bank_format` | varchar(30) | Formatter key, e.g. `csv` \| `fixed_width`. |
| `status` | varchar(20) | `generated` → `returned_processed` (once the bank's return is processed). Indexed. |
| `item_count` | integer | Number of cheques / authorized accounts in the file. |
| `total_amount` | numeric(18,2) | Sum of cheque amounts (always 0 for an ACH-authorization file). |
| `currency` | varchar(3) (nullable) | Currency `total_amount` is denominated in (migration `0070`). For a check-issue file, the one currency the included cheques' invoices agree on — `services/payment_runs.one_currency`, the rule the run endpoints use — because `Payment.amount` is denominated in its **invoice's** currency, not the org's (the home-currency debit is `source_amount`). A **stored label, not an FX conversion**. Until 2026-09 this stamped the org's reporting currency, so a USD cheque run for a EUR-reporting org came back labelled EUR. An ACH-authorization file (total always 0) stamps the reporting currency. NULL for legacy rows created before the column, and for a legacy run whose cheques span two currencies: the UI renders the total **bare** then, never under the org default (`docs/decisions.md` §196). |
| `content_hash` | varchar(64) | sha256 hex of the rendered file content — tamper-evidence / dedupe aid. |
| `file_key` | varchar(512) | MinIO key of the rendered file (the file holding the full account numbers). |
| `account_last4` | varchar(4) | Masked originating / cheque account — **never** the full number. |
| `generated_by` | uuid | The actor who generated the file (plain UUID, no cross-DB FK). |
| `meta` | jsonb (MutableDict) | Free-form bag — holds the `issued_map` (`{normalized_check_number: {"invoice_id": ..., "amount": "..."}}`, the POINT-IN-TIME snapshot of what was actually sent to the bank at generation — return processing classifies against this, never a live re-query of the run's current payment statuses, so an issued-then-voided cheque still classifies correctly instead of `not_on_file`) and, after a return, `return_summary` + `return_history`. **No PII.** |
| `entity_id` | uuid FK → entities | From `EntityMixin` (multi-entity). |
| `created_at` / `updated_at` | timestamptz | From `TimestampMixin`. |

The file-type / status string constants live on the model (`FILE_TYPE_*`,
`STATUS_*`).

### The run must have executed (422)

`POST .../check-issue` refuses a run whose `executed_at` is NULL. A draft run
has issued nothing: its payments are still `pending` with a NULL `reference`, so
`build_check_issue_items` yields no items and the file's `meta["issued_map"]`
persists **empty**. Combined with the idempotency slot below — claimed for good
on first generation — that empty snapshot could never be regenerated once the
run did execute, and `POST .../process-return` would classify every genuinely
issued cheque `not_on_file`, raising a `fraud_flag` Exception against each real
payment. The guard runs *before* the idempotency lookup, so an unexecuted run
can neither mint such a file nor be handed one back.

A file generated against a draft run **before** this guard existed is unusable
for the same reason (empty `issued_map`, permanently-claimed slot): delete it
with `DELETE /api/positive-pay/{id}` and regenerate once the run has executed.

### Idempotency

The partial unique index
`uq_positive_pay_run_format ON (payment_run_id, bank_format) WHERE
payment_run_id IS NOT NULL` enforces **one check-issue file per (run, format)**.
Re-generating returns the existing row (HTTP 200) rather than emitting a second
file; a concurrent double-fire loses the race on `flush()` → the loser rolls
back and returns the winner. ACH-authorization files are run-less
(`payment_run_id IS NULL`), so the partial predicate exempts them — you can
generate as many as you like.

**The index IS the concurrency control — the handler has no other one.** The
endpoint is a read-then-insert: it SELECTs an existing file for
`(run, bank_format)`, returns it if found, and otherwise renders, uploads and
inserts. Two concurrent calls (a double-clicked button, a client retry after a
timeout) both find nothing, so the only thing that stops both inserting is this
index turning the loser into the `IntegrityError` the handler catches. Without
it the run ends up with **two** Positive Pay files — two MinIO objects carrying
full account/routing numbers — and the handler's own `scalar_one_or_none()`
idempotency lookup then raises `MultipleResultsFound` on every later call: a
permanent 500 on that run, recoverable only by deleting a row.

That mattered in practice, because until migration 0093 the index existed
**only in migration 0048 and not on the model** — so every tenant provisioned by
`create_all` (i.e. every NEW tenant) ran this endpoint with no backstop at all,
while migrated tenants had one. `PositivePayFile.__table_args__` now declares it
and `tests/test_migration_model_index_parity.py` is the guard on the whole
class. If 0093 refuses to apply with a "duplicate Positive Pay check-issue
file(s)" message, that tenant already accumulated the duplicates this index
prevents: decide which file was actually sent to the bank, `DELETE
/api/positive-pay/{id}` the others (which also removes the stored object), and
re-run.

### Indexes

`positive_pay_files` carries one index per column the API actually filters on
(`organization_id`, `entity_id`, `file_type`, `status`) plus
`uq_positive_pay_run_format`. It deliberately carries **no** separate
`(payment_run_id)` index:

- A B-tree's leading column is independently searchable, so
  `uq_positive_pay_run_format (payment_run_id, bank_format) WHERE payment_run_id
  IS NOT NULL` serves every `payment_run_id = …` read a narrow index could —
  including the referential-integrity probe the FK to `payment_runs` issues when
  a parent run is deleted, which only ever looks for a non-NULL id and so
  satisfies the partial predicate by construction.
- Both real call sites (the check-issue idempotency lookup and its
  post-`IntegrityError` re-read) qualify on `bank_format` too, so the composite
  serves them **better**: both columns land in the `Index Cond` instead of the
  second becoming a `Filter`.
- The one read the partial composite cannot serve is `WHERE payment_run_id IS
  NULL` — its predicate excludes those rows, so that would be a sequential scan.
  No such query exists: the run-less `ach_authorization` files are reached
  through `file_type` (indexed), and the list endpoint's only other use of the
  column is `cast(payment_run_id AS text) ILIKE '%term%'`, which no B-tree on the
  raw column serves either. **Adding a "list the run-less files" query is the
  trigger to revisit this.**

Migration 0048 built `ix_positive_pay_files_payment_run_id` (and the model
declared it via `index=True`), so it was pure write overhead on every insert,
`process-return` status update and delete. **0094 drops it, and the model drops
`index=True` in the same commit** — a migration-only drop would reach only
migrated tenants while every `create_all`-provisioned tenant kept re-creating
it (`docs/decisions.md` §104 / §109). The parity guard's `EXEMPT` entry records
why 0048's `CREATE` now has no model declaration.

## Formatter adapters (`services/positive_pay_adapters/`)

The rendering layer mirrors `payment_adapters/`: a pluggable, per-bank
formatter, selected by the `bank_format` key, with a default for a **missing**
key and a refusal for an unrecognised one.

```python
@register_positive_pay_formatter("my_bank")
class MyBankFormatter(PositivePayFormatter):
    format_name = "my_bank"
    file_extension = "csv"
    content_type = "text/csv"

    def format_check_issue(self, items: list[CheckIssueItem], ctx: FormatterContext) -> str: ...
    def format_ach_authorization(self, items: list[AchAuthorizationItem], ctx: FormatterContext) -> str: ...
```

The dataclasses (`base.py`):

- `CheckIssueItem(check_number, payee, amount, issue_date, account_number)`
- `AchAuthorizationItem(vendor_name, routing_number, account_number, status)`
- `FormatterContext(company_name, account_number, file_date, currency)`

`get_positive_pay_formatter(name_or_config)` (`dispatcher.py`) resolves the
formatter. A **missing** name resolves to `csv` — the local-first default every
install ships. A **named** layout we have no formatter for raises
`UnknownPositivePayFormatError`, which both generate routes turn into a **422**
naming the bad value and the registered alternatives.

It used to fall back to `csv` for a named-unknown layout too, on the reasoning
that a bad config shouldn't break file generation. That trade was wrong here for
the same reason `decisions.md` §29 gives for the payment / ERP / FX dispatchers
and §36 for sanctions: the fallback is not inert, so the misconfiguration became
a confident wrong answer. Positive Pay is a **fraud control the bank enforces off
the file we hand it**, so one typo in `bank_format` (`"wells_fargo_fixed"` for a
registered layout) rendered a CSV body, stored it in MinIO, stamped the
`PositivePayFile` row *and* the audit trail with the **requested** name, filed
the `(payment_run_id, bank_format)` idempotency slot under it, and returned 201.
The bank then cannot parse the file — so either every cheque in the run is
refused, or Positive Pay is simply not in force and an altered cheque clears with
nothing to match it against. Nothing anywhere said so.

The refusal lands **before** the items are projected, so no cheque or vendor bank
data is rendered and no object is uploaded for a file that could never have been
formatted correctly. It lands **after** the idempotency lookup, so a row
generated under a since-removed format name is still returned rather than 422'd.
Guard: `tests/test_positive_pay.py` (dispatcher) + `tests/test_positive_pay_api.py`
(both routes, and that the idempotency slot stays free).

Registered formatters:

| Key | Class | Shape |
|---|---|---|
| `csv` (default) | `CsvFormatter` | RFC-4180 CSV. check_issue header `check_number,payee,amount,issue_date,account_number`; ach header `vendor_name,routing_number,account_number,status`. Amounts are plain `str(Decimal)`; dates ISO. |
| `fixed_width` | `FixedWidthFormatter` | Headerless, column-aligned, deterministic. Fixed widths documented as a contract in the module docstring (check_issue 89-char record: check#=10, payee=40, amount=14 zero-padded cents, issue_date=8 `YYYYMMDD`, account=17; ach 80-char record: vendor=40, routing=9, account=17, status=14). **Identifiers and money never truncate** — an over-long check / account / routing number or an amount too large for its column raises `PositivePayFieldOverflow`, surfaced as a 422 naming the column (never the value: these are full account numbers). Descriptive text (payee, vendor name, status) still truncates, which is the intended layout behaviour. See § Field overflow. |

### Adding a per-bank format

1. Copy `csv_formatter.py`, implement `format_check_issue` /
   `format_ach_authorization` for your bank's exact layout.
2. Decorate the class with `@register_positive_pay_formatter("your_bank")`.
3. Import it in `positive_pay_adapters/__init__.py` so the decorator runs at
   import time.
4. The frontend exposes formats via the `BANK_FORMATS` constant in
   `frontend/src/lib/types/positivePay.ts` — add your key there to surface it in
   the generate modal, plus its **label key** in `BANK_FORMAT_LABEL_KEYS` (the
   record is total over `BankFormat`, so a format with no label is a compile
   error) and that key's string in all six locale catalogues under
   `frontend/src/lib/i18n/locales/`. Until then the list's Format column, the
   detail meta pill and the picker fall back to the raw key via
   `bankFormatLabelKey()` — visible and searchable, never a blank cell.

Amounts are always derived from the `Decimal` on the item (zero-padded cents in
fixed-width, plain decimal string in CSV) — never a float.

### Field overflow — why identifiers fail loudly

A Positive Pay file is a fraud control: the bank refuses any item that does not
match what we told it we issued. That makes a *silently wrong* record worse
than no record at all — a truncated check number matches nothing, so the bank
rejects a cheque we genuinely wrote, and a truncated account number points the
row at an account that does not exist.

The fixed-width renderer therefore splits its fields in two:

| Kind | Fields | Overrun behaviour |
|---|---|---|
| Identifier / money | `check_number`, `account_number`, `routing_number`, `amount` | `PositivePayFieldOverflow` → **422**, no file written |
| Descriptive text | `payee`, `vendor_name`, `status` | Truncated to the column (intended) |

The amount case is the sharpest: the previous renderer padded then sliced
(`str(cents).rjust(w, "0")[:w]`), which keeps the **high-order** digits and
drops the low-order ones. An overrunning figure was not merely imprecise — it
was divided by ten per dropped digit, so `123456` cents rendered into a 4-char
column read as `1234`, i.e. $12.34 instead of $1,234.56, and the bank would
clear a cheque for the wrong amount.

The drawee `account_number` column was also widened from 8 to 17. It carries a
FULL account number from `settings.payments.check_account_number`, and a US
account alone runs 8-12 digits, so an 8-char column could not hold one: every
rendered check-issue file silently carried a truncated account. 17 matches the
`ach_authorization` account column in the same layout, which holds the same
kind of value. The check-issue record is consequently 89 chars, not 80.

Two details of the amount column that the tests pin:

- The width check runs on the **quantized** cents, not the raw `Decimal`. A
  figure that only overruns once rounded to 2 places is refused rather than
  sliced, so rounding can't be the thing that smuggles a rescale back in.
- The column is **unsigned** — `_amount_cents` renders `abs(cents)`. A negative
  amount therefore renders its magnitude, and overflows on its magnitude. The
  layout has nowhere to put a sign, so a credit belongs in a return file, not a
  check-issue row.

The error names the column and its width only — never the offending value,
which is a full account or routing number and would otherwise reach an HTTP
body (see `PositivePayFieldOverflow`).

Positive Pay files are **deliberately excluded** from the platform-wide CSV
formula-injection guard (`report_export.csv_safe_cell`, CWE-1236): they are
fixed-format uploads consumed by the bank's machine matching, never opened as a
spreadsheet workflow, and a `'` prefix on the payee/vendor name would break the
bank's exact-match comparison and raise a false fraud flag on every cheque. All
human-facing CSV exports (analytics, audit, report builder, invoice/workflow
exports) do apply the guard — see `backend/CLAUDE.md` § Security utilities.

## The pure return classifier

`classify_presented_items` (`services/positive_pay.py`) is **pure**: no DB, no
I/O, no network, no account numbers. It takes the items the bank reports as
*presented* and the items we *issued*, and labels each presented item:

| Classification | Meaning | Fraud? |
|---|---|---|
| `matched_ok` | Found by normalised check number, `|presented − issued| <= tolerance` | No |
| `amount_mismatch` | Found by check number, amount differs beyond tolerance — an **ALTERED** cheque | **Yes** |
| `not_on_file` | No issued cheque with that number — a cheque we **never wrote** | **Yes** |

Check numbers are normalised before matching (`normalize_check_number`: strip,
upper-case, drop every non-alphanumeric char), so `"1001"`, `"#1001"`, and
`"chk-1001"` collapse to a comparable form — mirroring the
`vendor_statement_recon` invoice normalisation. Default tolerance is one cent
(`Decimal("0.01")`). The classifier returns per-item `ReturnItemResult`s + roll-up
counts (`presented_count`, `matched_ok`, `amount_mismatch`, `not_on_file`).

## File-item builders (DB → formatter dataclasses)

Two async builders in `services/positive_pay.py` project tenant rows into the
formatter dataclasses. Both are entity-scoped and never log an account / routing
number.

- **`build_check_issue_items(db, *, run, entity_id, account_number="")`** —
  selects the run's cheque payments (`method == "check"`, excluding `failed` /
  `cancelled` / `voided` **at the time it's called**), joins `Invoice` for the
  payee name, and projects each into a `CheckIssueItem`. `check_number` is the
  Payment's `reference`; `issue_date` is the run's `executed_at` date, or —
  when the run hasn't executed — **today in UTC** (`app.utils.dates.utc_today`,
  not the server's local date). `executed_at` is a tz-aware UTC column, so
  `.date()` on it is already a UTC date; a local-time fallback would put the
  two branches a day apart on a non-UTC host, inside one file the bank matches
  cheques against. The `FormatterContext.file_date` both generators stamp
  resolves the same way. Returns `(items, total_amount, mapping)` where
  `mapping` is
  `[(normalized_check#, invoice_id, amount)]`. Called **only at file
  generation**; the result is persisted onto `meta["issued_map"]` so return
  processing never calls this again (see § Return handling — a second live call
  at return time would reflect a payment's CURRENT status, not what was
  actually on the file already sent to the bank).
- **`build_ach_authorization_items(db, *, org_id, entity_id)`** — selects
  `active` vendors whose `bank_details` carry both a routing and an account
  number, and projects each into an `AchAuthorizationItem`. Vendors without ACH
  bank details are skipped (nothing to authorize).

## API surface

Mounted at `/api/positive-pay` (`app/api/positive_pay.py`). Money is `Decimal`
end-to-end; every mutation is RBAC-gated, writes an audit row, and is
entity-scoped.

| Method + path | Purpose |
|---|---|
| `POST /positive-pay/payment-runs/{run_id}/check-issue` | Generate the check-issue file for a payment run (`bank_format?` body, default `csv`). **422 unless the run has executed** (see below). **Idempotent** on (run, format): returns the existing file 200, else renders + stores + persists 201. |
| `POST /positive-pay/ach-authorization` | Generate a standalone ACH debit-authorization file for the org (`bank_format?`). 201. |
| `GET /positive-pay` | List files (filters: `file_type`, `status`, `search`; paginated `page` / `page_size`), entity-scoped. `search` matches the bank format, the file type, and both ids as text — the row's label renders an 8-character id prefix, so pasting what is on screen finds the row. The rendered label itself is deliberately NOT searchable server-side: it is localised, so matching it in SQL would make the result set depend on the caller's browser language. The page used to filter the LOADED rows in the browser, so a file on page 2 read as "nothing matched" while the footer's "Showing all N" (the server's whole-set total) sat above a client-narrowed table. |
| `GET /positive-pay/summary` | Whole-set KPI rollup — `by_status` counts + `items_exported` (SUM of `item_count`) + `returns_flagged` (SUM of each file's `meta.return_summary.amount_mismatches + .not_on_file`). Shares `_positive_pay_list_filters` (`file_type` / `status` / `search`) with the list so the page's `itemsExported` / `returnsFlagged` KPIs can't describe only the loaded page. Declared **before** `/{file_id}` so the literal path wins. |
| `GET /positive-pay/{file_id}` | Detail. |
| `GET /positive-pay/{file_id}/download` | Stream the rendered file from MinIO. Verifies `file_key`'s first segment equals the caller's org (cross-tenant gate — **same 404** for wrong-org and missing, so it can't enumerate). |
| `POST /positive-pay/{file_id}/process-return` | Process the bank's return against a check-issue file (`presented_items: [{check_number?, amount?}]`). Classifies, raises deduped fraud Exceptions, flips status to `returned_processed`, stores the summary in `meta`. 422 if the file isn't a check-issue file. |
| `DELETE /positive-pay/{file_id}` | Delete the file row. 204. |

The originating company name + cheque account number are read from the
control-plane `Organization.settings` (`settings.company.name`;
`settings.payments.check_account_number` → `payments.account_number`). When no
account is configured the file still renders (account-less) and `account_last4`
is `None`.

### RBAC

Positive pay is a **treasury control** — clerks are excluded, matching the
Payments surface.

- **read** (list / detail / download) = `admin`, `ap_manager`, `cfo`
- **write** (generate / process-return / delete) = `admin`, `ap_manager`

Every read/write is **entity-scoped** (`X-Entity-ID` narrows to a subsidiary via
`apply_entity_scope` / `get_entity_id` / `get_write_entity_id`).

### Audit actions

Every mutation writes an `audit_log` row via `dispatch_audit` (append-only at
the DB layer). `entity_type` is `positive_pay_file`; `details` are **PII-free**
(file type, bank format, item count, total as string, run id — never an account
number):

| Action | Emitted by |
|--------|-----------|
| `positive_pay.check_issue_generated` | `POST .../check-issue` |
| `positive_pay.ach_authorization_generated` | `POST /ach-authorization` |
| `positive_pay.return_processed` | `POST .../process-return` (`details` = the return summary counts) |
| `positive_pay.deleted` | `DELETE /{id}` |

## Return handling — both fraud signals become `fraud_flag` Exceptions

`POST .../process-return` classifies each presented item against the
POINT-IN-TIME `meta["issued_map"]` snapshot persisted on the file at
generation — what was actually sent to the bank — **never** a live re-query of
the run's current payment statuses (issue #178: a cheque issued, then later
voided in the app, must still classify against what the bank was told, or its
presentment is mislabeled `not_on_file` and orphaned from its invoice — the
exact altered/fraud case Positive Pay exists to catch). It raises a deduped
`fraud_flag`
Exception for **every** fraud signal (`exception_type="fraud_flag"`,
`severity="error"`, `status="open"`, description `"Positive Pay return: check
<num> <reason>"` — no account numbers):

- **`amount_mismatch`** (an *altered* cheque whose number we did issue) maps to
  its invoice → an invoice-scoped `fraud_flag`.
- **`not_on_file`** (a cheque the bank cleared that we *never issued* — the
  strongest fraud signal) has, by definition, no invoice on our side. It becomes
  a **standalone `fraud_flag` with `invoice_id = None`**, so it's a first-class,
  queryable item in the exception queue rather than a buried JSON field.

**The dedupe key is the description, not the invoice.** Both branches skip only
when an open/escalated `fraud_flag` with the **same description** already exists
(narrowed by `invoice_id` when there is one) — and the description carries this
cheque's number and its verdict, so a bank redelivery of the same return still
creates nothing.

It used to be `(invoice_id, "fraud_flag")` on the invoice-scoped branch,
mirroring `invoice_warnings._ensure_exception`. That is the right key for a
*re-derived per-invoice rule* — re-running extraction must not re-raise the
round-amount flag — but the wrong key for a bank return, because the invoice may
already carry an open `fraud_flag` raised by something else entirely: the
round-amount rule (`severity="info"`), a settlement-amount divergence, or an
a cheque from an earlier run on the same invoice (re-paid after a void). That
unrelated row silently absorbed the bank telling us a cheque had been
**altered**: `process-return` returned `amount_mismatches: 1,
exceptions_created: 0`, and the queue showed only the pre-existing — possibly
`info`-severity — description. Narrowing the key can only ever raise *more*
rows, never suppress one. Guard:
`test_altered_cheque_raises_even_when_the_invoice_already_has_a_fraud_flag` in
`tests/test_positive_pay_api.py`.

**Why this needs a nullable `invoice_id`.** Migration `0049` drops the
`exceptions.invoice_id` NOT NULL constraint precisely so a never-issued cheque
can be a real Exception without fabricating a placeholder invoice (which would
pollute the AP ledger + every aggregate). One consequence: an invoice-less
exception can't be auto-resolved by an agent (there's no invoice to act on), so
`POST /api/exceptions/{id}/agent-resolve` returns **422** for it — human triage
only. The exceptions list already `outerjoin`s the invoice, so these rows render
with a null `invoice_id` / `invoice_number`. (Vendor-statement reconciliation
still models its `missing_on_our_side` rows as recon **lines**, not Exceptions —
that's a deliberate choice for its resolve/ignore lifecycle, no longer forced by
the constraint.)

The file then flips to `status = "returned_processed"` with a PII-free summary in
`meta["return_summary"] = {presented_count, matched_ok, amount_mismatches,
not_on_file, exceptions_created}`, and each run is appended to
`meta["return_history"]` so a re-processed bank redelivery never clobbers the
prior outcome.

## storage helper

`storage.upload_positive_pay_file(org_id, file_id, content: bytes, filename,
content_type) -> (file_key, download_url)` (`services/storage.py`) stores the
already-rendered bytes (not an `UploadFile`) at
`{org_id}/positive-pay/{file_id}/{_safe_filename(filename)}` and returns the key
+ the `/api/positive-pay/{file_id}/download` path. The filename is sanitised via
the shared `_safe_filename` (strips path separators / `..` / dotfiles); the body
is capped at `MAX_FILE_SIZE` defensively even though the content is
system-generated. The download route re-checks the key's org segment before
streaming.

## File expiry and erasure

The rendered file is the one artefact in this system carrying every payee's full
account and routing number in the clear, so two paths delete it. Both delete the
**object** and keep the **row**: `file_key` is nulled, a PII-free marker is
stamped into `meta`, and `item_count` / `total_amount` / `content_hash` /
`account_last4` — the audit-grade evidence the row exists to carry — stay.

| Trigger | Path | Marker written |
|---|---|---|
| A timer | The `positive_pay` retention class (`services/retention_sweep`), default **1 month** | `meta.file_expired_at`, `meta.file_expired_reason = "retention_policy"` |
| A request | A `vendor_contact` erasure whose invoices were paid by this run (`services/privacy_documents`) | `meta.file_erased_at`, `meta.file_erased_reason = "data_subject_erasure"` |

The default window is one month rather than the platform-wide 84 because the
bank consumes the file within days; it is per-org configurable as
`settings.retention.positive_pay_months` (PUT `/api/retention-policy`). The
retention sweep is off by default (`FEOH_RETENTION_ENABLED`) like every other
sweep here, so enabling it is a deployment step.

**Why the erasure path deletes a file that looks like transaction evidence:** it
is an *instruction* to a bank, not a record of what happened. The money trail is
`payments` plus this row; nothing evidential is lost. It is multi-subject (one
file covers a whole run), so an erasure removes other payees' coordinates too —
a reduction in their exposure, not a loss. Full reasoning:
`backend/docs/privacy.md` § Stored documents and `docs/decisions.md` §183–§184.

**Why not an S3 lifecycle rule**, which is the obvious instrument: every key
starts with the owning org's id (`{org_id}/positive-pay/...`) and an S3 prefix
filter is literal with no wildcard, so no single rule names these across
tenants; and the bucket carries a GOVERNANCE-mode Object Lock default retention
(`infra/s3.tf`) that defers any expiration until the lock elapses, so a rule
measured in weeks would never fire. `docs/decisions.md` §184.

## Frontend

Route `/positive-pay` (`frontend/src/routes/positive-pay/+page.svelte`), under
the **Billing** nav group (`$lib/nav.ts`, roles `admin` / `ap_manager` / `cfo` —
clerks excluded, matching the backend). Built from the shared `ui/` components:
`PageHeader` + a KPI row (`KpiCard`: total files, items exported, returns
flagged) + file-type `FilterChips` (All / Check issue / ACH auth) + `SearchBox`
+ a `DataTable` of files with clickable rows (`RowLink`) and an armed-confirm
delete (`RowAction`). Money renders via `<Money>` in the file's stored
`currency` — bare, with no symbol, for a NULL one (never the org default); URL-backed
filter state via `$page` + `replaceState` (deep-link `?id=` opens a file's
detail modal).

The modal (`$lib/components/modals/PositivePayModal.svelte`) is dual-mode:

- **Generate** — pick file type (check-issue → choose a payment run;
  ACH-authorization → org-wide) + bank format, gated to managers.
- **Detail** — file metadata, a download button (fetches the bytes with the
  Bearer token via `api.downloadBlob`), the return summary + unmatched-returns
  table, and a "process return" sub-form (paste `check#,amount` per line) for
  check-issue files.

Typed client `$lib/api/positivePay.ts` over the shared `api` object; types in
`$lib/types/positivePay.ts`.

## Migration

- **0048_positive_pay** — creates `positive_pay_files` + its indexes + the
  partial unique index `uq_positive_pay_run_format`. Tenant DB only (gated on the
  `invoices` table → no-ops on the control plane, fans out via
  `scripts/migrate_all_tenants.py`). Idempotent (`CREATE TABLE/INDEX IF NOT
  EXISTS`, the partial unique index via raw SQL `CREATE UNIQUE INDEX IF NOT
  EXISTS ... WHERE payment_run_id IS NOT NULL`). The FKs (payment_runs,
  entities) exist by earlier migrations.
- **0093_migration_only_indexes** — restates that `CREATE UNIQUE INDEX` so an
  already-provisioned tenant picks it up, and ships the matching
  `PositivePayFile.__table_args__` declaration that `create_all` needs. 0048
  claimed to mirror `app.models.positive_pay` exactly; for
  `uq_positive_pay_run_format` it did not, and only migrated tenants ever had
  the index. 0093 pre-flights for existing duplicate `(run, bank_format)` rows
  and refuses with an actionable, PII-free message rather than failing on the
  `CREATE UNIQUE INDEX` itself. See `docs/database.md` § Index parity between
  the two provisioning paths.
- **0094_drop_redundant_ppf_index** — drops
  `ix_positive_pay_files_payment_run_id`, a redundant prefix of
  `uq_positive_pay_run_format` (see § Indexes for why, and for the one read the
  composite does not cover). Ships with the matching removal of `index=True`
  from `PositivePayFile.payment_run_id`, because a migration-only drop reaches
  only migrated tenants. Tenant DB only (gated on the table existing), and
  reversible — `downgrade()` recreates 0048's index verbatim.

## Local-first

No new external dependency and no new `pnpm` script — generation, download, and
return processing all run in-process against MinIO. Generation is user-triggered;
the only background involvement is the shared retention sweep's `positive_pay`
class, which is off by default like every other sweep, so `pnpm dev` runs the
whole feature with no cloud credential and nothing to enable.

## Deferred / future work

- **More per-bank formats.** Only `csv` + `fixed_width` ship; a real BAI2 layout
  and named-bank templates (Wells Fargo ARP, BofA, Chase) slot in behind the
  same `@register_positive_pay_formatter` interface.
- **Automated return ingestion.** Today the bank's return is pasted / posted to
  `process-return`; a webhook or SFTP poller that ingests the bank's
  "items-presented-not-on-file" report and calls the same classifier is the
  natural next step.
- **Direct bank transmission.** The file is generated + downloaded; pushing it to
  the bank's cash-management portal (SFTP / API) is out of scope for this slice.
