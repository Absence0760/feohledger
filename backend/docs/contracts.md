# Contract Management — contract lifecycle (CLM)

Vendor contracts are the spine of contract lifecycle management. A `Contract`
row anchors five downstream capabilities: a searchable repository with a stored
document, spend-to-contract tracking, renewal alerts, compliance monitoring, and
contract-based PO creation. Only enterprise tools (Coupa, Basware) ship CLM
natively — most mid-market AP competitors don't.

## Lifecycle

A contract moves through five statuses:

```
draft → active → expired
   │       │
   │       └→ terminated
   └──────────→ cancelled
```

| Status | Meaning |
|--------|---------|
| `draft` | Created, not yet signed/in-force. Deletable. Can spawn a PO. |
| `active` | Signed and in force. Spend is tracked + compliance-checked; renewal sweep watches its `end_date`. |
| `expired` | Past `end_date`. Set by the renewal sweep's end-of-term expiry pass (see below) — never by `PATCH` or an admin flow. Can be re-activated (`activate`) or renewed (`renew`). |
| `terminated` | Ended early. Not deletable. |
| `cancelled` | Voided from `draft`/`active`. Deletable. |

Lifecycle transitions are owned by dedicated endpoints (not `PATCH`):

| Endpoint | From → To |
|----------|-----------|
| `POST /{id}/activate` | `draft` / `expired` → `active` |
| `POST /{id}/terminate` | `active` / `expired` → `terminated` |
| `POST /{id}/cancel` | `draft` / `active` → `cancelled` |
| `POST /{id}/renew` | any non-terminal → `active` (pushes `end_date` forward) |

A transition from a disallowed source status is refused with `409`.

## Data model

Tenant-scoped (`EntityMixin` + `TimestampMixin` + explicit `organization_id`).
A contract follows the `entity_id` of the vendor it's struck with (multi-entity
Phase 2), mirroring credit memos. Money is exact: every currency column is
`Numeric(15, 2)` — never float.

### `Contract` (`contracts`)

| Field | Type | Notes |
|-------|------|-------|
| `id` | uuid PK | |
| `contract_number` | varchar(100) | Required. |
| `title` / `description` | varchar(255) / text | |
| `contract_type` | enum | `purchase` (default), `service`, `subscription`, `lease`, `sla`, `msa`, `sow`, `other`. |
| `status` | enum | `draft` (default), `active`, `expired`, `terminated`, `cancelled`. |
| `vendor_id` | uuid FK → vendors | Required, indexed. |
| `currency` | varchar(3) | Default `USD`. |
| `total_value` | numeric(15,2) | Total committed value over the contract's life. |
| `spend_limit` | numeric(15,2) | Cumulative-spend ceiling for compliance. NULL = no ceiling. |
| `not_to_exceed` | boolean | When true, spend over `spend_limit` is a hard violation (`error` exception); else advisory (`warning`). |
| `start_date` / `end_date` / `signed_date` | date | `end_date` indexed (renewal sweep). |
| `auto_renew` | boolean | |
| `renewal_term_months` | integer | |
| `renewal_notice_days` | integer | Default 30. Per-contract lead window for the renewal alert. |
| `renewal_alert_sent_at` | timestamptz | Set once an alert fires for the current `end_date` (sweep idempotency); cleared on renew. |
| `payment_terms` | varchar(100) | |
| `owner_user_id` | uuid | Control-plane User; notified on renewal. |
| `file_url` / `file_key` | varchar | Stored contract document (S3/MinIO). |
| `terms` | jsonb | Structured compliance terms. Recognised keys: `allowed_gl_accounts`, `allowed_cost_centers`, `categories`. |
| `meta` | jsonb | Free-form bag. No PII / banking data. |
| `organization_id` | uuid | Indexed. |
| `entity_id` | uuid FK → entities | From `EntityMixin`; copied from the vendor. |

### `ContractLineItem` (`contract_line_items`)

`id`, `contract_id` (FK, indexed), `line_number`, `item_code`, `description`,
`quantity` (numeric(12,4)), `unit_price` / `total` (numeric(15,2)),
`gl_account`. Cascade-deleted with the contract.

The spend-to-contract link lives on the invoice side: `invoices.contract_id`
(nullable FK → contracts, indexed) — added by migration `0037`.

## Capabilities

### 1. Repository + document upload

`Contract` rows store the contract metadata, line items, and a single stored
document. `POST /{id}/upload` pushes the file to S3/MinIO via
`storage.upload_contract_file` (key form `<org_id>/contracts/<contract_id>/<filename>`)
and stamps `file_key` / `file_url`. `GET /contracts/file/{file_key}` proxies the
stored document back, cross-tenant-checked: the key's first segment must equal
the caller's `organization_id`, with the **same 404** for wrong-org and
missing-file so the response can't enumerate prefixes (mirrors the invoice file
endpoint).

List (`GET /contracts`) supports `status`, `contract_type`, `vendor_id`, and
`search` (over `contract_number` + `title`) filters with pagination.

### 2. Spend-to-contract tracking

`services/contract_spend.compute_spend_summary` aggregates the invoices linked
via `Invoice.contract_id` into a `ContractSpendSummary`: invoiced total, invoice
count, the `spend_limit`, `remaining`, and an `over_limit` flag. The sum runs in
the DB over the `Numeric` `amount` column and is handled as `Decimal`; only the
API response coerces to float (matching every other money field on the wire).
**Rejected invoices are excluded** — a rejected bill never became real spend.
The summary is attached to `GET /contracts/{id}` and to the lifecycle/update
responses.

Linking is done from the invoice:

```
POST /api/invoices/{id}/link-contract   body: { contract_id }
POST /api/invoices/{id}/unlink-contract
```

Linking is allowed in **any** invoice status (spend attribution on a paid
invoice is exactly when you want it). Each writes an `invoice.contract_linked` /
`invoice.contract_unlinked` audit row and re-runs
`invoice_warnings.refresh_warnings` so the contract-compliance flags recompute
for the new link. Role-gated `admin` / `ap_manager` / `cfo`.

### 3. Renewal alerts (background sweep)

`services/contract_renewal` is a long-lived asyncio loop started in
`main.lifespan` (mirrors the `audit_log_shipper` pattern: fresh per-tenant
engine, one tenant's failure logged but never halts the sweep). Each tick:

1. Enumerate tenant DBs from the control plane.
2. Per tenant, find `active` contracts with an `end_date`, **no** alert sent
   yet (`renewal_alert_sent_at IS NULL`), that fall within their own
   `renewal_notice_days` lead window (already-expired-but-unalerted contracts
   qualify too).
3. Notify the contract owner + every AP manager **once** via
   `notification_dispatch.notify_event` (in-app + email, preference-gated) with
   the `contract_renewal_due` event (`entity_type="contract"`).
4. Stamp `renewal_alert_sent_at = now()` so the alert never re-fires for this
   term. `POST /{id}/renew` clears it, re-arming the alert for the new
   `end_date`. (When there's no one to notify yet — no AP manager, no owner —
   the row is deliberately left **unstamped** so a later sweep, once the org
   has a recipient, still fires the alert instead of silently dropping it;
   re-scanning a recipient-less contract every tick is cheap.)
5. Separately (same tick), find `active` contracts whose `end_date` has
   actually **passed** (not just approaching) and transition them to
   `expired`, writing a `contract.expired` audit row
   (`entity_type="contract"`). This is the only runtime path that ever sets
   `ContractStatus.expired` — without it an over-term contract stays `active`
   forever and the `expired → …` branches of `activate`/`terminate` can never
   fire. Idempotent: only `active` contracts match, and expiring one moves it
   out of `active`, so a repeat sweep never double-expires or double-audits.

Disabled by default. Env vars:

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_CONTRACT_RENEWAL_ENABLED` | `false` | Master switch for the renewal sweep. Off in local dev/tests; flip on in deployed envs. |
| `FEOH_CONTRACT_RENEWAL_INTERVAL_SECONDS` | `3600` | Sweep interval. |
| `FEOH_CONTRACT_RENEWAL_DEFAULT_NOTICE_DAYS` | `30` | Platform default lead window; per-contract `renewal_notice_days` overrides it. Read by `contract_renewal.resolve_notice_days`, which is the single fallback both the SQL candidate query (`COALESCE`) and the under-lock Python re-check go through. |
| `FEOH_CONTRACT_RENEWAL_BATCH_SIZE` | `200` | **Page** size for the keyset pagination in *both* passes — not a per-tick cap. Each pass carries its own cursor and pages (`WHERE id > :cursor ORDER BY id`) until the tenant is exhausted, locking one contract at a time. Capping is not available here: a contract outside its lead window stays un-alerted and one not yet over term stays `active`, so a `LIMIT` would re-serve the same lowest-id contracts every tick and never reach the tail. See `background-sweeps.md` § Locking. |

`notify_renewals_once(today=…)` is callable directly (CLI / tests) for a single
sweep.

**The lead window in step 2 is a SQL predicate, not a Python filter.** It used to
pre-filter coarsely on `end_date <= today + 3650 days` — effectively every active
contract with an end date — and drop the out-of-window rows after loading them.
It is now `end_date - today <= COALESCE(renewal_notice_days, <platform default>)`
(`contract_renewal.lead_window_predicate`), evaluated in Postgres; the Python
check that remains is the *under-lock re-check* the two-phase sweep shape
requires, and it reads the same `resolve_notice_days` fallback, so the two cannot
drift by a day. `tests/test_sweep_candidate_pagination.py` pins the agreement on
every boundary (one day before the window, exactly on it, one day after) plus the
NULL notice-days fallback, evaluating the real expression against real Postgres
rather than re-implementing it.

### 4. Compliance monitoring

`services/contract_compliance.evaluate_contract_compliance` is a pure-ish
evaluator: given an invoice with a `contract_id`, it loads the contract and
returns a list of finding dicts (`{type, severity, message}`). Exception
creation + persistence is the caller's job
(`invoice_warnings.refresh_warnings`), so the evaluator stays unit-testable in
isolation. A dangling link (contract deleted) yields no findings. Findings
checked:

| Check | Severity |
|-------|----------|
| Invoice dated after the contract's `end_date` (expired) | `warning` |
| Invoice dated before the contract's `start_date` | `warning` |
| Spend recorded against a `terminated` / `cancelled` contract | `warning` |
| Invoice vendor differs from the contract vendor | `warning` |
| Cumulative linked spend (excl. rejected, exact `Decimal`) over `spend_limit` | `error` if `not_to_exceed`, else `warning` |
| Invoice `gl_account` outside `terms.allowed_gl_accounts` | `warning` |

All findings carry the exception type **`contract_noncompliant`** and surface as
an `Exception` row via the normal warnings/exception pipeline.

### 5. Contract-based PO creation

`POST /{id}/create-po` spins a `PurchaseOrder` out of a contract, auto-populated
from its terms: vendor, line items, and total are copied from the contract.
`po_number` defaults to `PO-<contract_number>-<short>`; `total` defaults to the
sum of the contract's line-item totals, falling back to `total_value`. Only
`draft` / `active` contracts can spawn a PO (`409` otherwise). The PO inherits
the contract's `entity_id` and its `currency` — the line items and
`total_value` the PO's figures come from are denominated in it, and the response
carries the code (`docs/decisions.md` §197). Writes a `contract.po_created` audit
row.

## API surface

Mounted at `/api/contracts`.

| Method + path | Purpose | RBAC |
|---------------|---------|------|
| `GET /contracts` | List (filters: `status`, `contract_type`, `vendor_id`, `search`; paginated; `sort=contract_number\|title\|status\|start_date\|end_date\|total_value&order=asc\|desc`, `.id` tie-break) | read |
| `GET /contracts/ids` | Every contract id matching the caller's list filters, capped at `MAX_SELECT_ALL_IDS` — the "select all N matching" resolver | read |
| `POST /contracts` | Create (always lands in `draft`) | mutate |
| `GET /contracts/{id}` | Detail + spend summary | read |
| `PATCH /contracts/{id}` | Update (status excluded — lifecycle endpoints own it) | mutate |
| `DELETE /contracts/{id}` | Delete — `draft` / `cancelled` only (`409` otherwise) | mutate |
| `POST /contracts/{id}/upload` | Upload contract document → S3 | mutate |
| `GET /contracts/file/{file_key}` | Proxy stored document (cross-tenant-checked) | any authenticated |
| `POST /contracts/{id}/activate` | Lifecycle → `active` | mutate |
| `POST /contracts/{id}/terminate` | Lifecycle → `terminated` | mutate |
| `POST /contracts/{id}/cancel` | Lifecycle → `cancelled` | mutate |
| `POST /contracts/{id}/renew` | Extend `end_date`, re-activate, re-arm alert | mutate |
| `POST /contracts/{id}/create-po` | Spawn a PO from the contract | mutate |
| `POST /contracts/bulk/status` | Bulk activate/terminate/cancel over a hand-picked set (`{ids, action}`), routed through the same `_transition` helper the single-row endpoints use | mutate |
| `POST /contracts/bulk/export` | CSV export of a hand-picked set (`{ids}`) | read |

Plus the invoice-side link endpoints (`POST /api/invoices/{id}/link-contract`
and `/unlink-contract`, `admin` / `ap_manager` / `cfo`).

### RBAC

- **read** = `admin`, `ap_manager`, `ap_clerk`, `cfo`
- **mutate** (create / update / delete / upload / lifecycle / create-po) =
  `admin`, `ap_manager`
- The file-proxy route uses `get_current_user` plus the org-prefix cross-tenant
  check (any authenticated user, scoped to their own org's keys).

### Bulk operations (issue #328)

`/contracts` was, along with `/vendors`, one of the two primary list pages
shipping zero bulk actions. `POST /bulk/status` resolves each id
independently and calls the SAME `_transition(action, contract_id, …)`
helper `POST /{id}/activate|terminate|cancel` use — so a bulk call can't
reach a status (or skip an audit row) the single-row path wouldn't allow.
A contract not in a legal starting status for the action, or an id that
doesn't resolve, is skipped-and-reported (`{updated, skipped: [{id,
reason}]}`) rather than failing the whole batch — the same partial-success
contract as `api/vendors.py::bulk_vendor_status` /
`api/invoices.py::bulk_status_change`. `renew` is deliberately NOT a bulk
action — it needs a per-contract `end_date`, which has no sane bulk default.
See `backend/tests/test_contract_bulk_ops.py` and
`backend/tests/test_list_sorting.py`.

## Audit actions

Every mutation writes an `audit_log` row via `dispatch_audit` (append-only at
the DB layer). `entity_type` is `contract` (or `invoice` for the link
endpoints):

| Action | Emitted by |
|--------|-----------|
| `contract.created` | `POST /contracts` |
| `contract.updated` | `PATCH /contracts/{id}` (only when fields changed; `details.fields` lists them) |
| `contract.deleted` | `DELETE /contracts/{id}` |
| `contract.document_uploaded` | `POST /{id}/upload` |
| `contract.active` / `contract.terminated` / `contract.cancelled` | lifecycle endpoints |
| `contract.renewed` | `POST /{id}/renew` |
| `contract.po_created` | `POST /{id}/create-po` (`details` carries the new `po_id` / `po_number`) |
| `invoice.contract_linked` / `invoice.contract_unlinked` | invoice link/unlink |

Audit `details` never carry PII / banking data.

## Notifications

The renewal sweep emits the **`contract_renewal_due`** event
(`EVENT_CONTRACT_RENEWAL_DUE`, `entity_type="contract"`), rendered by
`notification_templates.render_contract_renewal` and dispatched in-app + email
(preference-gated) to the contract owner + AP managers. See
[notifications.md](notifications.md).

## Migrations

- **0036_contracts** — creates `contracts` + `contract_line_items`. Tenant DB
  only (gated on the `invoices` table → no-ops on the control plane, fans out to
  every tenant via `migrate_all_tenants.py`). Idempotent (`CREATE TABLE/INDEX IF
  NOT EXISTS`). Mirrors the ORM model (incl. the `entity_id` column) so a fresh
  tenant built by `tenant_provisioning._create_tenant_tables` (`create_all`)
  matches a migrated one.
- **0037_invoice_contract_link** — adds the nullable `invoices.contract_id` FK
  (+ index). Tenant DB only (gated on the `contracts` table from 0036).
  Idempotent (`ADD COLUMN IF NOT EXISTS`).

## Local-first

No new external dependency and no new `pnpm` script. The renewal sweep is
disabled by default (`FEOH_CONTRACT_RENEWAL_ENABLED=false`), so `pnpm dev` runs
the whole contract feature — repository, spend tracking, compliance, PO
creation — with no background loop and no cloud credential. Flip the switch on
in deployed envs to enable renewal alerts.

## Tests

- `frontend/tests-e2e/contracts/contracts.spec.ts` — UI list render, API-driven
  create surfaced in the table, an activate reflected on the row.
- `frontend/tests-e2e/contracts/contracts-lifecycle.spec.ts` — the control
  paths, API-driven: each lifecycle transition (activate / terminate / cancel /
  renew) enforces its valid source state (`409` / `400` on an invalid one) and
  writes **exactly one** append-only audit row (a rejected transition writes
  none); RBAC (clerk + cfo can read but get `403` on every mutation, create-po,
  upload, and PATCH); `create-po` copies the contract's money exactly
  (`Numeric`, no float drift), links the PO back to the contract, is audited,
  and is refused (`409`) from a terminated contract; spend roll-up sums linked
  invoices in `Decimal` (`100.10 + 100.20 = 200.30`), excludes rejected, and
  computes `remaining` / `over_limit` correctly; and the file-proxy refuses a
  cross-org key with the same `404` it returns for a missing file (no
  enumeration).
