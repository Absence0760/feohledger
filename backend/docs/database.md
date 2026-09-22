# Database

PostgreSQL 16 running in Docker, accessed via async SQLAlchemy 2 with asyncpg.

## Multi-Database Architecture

The app uses a **database-per-tenant** isolation model:

| Database            | Purpose                    | Contains                                                                                    |
|---------------------|----------------------------|---------------------------------------------------------------------------------------------|
| `feohledger`  | Control plane              | organizations, users, roles, user_roles, email_verifications, plans, subscriptions, api_keys, assistant_usage |
| `feoh_acme`           | Acme Corp tenant           | invoices, vendors, payments, workflows, vendor_extraction_priors, invoice_embeddings, extraction_usage, card_rebates, ... |
| `feoh_techflow`       | TechFlow Inc tenant        | invoices, vendors, payments, workflows, vendor_extraction_priors, invoice_embeddings, ...   |

All databases run on the same PostgreSQL instance. Tenant database URLs are derived from the control-plane URL by swapping the database name.

**The two billing meters are TENANT tables**, despite being a per-org concern:
`extraction_usage` and `card_rebates` are absent from
`tenant_provisioning.CONTROL_TABLES`, no Alembic revision creates them in the
control plane, and `services/billing`'s `rollup_usage` reads them per tenant.
This table said otherwise for a long time, and `services/extraction.py` was
written against the claim — the resulting INSERT against a table the control
plane does not have raised into the extraction handler and marked successful
invoices `failed`. `assistant_usage`, by contrast, genuinely *is* control-plane.

## Connection

Default control-plane connection string (configured via `FEOH_DATABASE_URL`):

```
postgresql+asyncpg://postgres:postgres@localhost:5432/feohledger
```

Tenant connections are derived automatically:

```
postgresql+asyncpg://postgres:postgres@localhost:5432/feoh_acme
```

## Data Models

### Numeric column types — the guard is opt-out

Project invariant #1 is **money is exact**: currency is `Numeric(precision,
scale)`, never `Float` / `Double` / `REAL`, because a binary float cannot
represent `0.10` and every `SUM` across the column comes back slightly wrong.
Two rules follow, and both are enforced across the *whole* schema by
`backend/tests/test_money_invariants.py` — not against a list of columns
someone remembered to add:

1. **Every `Numeric` column declares precision AND scale.** A bare
   `Numeric()` is arbitrary-precision on one backend and double-precision
   float on another, so the same model rounds differently depending on where
   it is deployed. There is no allowlist — nothing wants an unspecified one.
2. **No `Float` / `Double` / `DOUBLE_PRECISION` / `REAL` column exists at
   all**, unless it is named in `NON_MONEY_FLOAT_ALLOWLIST` in that test file
   with a written reason.

The consequence for a new column is that protection is **opt-out**: add
`mapped_column(Numeric(15, 2))` and you are already covered — you do not have
to register it anywhere. Adding a float is what costs you something.

This replaced a name-token sweep that matched `amount` / `total` / `price` /
`subtotal` / `_tax` and asserted only "not Float". It had drifted off the
schema — 35 of the 88 `Numeric` columns matched no token (every FX rate at
`NUMERIC(18, 8)`, every expense-policy threshold, `Contract.spend_limit`,
`CashPlan.opening_balance`) — so a `Column(Float)` named `balance`, `budget`,
`limit`, `fee`, `cost` or `*_rate` passed the entire file.

**Adding a justified non-money float.** Rare enough that the allowlist is
currently empty. If you genuinely need one:

```python
# backend/tests/test_money_invariants.py
NON_MONEY_FLOAT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("some_table", "some_column"): "model confidence score, never multiplied by money",
}
```

The key is `(table_name, column_name)` and the value is the reason. The bar is
high, and *"the test went red"* is not it — if the column plausibly holds
currency, a quantity that gets multiplied by a price, a rate applied to money,
or a threshold money is compared against, it **is** money: fix the column type
(with an Alembic migration that fans out to every tenant), don't list it.
Legitimate entries look like a model confidence score, a ranking weight, a
latency measurement, a geo coordinate. `test_float_allowlist_has_no_stale_entries`
prunes the list for you — an entry whose column is no longer a float fails,
because a stale key silently pre-approves a *future* column of that name.

Note that several existing columns are `Numeric` **without** being money —
`agent_decisions.confidence` `NUMERIC(5, 4)`, `vendors.risk_score`
`NUMERIC(5, 2)`, `invoice_line_items.quantity` `NUMERIC(12, 4)`. They stay
`Numeric` deliberately: a confidence is compared against a configured
threshold and a quantity is multiplied by a unit price, so both want exact
decimal comparison rather than binary drift.

### Control-Plane Tables

- `organizations` — tenant registry (name, slug, db_name, settings, plan)
- `users` — all users across all tenants. Columns: `email`, `full_name`, `hashed_password` (nullable for SSO-only), `organization_id`, `is_active`, `must_change_password`, `sso_provider` + `sso_provider_id` (OIDC linkage), `mfa_secret` + `mfa_enabled` + `mfa_enrolled_at` (TOTP MFA)
- `roles` — role definitions (admin, ap_manager, ap_clerk, cfo)
- `user_roles` — many-to-many join table
- `email_verifications` — pending self-service signups (token, email, slug, admin_name, expires_at, consumed_at). Created by `POST /api/signup/start`, consumed by `POST /api/signup/complete`.
- `extraction_usage` — billing rows: invoice_id, provider, program_type, period (used for tracking platform-extraction usage)
- `card_rebates` — billing rows: virtual_card_id, amount, rate, status, period

### Tenant Tables

#### Invoice Pipeline
- `invoices` — master record (invoice_number, vendor_name, amount, currency, due_date, status, file_url, vendor_address, vendor_tax_id, ship_to_address, tax_rate, payment_method, reference_number, assigned_to_id, assigned_to, approved_by, rejected_by, **warnings** JSONB, **po_match** JSONB, **meta** JSONB)
  - `meta` is a free-form per-invoice metadata bag. Currently holds the cached audit-log summary under `meta["audit_summary"]` = `{text, confidence_context, source_fingerprint: {count, last_at}, generated_at, model}` — regenerated lazily when the audit-log fingerprint changes. No PII / banking data. See [`audit-summary.md`](audit-summary.md).
- `invoice_line_items` — individual lines (description, qty, unit_price, tax, total)
- `invoice_extraction_results` — AI extraction output per attempt (method, confidence, raw JSON, **priors_metadata** — summary of vendor cache overrides + RAG neighbors applied during extraction)
- `invoice_embeddings` — pgvector `vector(1536)` RAG store. One row per approved invoice; corrected_fields snapshot + embedding. Queried via cosine distance for few-shot retrieval. See [`ai-extraction.md`](ai-extraction.md).

#### Extraction priors
- `vendor_extraction_priors` — per-vendor correction cache. Unique on `(vendor_id, field_name)`. Populated when reviewers correct fields during approval; applied to low-confidence extractions for the same vendor.

#### Procurement (for 3-way matching)
- `purchase_orders` — PO header (vendor, total, **currency**, status). `currency` is nullable with no default (migration 0099): the code the PO's source knew, NULL when none said — see `docs/decisions.md` §197
- `po_line_items` — PO lines
- `goods_receipts` — GR header
- `gr_line_items` — GR lines

#### Workflow Engine
- `workflow_definitions` — configurable per org (steps, rules, conditions as JSON)
- `workflow_instances` — one per invoice (current step, state machine)
- `workflow_steps` — individual step records (assigned_to, action, timestamps)
- `audit_log` — immutable event log (actor, action, entity, timestamp)

#### Payments
- `payment_runs` — batch payment execution records
- `payment_schedules` — due dates, early-pay discount windows
- `payments` — individual payment records (amount, method, status, ref)

#### Exceptions
- `exceptions` — flagged issues (duplicate, mismatch, anomaly, resolution status)

Tenant tables have an `organization_id` column (plain UUID, no foreign key) for backward compatibility.

### pgvector

`invoice_embeddings.embedding` uses the `vector` data type from the [pgvector](https://github.com/pgvector/pgvector) Postgres extension. Both the local `docker-compose.yml` (the `pgvector/pgvector` image on its `-pg16` line, pinned by digest — see [docker.md § Image pinning](docker.md#image-pinning)) and `services.tenant_provisioning._create_tenant_tables` / `scripts.seed.create_tenant_tables` run `CREATE EXTENSION IF NOT EXISTS vector` before creating tenant tables so the column type resolves.

## Migrations (Alembic)

### Control-plane DB

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
```

### Single tenant DB

```bash
FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head
```

### All tenant DBs

```bash
python scripts/migrate_all_tenants.py
```

### Generate a new migration

```bash
alembic revision --autogenerate -m "description of change"
```

### Downgrade

```bash
alembic downgrade -1
```

### Existing migrations

Migrations may target either the control plane or tenant DBs — never both. Each migration in `alembic/versions/` gates on presence of a table that only exists in its target shape (e.g., `users` for control, `vendors` for tenant) and no-ops on the wrong DB.

| Revision | Scope   | Gate table   | What it does                                                                                                             |
|----------|---------|--------------|--------------------------------------------------------------------------------------------------------------------------|
| 0001     | Control | `users`      | Adds `users.must_change_password`; creates `email_verifications`.                                                        |
| 0002     | Tenant  | `vendors`    | Creates `vendor_extraction_priors` (per-vendor correction cache).                                                        |
| 0003     | Tenant  | `invoices`   | Creates pgvector extension + `invoice_embeddings` + HNSW cosine index; adds `invoice_extraction_results.priors_metadata`. |
| 0004     | Control | `users`      | Adds `users.sso_provider` + `users.sso_provider_id` + partial index for OIDC user lookup.                                |
| 0005     | Control | `users`      | Adds `users.mfa_secret`, `users.mfa_enabled`, `users.mfa_enrolled_at` (TOTP MFA).                                        |
| 0006     | Tenant  | `invoices`   | Adds `invoices.po_match` JSONB column (latest 2/3-way PO match result).                                                  |
| 0007     | Tenant  | `payments`   | Adds `payments.provider`, `provider_payment_id`, `failure_reason`, `submitted_at`, `completed_at` (adapter lifecycle).   |
| 0022     | Tenant  | `vendors`    | Creates `vendor_change_requests` (supplier-portal staged bank/tax changes pending AP approval) + a partial index on `status='pending'`. |

> Migration 0022 may need renumbering or a merge revision at integration time — sibling features built on parallel branches may also claim `0022` (all branch off `0021_scim_bearer_hash`).

The `vendor_change_requests` table (tenant DB): `id`, `vendor_id` (FK → `vendors`, ON DELETE CASCADE), `organization_id`, `requested_by_vendor_user_id`, `change_type` (`bank_details` | `tax_id`), `status` (`pending` | `approved` | `rejected`), `proposed_value` (JSONB — banking PII, never logged), `reviewed_by_user_id`, `reviewed_at`, `review_note`, `created_at`, `updated_at`. Bank/tax changes from the supplier portal stage a row here instead of mutating the vendor; AP approval applies them. See `docs/supplier-portal.md`.

### Index coverage on `invoices`

Two of `invoices`' index-bearing columns exist for the procurement budget
rollup rather than for any list endpoint: a budget attributes realised spend by
an equality against one of `cost_center` / `gl_account` / `department` /
`project` (`services/budget_service._DIMENSION_MATCH_COLUMN`), so an unindexed
one seq-scans the whole invoice table on the `GET /budgets/check` path.

`department` / `project` were indexed by migration `0044`; `cost_center` /
`gl_account` predate it and were not, until
`0090_invoice_budget_dimension_indexes`. All four now carry
`ix_invoices_<column>` — SQLAlchemy's default single-column name, which is what
`create_all` produces, so a tenant provisioned by
`tenant_provisioning._create_tenant_tables` and one brought up by Alembic end
up with identical schemas. The measurement that justified 0090 (and the case
it deliberately does *not* improve) is in
[`procurement-budgets.md`](procurement-budgets.md) § Index coverage on the four
dimensions.

Both revisions gate on the `invoices` table existing, so they no-op on the
control plane and fan out to every tenant DB via
`scripts/migrate_all_tenants.py`.

### Index coverage on list + audit reads

Two query shapes ran against unindexed columns until
`0092_list_and_audit_indexes`, and both hit tables that only grow.

**`audit_log` — the fastest-growing table in the schema.** Nothing was indexed
on `created_at` or `action`, so five callers seq-scanned the whole table:

| Caller | Predicate | Now served by |
|---|---|---|
| `GET /api/audit/verify-signatures` (SOX signature sweep) | `action = 'invoice.approved' AND created_at` range | `ix_audit_log_action_created_at` |
| `GET /api/audit/export` (auditor date-range export) | `created_at` range `ORDER BY created_at` | `ix_audit_log_created_at` |
| `api/dashboard.py` approval-timing leg (every dashboard load) | `entity_type='invoice' AND action='invoice.approved'` | `ix_audit_log_action_created_at` |
| `api/adaptive_workflows.py` feedback reads | `action IN (…) AND created_at >= :since` | `ix_audit_log_action_created_at` |
| `services/retention_sweep.py` overdue count | `created_at < :cutoff`, and `… AND shipped_at IS NULL` | `ix_audit_log_created_at` / `ix_audit_log_shipped_at_null` |

A sixth — `services/audit_log_shipper._ship_tenant`, `shipped_at IS NULL ORDER BY
created_at ASC LIMIT n`, **per tenant every 60 s, returning nothing on a healthy
platform** — was a full scan only on tenants provisioned by `create_all`.
Migration `0010_audit_log_shipping` built `ix_audit_log_shipped_at_null` for it,
but nothing declared it on `AuditLog`, and fresh tenants never run Alembic. So a
migrated tenant had the index and a freshly-provisioned one did not, for as long
as audit shipping has existed. 0092 fixes that by declaring it on the model and
restating the (idempotent) `CREATE`; it deliberately does **not** drop it on
downgrade, and must never rename it — a second identical partial index would be
pure write overhead on every audit row forever.

That partial predicate is what keeps the index to the tail the shipper still has
work in: **8 KB** on a caught-up 1.2 M-row trail, against 50 MB for
`(action, created_at)` and 26 MB for `(created_at)`. Measured on a
`create_all`-provisioned tenant, the caught-up tick goes from 39.7 ms / 30 003
buffers to 0.040 ms / 1.

**List endpoints ordered by an unindexed column.** Page 1 of every list view was
a whole-table read plus a top-N heapsort — a cost that grows with the table
while the page size stays at 20. Seven tables get a plain ordering index and a
status-leading composite:

| Table | Ordering index | Status composite |
|---|---|---|
| `invoices` | `ix_invoices_created_at_id` | `ix_invoices_status_created_at_id` |
| `payments` | `ix_payments_created_at_id` | `ix_payments_status_created_at_id` |
| `exceptions` | `ix_exceptions_created_at_id` | `ix_exceptions_status_created_at_id` |
| `expenses` | `ix_expenses_created_at_id` | `ix_expenses_status_created_at_id` |
| `purchase_orders` | `ix_purchase_orders_created_at_id` | `ix_purchase_orders_status_created_at_id` |
| `contracts` | `ix_contracts_created_at_id` | `ix_contracts_status_created_at_id` |
| `corporate_card_transactions` | `ix_corp_card_txn_date_id` | `ix_corp_card_recon_status_txn_date_id` |

`corporate_card_transactions` is the odd one out **by design**: its list orders
by `txn_date DESC` (the date the charge happened, not the date we imported it)
and filters on `reconciliation_status`, so an index on `created_at` there would
never be read.

The composite is not redundant with the ordering index. For a *rare, scattered*
status Postgres otherwise walks the ordering index discarding non-matching rows
until it has 20 — 18 928 discarded for one page at 200 k invoices — and that
discard count grows with the table. Measured at 200 k invoices: list page 1
15.8 ms / 4 952 buffers → 0.031 ms / 4; a 0.1 %-selective status chip 2.1 ms /
591 buffers → 0.036 ms / 4.

Measured and deliberately **excluded**: `GET /api/invoices/counts` (the status
chips) and the list's own `SELECT count(*)`, both rollups over 100 % of the
filtered population where a seq scan is the right plan (the same conclusion 0090
reached for the budget rollup); and the `search=` path, whose `ILIKE '%term%'`
leading wildcard no btree index can serve — only `pg_trgm` would, at its own
write cost, which is a separate decision.

**No `CREATE INDEX CONCURRENTLY`**, deliberately: it cannot run inside Alembic's
transaction, and combined with `IF NOT EXISTS` it is a trap — a failed or
cancelled concurrent build leaves an *INVALID* index whose name then makes every
later run skip it and report success. The whole revision builds in ~2.5 s across
all eight tables at the volumes above (longest single index 742 ms on 1.2 M audit
rows), and that is the `ACCESS EXCLUSIVE` window. An operator whose `audit_log`
is large enough for that to matter can build the indexes by hand with
`CONCURRENTLY`, verify `pg_index.indisvalid` on each, and then run the migration
— every statement is `IF NOT EXISTS`, so it becomes a no-op.

Every index is declared **twice on purpose**: in the migration (existing tenants)
and in the owning model's `__table_args__` (fresh tenants, which are built by
`create_all` in `tenant_provisioning`, not by Alembic). Skipping the second half
is what left `ix_audit_log_shipped_at_null` missing from provisioned tenants for
82 revisions, so `tests/test_list_and_audit_indexes.py` is the guard — it fails
if the two spellings ever disagree on a name, a column order, or a partial
predicate, and it asserts each index actually serves its caller's query. That was
one instance of a class of 20; see **Index parity between the two provisioning
paths** below for the rest and for the systemic guard that now covers every
revision.

### Index parity between the two provisioning paths

A database here is built **two ways**, and only one of them runs Alembic:

| Path | Built by | Applies to |
|------|----------|-----------|
| Alembic | `alembic upgrade head` / `scripts/migrate_all_tenants.py` | every EXISTING tenant + the control plane |
| `create_all` | `Base.metadata.create_all` in `services/tenant_provisioning._create_tenant_tables` | every NEW tenant, the control plane on a fresh install, and the pytest `realdb` harness |

`create_all` builds **exactly what the ORM declares**. So an index written into a
migration and never declared on its model reaches migrated databases and
*silently never reaches a freshly-provisioned one*. Nothing notices: the reads
still return correct rows, just by sequential scan — and where the index is
UNIQUE, the invariant it enforces is simply absent on half the fleet.

**Every index must therefore be declared twice on purpose**: in the migration
(existing databases) and in the owning model's `__table_args__` (new ones).

Migration 0092 fixed the first instance of this found
(`ix_audit_log_shipped_at_null`, migration 0010 — the audit shipper's own
60-second sweep running as a full scan on every provisioned tenant). Migration
**0093** closed the rest of the class: an audit of all 216 `CREATE INDEX`
statements across every revision found **20** that no model declared, verified
absent on a real `create_all`-built tenant (`feoh_pytesta`, which has no
`alembic_version` row at all) and on a `create_all`-built control plane.

- **18 were genuinely missing** and are now declared on their models —
  `users.ix_users_sso_lookup` (the SSO callback's identity lookup),
  `organizations.ix_organizations_scim_bearer_hash`,
  `subscriptions.uq_subscription_one_live_per_org`,
  `invoice_embeddings.ix_invoice_embeddings_embedding_hnsw` (pgvector HNSW,
  expressed with `postgresql_using="hnsw"` + `postgresql_ops`),
  `exceptions.ix_exceptions_due_at`, `payments.ix_payments_corridor`, both
  `sanctions_checks` indexes, both `bank_transactions` read indexes,
  `scheduled_reports.ix_scheduled_reports_due`,
  `vendor_change_requests.ix_vendor_change_requests_pending`, both
  `quality_inspections` FK indexes, all three `vendors` screening indexes, and
  `positive_pay_files.uq_positive_pay_run_format`. 0093 restates each `CREATE
  INDEX IF NOT EXISTS` so an already-provisioned database catches up; it
  **ensures, it does not own** them, so its `downgrade()` drops none of them
  (the original revision still owns each DROP) — the `_ADOPTED` semantics 0092
  established.
- **2 were not missing at all** and are reconciled rather than duplicated onto a
  model, because declaring them would build a second, identical index:
  `ix_bank_transactions_matched_payment` (0019) is the exact column and
  predicate of the UNIQUE `uq_bank_transactions_matched_payment` that 0081 added
  and the model declares, so 0093 **drops** it; and
  `ix_vendor_change_requests_org_id` (0022) is the model's
  `ix_vendor_change_requests_organization_id` under another name, so 0093
  converges on the model's spelling (create, then drop the alias).

Two of the 18 are **UNIQUE, i.e. correctness rather than performance**:
`uq_positive_pay_run_format` is the only concurrency backstop under the
check-issue endpoint's read-then-insert (see `docs/positive-pay.md`
§ Idempotency), and `uq_subscription_one_live_per_org` is the "one live
subscription per org" billing invariant — `uq_subscription_org_plan`, the one
the model *did* declare, does not bound the live count, since two rows for two
different plans satisfy it. Because a database that ran without one may already
hold the rows it was meant to prevent, 0093 **pre-flights** each UNIQUE index:
it counts the offending groups and refuses with an actionable, PII-free message
(counts only) *before* any DDL, rather than failing half-way through with a bare
Postgres error. Cleaning up is a judgement call about real artefacts — which of
two Positive Pay files went to the bank, which subscription is billing — so it
is deliberately the operator's, not a silent `DELETE` inside a migration.

**The guard is `tests/test_migration_model_index_parity.py`** — opt-out, not
opt-in. It parses every `CREATE [UNIQUE] INDEX` out of every revision (via
`ast`, so DDL split across adjacent string literals is read correctly) and fails
if any of them, on a table still in `Base.metadata`, is not declared on the
model. A new migration-only index fails the suite the day it lands, with no list
to remember to update. An exemption needs a written reason in `EXEMPT`, and is
itself re-checked so it can't rot. Against a real Postgres it also drops what
`create_all` built, rebuilds it from 0093's own SQL, and compares
`pg_get_indexdef` — same access method, columns, sort direction, operator class
and partial predicate, not just the same name.

`tests/test_list_and_audit_indexes.py` remains the per-index guard for 0092's own
16 list/audit indexes (including that each actually serves its caller's query).

**The same rule runs in the other direction.** An index that is genuinely
redundant has to be dropped from the model AND the database in one commit, or
every freshly-provisioned tenant keeps re-creating what the migration just
removed — the mirror image of the class above. Migration **0094** is the worked
example: `ix_positive_pay_files_payment_run_id` is a `(payment_run_id)` B-tree
whose only column is the LEADING column of the partial UNIQUE
`uq_positive_pay_run_format (payment_run_id, bank_format)`, so the composite
serves every read it could — measured, not assumed (see
`docs/positive-pay.md` § Indexes for the plans, and for the one read a partial
index cannot serve). The drop ships with the removal of `index=True` from
`PositivePayFile.payment_run_id`, and the parity guard's `EXEMPT` entry records
why 0048's `CREATE` now has no model declaration — plus a structural assertion
that the composite still LEADS on that column, since the whole argument
collapses if it ever stops.

## Seeding

Seed the control plane and both demo tenants:

```bash
python scripts/seed.py
```

This creates databases, tables, and inserts demo data. Safe to re-run (idempotent). Seeds 6 users across 2 orgs with different roles (admin, AP manager, AP clerk, CFO).

To re-seed from scratch, drop the Docker volumes first: `docker compose down -v && docker compose up -d`

## Provisioning New Tenants

```bash
python scripts/create_tenant.py \
  --name "New Corp" --slug newcorp \
  --admin-email admin@newcorp.com --admin-password changeme
```

This creates the database, tables, org record, and admin user in one step.

## Model inventory (control plane vs tenant)

Which table lives in which database, and the notable columns on each.
Extracted from `backend/CLAUDE.md` to keep that file cheap to load.

`CONTROL_TABLES` in the models package is the enforced list — a coverage
gate fails if a model is added without being classified.

**Two-database pattern:**

1. **Control plane** (`feohledger`) — shared across all tenants
   - `Organization` — id, name, slug, db_name, settings (JSONB), plan
   - `User` — email, full_name, hashed_password, sso_provider/id, mfa_secret/enabled/enrolled_at, must_change_password, notification_prefs (JSONB — per-user email/in-app channel prefs, user-global), device_tokens (JSONB — one mobile push token per platform, registration only, no push-sending adapter yet; migration 0078), organization_id
   - `Role` — name (admin, ap_manager, ap_clerk, cfo)
   - `UserRole` — junction table
   - `WebAuthnCredential` — registered passkey (credential_id, public_key, sign_count, transports) per `user_id`; the WebAuthn second factor (migration 0063)
   - `AssistantUsage` — billing: per-org/month assistant token meter. The
     *only* usage meter that is control-plane; `ExtractionUsage` and
     `CardRebate` read like control-plane data but are tenant-local (see
     the tenant list below and `docs/decisions.md` §57)

2. **Tenant DBs** (`feoh_<slug>`) — isolated per customer
   - `Entity` — legal entity / subsidiary within the tenant (name, slug, currency, is_default, is_active). Business tables carry a nullable `entity_id` FK (`EntityMixin`); every tenant has one `is_default` Entity. Multi-entity Phase 2 (reads/writes scoped by the `X-Entity-ID` header) — see `../docs/multi-entity.md`
   - `Invoice` — invoice_number, vendor_name, amount, status (12 states), file_key, warnings (JSONB), po_match (JSONB), meta (JSONB — holds `audit_summary`)
   - `InvoiceLineItem` — invoice_id, item_code, description, quantity, unit_price, total, gl_account
   - `InvoiceExtractionResult` — invoice_id, method, confidence, raw_result (JSONB)
   - `Vendor` — name, code, tax_id, status (active/unverified/inactive/rejected), source (manual/erp_sync/ai_extracted)
   - `PurchaseOrder` — po_number, vendor_id, total, currency (nullable, no default — migration 0099), status
   - `POLineItem` — po_id, description, quantity, unit_price, total
   - `GoodsReceipt` — gr_number, po_id, received_date, status
   - `GRLineItem` — gr_id, description, quantity_received
   - `QualityInspection` — inspection_number, po_id, gr_id, result (pass/fail/partial), accepted_quantity, rejected_quantity, deviation_notes — the 4-way match leg (see `docs/po-matching.md`)
   - `GLAccount` — code, name, account_type, parent_code, erp_account_id
   - `PaymentRun` — status, total_amount, initiated_by, executed_at
   - `PaymentSchedule` — invoice_id, due_date, discount_date, discount_percent
   - `Payment` — invoice_id, payment_run_id, amount, method (`String(50)`, not a DB enum — `ach`/`wire`/`check`/`virtual_card`, the international rails `sepa`/`international_ach`/`international_wire`, and the UK domestic rails `bacs`/`faster_payments`/`chaps`; classified on both the 1099 and geography axes in `services/payment_methods.py`), status, `retry_of_payment_id` (self-FK, migration 0080 — `/runs/{id}/retry-failed` books a NEW attempt row pointing at the failed one it replaces and never mutates that row, because `correlation_id` is the PROCESSOR's idempotency key; run rollups count the latest attempt per invoice via `payment_runs.active_run_payments`. See `docs/payments.md` § Why a payment failed, and retrying it), `settled_amount` / `settled_currency` (migration 0083 — what the PROCESSOR says it moved, beside `amount` which is what AP AUTHORIZED. NULL is meaningful and is not zero: no rail ever reported a figure, which `payment_settlement.settlement_coverage` reads as "nothing indicates a shortfall" and fails OPEN, so an amount-free rail can't hold every invoice it settles. See `docs/payments.md` § Settlement-amount verification)
   - `VirtualCard` — invoice_id, card_provider (lithic/nium), provider_card_id, amount_limit, status
   - `CardRebate` — virtual_card_id, amount, rate, status (`pending`/`confirmed`/`paid_out`), period. Not in `CONTROL_TABLES` — fanned to every tenant DB like the rest, despite living in `app/models/virtual_card.py` alongside `VirtualCard`
   - `WorkflowDefinition` — name, steps_config (JSONB), is_active, is_default
   - `WorkflowInstance` — definition_id, invoice_id, current_step, state, steps_config_snapshot (JSONB)
   - `WorkflowStep` — instance_id, step_number, step_type, assigned_to, action, completed_at
   - `WorkflowExperiment` — A/B test of two workflow-rule configs (`config_a`/`config_b` JSONB) on one `workflow_definition_id`; `split_a_pct`, `primary_metric`, `min_sample_per_variant`, `status` (draft/running/concluded), `assignments` (JSONB `{invoice_id: "A"|"B"}`). Assigned at invoice creation (deterministic stable hash, freezes the variant config onto the instance snapshot); migration 0064. See `docs/adaptive-workflows.md` § A/B testing
   - `AuditLog` — actor_id, action, entity_type, entity_id, details (JSONB)
   - `Exception` — invoice_id, exception_type, severity, status (open/resolved/escalated/dismissed)
   - `CreditMemo` — vendor_id, original_invoice_id, amount, status (open/applied/voided)
   - `BankStatement` / `BankTransaction` — uploaded statement + parsed transactions for reconciliation
   - `SanctionsCheck` — append-only KYC / sanctions screening trail per vendor
   - `ScheduledReport` — recurring CFO report definition (cron, recipients, format)
   - `InvoiceEmbedding` — vector embedding per invoice for RAG / duplicate detection
   - `VendorExtractionPrior` — accumulated vendor field priors that bias the next extraction
   - `VendorUser` — supplier-portal credentials scoped to a single Vendor. `provisioned_by_user_id` (migration 0095) records the control-plane `User` who last minted a password for it (invite, or admin reset — the only two routes that hand an AP actor a working supplier credential). It is a segregation-of-duties record, not bookkeeping: see `VendorChangeRequest` below
   - `VendorChangeRequest` — staged supplier-portal change to a vendor's `bank_details` / `tax_id`, pending AP approval (migration 0022; fraud-prevention gate — see `docs/supplier-portal.md`). Carries TWO requester columns, and `approve_change_request` refuses when either equals the approver: `requested_by_user_id` (AP-initiated, migration 0066) and `requester_provisioned_by_user_id` (migration 0095) — the submitting portal identity's `provisioned_by_user_id`, **frozen at staging** so deleting that portal user afterwards can't erase the evidence. Without the second column every portal-submitted request short-circuited the check on a NULL, and one `ap_manager` could invite a portal user, sign in as it, stage a bank redirect, approve it and pay it
   - `CardRevealToken` — single-use token granting vendor access to a virtual-card PAN reveal page
   - `Notification` — in-app notification center rows (recipient_user_id, event_type, entity_id, title/body, read_at). See `docs/notifications.md`
   - `PeppolTransmission` — one row per PEPPOL transmission (direction=outbound|inbound). Outbound idempotency: partial unique index `uq_peppol_one_live_per_invoice_direction` on `(invoice_id, direction) WHERE status <> 'failed'`. Inbound dedupe: partial unique index `uq_peppol_message_id` on `message_id WHERE message_id IS NOT NULL`. See `docs/peppol.md`
   - `Contract` — vendor contract / CLM spine. contract_number, contract_type (purchase/service/subscription/lease/sla/msa/sow/other), status (draft/active/expired/terminated/cancelled), vendor_id, money (`Numeric` total_value / spend_limit + not_to_exceed), lifecycle dates, renewal config (auto_renew, renewal_notice_days, renewal_alert_sent_at), terms (JSONB), file_key. Spend link is `Invoice.contract_id`. See `docs/contracts.md`
   - `ContractLineItem` — contract_id, line_number, item_code, description, quantity, unit_price, total, gl_account

