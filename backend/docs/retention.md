# Retention policies (SOX records management)

Configurable per-record-class retention periods plus an audited enforcement
sweep that archives records past their window through a **privileged, audited
path** — never a raw `DELETE`. It composes with the audit-log immutability
trigger: `audit_log` rows are WORM and are never deleted.

## Configuration — not hardcoded

Retention windows live on `Organization.settings.retention`, keyed
`<record_class>_months`:

```json
{ "retention": { "invoices_months": 84, "audit_log_months": 84, "positive_pay_months": 1 } }
```

`resolve_retention_months(settings, record_class)` (in
`services/retention_sweep.py`) resolves the effective window: per-org override →
**per-class default** (`RECORD_CLASS_DEFAULT_MONTHS`) → platform default
`FEOH_RETENTION_DEFAULT_MONTHS` (84 = 7 years, the common SOX/IRS baseline). It
never raises — a malformed/missing value degrades to the default.

`positive_pay` is the one class with its own default (**1 month**), because the
platform baseline is the wrong answer for it by two orders of magnitude: the
artefact is a file of full account and routing numbers that a bank consumes
within days, so a seven-year retention is the opposite of what the class wants.
An org whose bank has a long dispute window can raise it like any other.

### API — `GET` / `PUT /api/retention-policy` (admin only)

`app/api/retention.py`:

- **GET** returns the effective policy per record class, the platform default,
  and whether the sweep is enabled.
- **PUT** updates one or more `<class>` windows (months, `> 0`). Unknown record
  classes or non-positive windows are rejected `422` before any write. Every
  mutation writes a `retention_policy.updated` audit row into the **tenant**
  trail (via the self-committing `dispatch_auth_audit`, since the settings
  themselves live on the control plane). PII-free — only class names + month
  windows.

Record classes the engine understands: `invoices`, `audit_log`, `positive_pay` (`RECORD_CLASSES`).

## Enforcement sweep — `services/retention_sweep.py`

A long-lived asyncio loop (mirrors `contract_renewal` / `qms_sync`) started in
`app/main.py` lifespan, **disabled by default** behind `FEOH_RETENTION_ENABLED`,
interval `FEOH_RETENTION_INTERVAL_SECONDS` (default daily). It sweeps every tenant
DB; one tenant's failure is logged but never halts the sweep.

### Business records (deletable) — soft-archive, idempotent

For each tenant, invoices in a **terminal** state (`done` / `paid`) whose
`created_at` is older than the `invoices_months` window get an `archived_at`
marker stamped into their `meta` JSONB bag. No row is destroyed; the marker is
the privileged, fully-reversible archival action — and **no schema change** is
needed. In-flight (non-terminal) invoices are never archived regardless of age.

**The candidate query is bounded on both axes**, and both bounds are
load-bearing:

- **Already-archived rows are excluded in SQL**
  (`invoices.meta->>'archived_at' IS NULL`), not skipped in Python. The
  idempotency check used to be a `continue` inside the loop, so every tick
  re-loaded the tenant's entire archive — a set that only ever grows. The
  Python check remains as a backstop; the SQL exclusion is what makes the sweep
  cost proportional to the work actually left.
- **The batch is capped** at `FEOH_RETENTION_BATCH_SIZE` (default 500) per
  tenant per tick, oldest first. Because archived rows leave the candidate set,
  a capped tick makes strict forward progress and the next tick resumes where it
  stopped — no starvation. Same shape as
  `FEOH_RECURRING_INVOICES_MAX_PER_SWEEP` and the audit shipper's batch size.

### Positive Pay files — expire the FILE, keep the row

The generated check-issue / ACH-authorization file legitimately carries every
payee's full account and routing number (that is its purpose) and lives in
object storage; the `positive_pay_files` row deliberately stores only
`account_last4`. Nothing expired the file until this class existed — issue #425.

Past the `positive_pay_months` window, for each row still holding a `file_key`
(same SQL-side idempotency exclusion and the same `FEOH_RETENTION_BATCH_SIZE`
cap as the invoice leg):

1. the object is deleted through `storage._delete_object` (offloaded to a worker
   thread — a bare boto3 call here would block the event loop the sweep shares
   with the API);
2. on success `file_key` is nulled and `meta.file_expired_at` /
   `meta.file_expired_reason = "retention_policy"` are stamped;
3. on failure nothing is nulled, so the key survives for the next tick and
   `positive_pay_files_failed` reports it in the manifest.

**The row itself is never deleted.** `item_count`, `total_amount`,
`content_hash` and `account_last4` are the audit-grade evidence that the file
existed and what was in it, and all of them are PII-free.

The same objects are reachable on *request* rather than on a timer: a
`vendor_contact` erasure deletes the Positive Pay files for any run that paid
that vendor's invoices, through the shared traversal in
`services/privacy_documents`. A retention rule that deletes on a schedule and an
erasure that deletes on demand are the same walk with different triggers.

**Why this is not an S3 lifecycle rule** — the cheaper instrument, and the one
the issue proposed first. Every key begins with the owning organisation's id
(`{org_id}/positive-pay/...`) and an S3 lifecycle prefix filter is a *literal*
prefix with no wildcard, so no single rule names these objects across tenants;
and the bucket carries a GOVERNANCE-mode Object Lock default retention
(`infra/s3.tf`), which defers any expiration until the lock elapses — a rule
measured in weeks would sit in the Terraform looking like a control and never
fire. `docs/decisions.md` §184.

### Audit records (WORM) — verify, never delete

**CRITICAL: the sweep never deletes `audit_log` rows.** Migration 0022 installs
a `BEFORE DELETE` trigger that rejects every delete (and every UPDATE except
`shipped_at`), so a deletion would be refused by Postgres anyway — but the sweep
must not even attempt it. For the `audit_log` class, "retention" means
*verifying* that rows past the window have been WORM-shipped (`shipped_at` set)
and recording a manifest:

- `audit_rows_overdue` — rows past the `audit_log_months` window.
- `audit_rows_overdue_unshipped` — of those, how many still have
  `shipped_at IS NULL` (the WORM sink is behind; an operator should investigate
  `audit_log_shipper`).

### Audited manifest

When a sweep archives anything **or observes overdue audit rows the WORM sink
has not taken**, it writes a `retention.archived` audit row (system actor,
PII-free `details`): the resolved window months per class, the **count** of
archived invoices, the batch size and whether the batch was capped
(`invoices_archive_batch_size` / `invoices_archive_batch_capped` — a capped tick
means more remain), the Positive Pay counts (`positive_pay_months` /
`positive_pay_files_expired` / `positive_pay_files_failed` — never a file key,
which embeds org and record ids), and the audit overdue/unshipped counts, plus a
note that audit rows are immutable and never deleted. An idle tenant writes no
manifest (no no-op spam).

**The gate is `archived or audit_rows_overdue_unshipped or positive_pay_files_expired
or positive_pay_files_failed` — deliberately not `audit_rows_overdue`.** The two
Positive Pay counters are safe to gate on for the same reason `archived` is: both
describe work actually done (or attempted) this tick and both return to zero once
the backlog drains, so neither can inflate itself. That counter is monotonic and self-inflating: it counts
every `audit_log` row past the window, and this sweep never deletes an audit row
(the trigger forbids it, and WORM evidence must not be destroyed anyway). So
once a tenant's oldest audit row crossed its window the old condition was
permanently true — a manifest reading `invoices_archived: 0` was appended on
every tick, forever, and each of those manifests is itself an `audit_log` row
that ages past the window and inflates the next tick's count. Unbounded growth
in an append-only, undeletable table.

`audit_rows_overdue_unshipped` is the actionable half of the same observation:
it is what an operator must act on (the sink is behind), it cannot inflate
itself (a manifest written now is far younger than the window, so it is never
counted), and it returns to zero once the shipper catches up — at which point
the manifest stops being written. `audit_rows_overdue` is still *reported* in
every manifest; it just no longer *causes* one. Guarded by
`backend/tests/test_retention_manifest_guard.py`.

**Counts, never the ids.** The manifest used to inline every archived invoice
id, so one JSONB row grew with the archive; past ~1 MB that single row
head-of-lines the audit shipper's 500-row batch (CloudWatch `PutLogEvents` caps
a batch at 1 MB), and nothing newer ships. The per-invoice evidence is the
`meta.archived_at` marker on the invoice row itself, which is durable,
queryable, and cannot grow without bound.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `FEOH_RETENTION_ENABLED` | `false` | Master switch for the enforcement sweep. Keep `false` in local dev; flip on in deployed envs. |
| `FEOH_RETENTION_INTERVAL_SECONDS` | `86400` | Sweep interval. |
| `FEOH_RETENTION_DEFAULT_MONTHS` | `84` | Platform-default window (months) when an org sets no per-class override. |
| `FEOH_RETENTION_BATCH_SIZE` | `500` | Max invoices soft-archived per tenant per tick, oldest first. A page, not a cap on total work: archived rows leave the candidate set, so the next tick resumes. |

## Why no migration

This first slice is **config + an audited archival sweep** that acts only on
already-deletable / soft state (the existing `Invoice.meta` JSONB marker) and
verifies WORM shipment for the immutable audit class — so it needs **no schema
change**. A future slice that wants a first-class `Invoice.archived_at` column
(for indexed exclusion of archived rows from list endpoints) would add it then.

## Tests

`backend/tests/test_retention_manifest_guard.py`: the manifest-write gate — a
tenant past its window with nothing archivable and nothing unshipped writes NO
manifest on any tick, while an unshipped overdue row (and real archival work)
still does.

`backend/tests/test_retention.py`: resolver (override → default, malformed →
default); per-tenant failure isolation; sweep archives only overdue terminal
invoices; idempotent (no double-archive); already-archived rows excluded in SQL
(a batch of 1 still reaches the *second* invoice on the next tick); the batch
cap; the manifest carries counts and **no** id list; **never deletes audit
rows** (composes with the immutability trigger); policy GET/PUT + audit row;
`422` on unknown class / non-positive window; RBAC (admin-only, 401/403).
