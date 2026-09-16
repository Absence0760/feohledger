# Centralized audit-log shipping

SOC 2 control. The `audit_log` table lives **inside each tenant's database**,
where a determined tenant-admin (or a compromised one) could theoretically
edit rows. To give auditors a tamper-evident, append-only copy of every
audit event, the backend runs a background shipper that pulls unshipped
rows out of every tenant DB and writes them to one or more WORM
(write-once-read-many) sinks: **CloudWatch Logs** and **S3 with Object Lock**.

- Code: `backend/app/services/audit_log_shipper.py`
- Adapters: `backend/app/services/audit_shipping/`
- SOC 2 mapping: `../../docs/soc2-readiness.md` → "Logging + monitoring"
- Migration: `alembic/versions/0010_audit_log_shipping.py`

## How it works

1. Every `FEOH_AUDIT_SHIPPING_INTERVAL_SECONDS` (default 60s), a background
   asyncio task wakes up.
2. The control plane's `Organization` table is queried for every tenant
   DB name.
3. For each tenant, the shipper opens a fresh async engine and runs:

   ```sql
   SELECT * FROM audit_log
   WHERE shipped_at IS NULL
   ORDER BY created_at ASC
   LIMIT :batch_size;   -- default 500
   ```

4. The batch is converted to `AuditLogRow` dataclasses and handed to every
   adapter listed in `FEOH_AUDIT_SHIPPING_PROVIDERS`.
5. **All** adapters must succeed before the tenant's rows get marked
   `shipped_at = now()`. If any one raises, the shipper logs a WARNING and
   runs the isolation pass below; whatever is still unshipped keeps
   `shipped_at` NULL and the next tick retries it. CloudWatch Logs and S3
   both have at-least-once semantics, so a replay may produce duplicate
   events downstream — that's documented and acceptable.
6. On shutdown the task is cancelled cleanly via `main.lifespan`.

### A poison row must not stop the tenant's trail

The batch is all-or-nothing and ordered `created_at ASC`, so ONE row a sink
refuses used to make `adapter.ship` raise on every tick, re-select the identical
oldest-first batch, and block every **newer** row for that tenant forever — the
WORM evidence trail simply ended there. `ShipResult.failures` climbed and
`GET /api/health/sweeps` went `degraded`, which is correct; the defect was that
the only remedy was an operator finding and hand-editing the offending row.

A failed batch is now followed by a **bounded isolation pass**:

- the rows are re-shipped one at a time, in order;
- a row an adapter refuses is re-offered to **that adapter** with its `details`
  replaced by a PII-free quarantine marker —
  `{"_details_quarantined": true, "reason": "sink_rejected_row", "error_class":
  …, "original_bytes": …}`. The row's identity (id, org, actor, action, entity,
  timestamp) is untouched, so the WORM copy stays an ordered, tamper-evident
  trail, and the complete row is still in the tenant `audit_log` table;
- the substitution is **per-adapter** — a row CloudWatch refuses may be fine for
  the S3 Object Lock copy, and the full-detail copy there is worth keeping;
- if an adapter refuses the marker version too, the sink is unhealthy rather
  than the row poisoned: the pass stops at that row, everything from it on stays
  unshipped, and the tick fails exactly as before. That is what bounds an outage
  to two extra calls per adapter instead of one per row, and what stops a
  transient outage from stripping the details off a whole batch;
- rows shipped before that stop **are** stamped, so the pass's progress is not
  re-shipped forever.

Quarantined rows are counted on `ShipResult.rows_quarantined` (surfaced in the
sweep-health payload) and logged one PII-free WARNING each — the row id and the
refusing exception's **class**, never the refused payload and never `str(exc)`.
They are deliberately **not** counted as sweep failures: the trail moved and
nothing was dropped, so the tick genuinely succeeded; the count plus the WARNING
is the operator signal, and a quarantined row is stamped shipped so it cannot
recur every tick.

Rows the isolation pass re-ships were already offered inside the failed batch,
so a sink may see them twice. Same at-least-once seam as everything else here: a
duplicate is identifiable by the row's own `id` and recoverable on read, a
missing row is not. This is the shipper-level sibling of the `cloudwatch`
adapter's `_details_truncated` marker (which handles the per-event 256 KiB cap
inside that one sink); the two keys are distinct so an operator can tell them
apart in the WORM store.

### Why one batch at a time per tenant

Simple + bounded. The largest tenant DB governs how quickly we drain
— if you notice rows aging past a few ticks of the interval, raise
`FEOH_AUDIT_SHIPPING_BATCH_SIZE`. A 500-row batch gzips to ~80KB of JSONL.

The batch size is **not** bounded by any sink's request limits, and must not
be read as if it were: `PutLogEvents` caps a call at 10 000 events *and* at
1 MiB, and audit `details` is free-form JSONB, so a handful of fat rows takes
a default 500-row batch past the byte cap. The `cloudwatch` adapter chunks the
batch to fit — see § `cloudwatch` below. Sizing the shipper's batch is a
throughput decision; fitting a sink's request is the adapter's job.

### Why a per-tick engine

The shipper doesn't own the cached tenant-engine pool (`database.get_tenant_engine`).
It spins one up per tenant per tick and disposes it in `finally`, matching
`extraction_reaper`. Cheaper than plumbing the cache through a background
task that doesn't own a request-scoped session, and it bounds connection
fan-out under a runaway sweep.

## Configuration

All env vars have the `FEOH_` prefix.

| Variable | Default | Purpose |
|---|---|---|
| `FEOH_AUDIT_SHIPPING_ENABLED` | `false` | Master switch. Default off so local dev doesn't fire AWS calls. |
| `FEOH_AUDIT_SHIPPING_INTERVAL_SECONDS` | `60` | How often the shipper sweeps. |
| `FEOH_AUDIT_SHIPPING_BATCH_SIZE` | `500` | Max rows pulled per tenant per tick. |
| `FEOH_AUDIT_SHIPPING_PROVIDERS` | `mock` | Comma-separated adapter names. Typical prod value: `cloudwatch,s3_objectlock`. |
| `FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP` | `/ap/audit` | CloudWatch Logs group name. |
| `FEOH_AUDIT_SHIPPING_S3_BUCKET` | (empty) | Object-Lock-enabled S3 bucket for the WORM copy. Required when `s3_objectlock` is enabled. |
| `FEOH_AUDIT_SHIPPING_S3_MIN_RETENTION_DAYS` | `2555` | Floor the bucket's default Object Lock retention must meet (≈ 7 years). The boot check refuses a shorter one. |

## Adapters

### `mock` — in-memory sink (default, local dev + tests)

Records every shipped row into a module-level list. Useful for tests
and as a "shipping enabled, but don't actually ship anywhere yet" stub.
Not durable — do not use in production.

### `cloudwatch` — AWS CloudWatch Logs

Writes each row as a single JSON log event. Events are partitioned by
log stream: one stream per `(tenant_db, UTC-date)`, e.g.
`feoh_acme/2026-04-21`. The log group is controlled by
`FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP`.

Why one stream per tenant per day? CloudWatch charges by ingested
bytes, not stream count, and pre-partitioning by tenant + day lets
auditors answer "show me every event for tenant X on day Y" with a
simple filter rather than a Log Insights scan.

The adapter lazily creates the log group + streams on first use; the
`ResourceAlreadyExistsException` is swallowed so repeated ticks don't
bang on `CreateLogStream` with errors.

#### The PutLogEvents caps are the adapter's problem, not the shipper's

`PutLogEvents` refuses a call carrying more than **10 000 events** or more than
**1 MiB** (the sum of the UTF-8 message bytes plus 26 bytes of framing per
event), and refuses a single event over **256 KiB** on the same accounting.
The adapter enforces all three:

- each stream's events are sorted by timestamp, then **chunked** to fit both
  per-call caps (`_chunk_events`, pure and unit-tested);
- a single row that could not fit a call even alone has its `details` replaced
  by a PII-free marker (`{"_details_truncated": true, "original_bytes": …,
  "limit_bytes": …}`) so the row's *identity* — id, org, correlation, actor,
  action, entity, timestamp — still lands in the tamper-evidence store. The
  complete row keeps living in the tenant `audit_log` table and in the
  `s3_objectlock` copy, which has no comparable limit. A count of truncated
  rows is logged (never their ids or content).

Why not just raise on an oversized batch? Because the shipper selects
`shipped_at IS NULL` **oldest first**: a batch that can never be accepted is
re-selected identically on every tick, so one fat row stops the tenant's whole
audit trail from ever shipping again — head-of-line blocking, with the sweep
reporting a failure nobody can act on. That is not hypothetical: a
`retention.archived` row that inlined every archived invoice id grew past 1 MB
and jammed exactly this path (see the note in `services/retention_sweep.py`).
Trimming that row fixed the symptom; the cap is now respected at the source.

#### `ship()` is all-or-nothing in the bookkeeping, at-least-once at the sink

Chunking makes that seam wider, not new. A batch already spanned one
`PutLogEvents` call per `(tenant, day)` stream, and
`FEOH_AUDIT_SHIPPING_PROVIDERS` already fanned out to several adapters — none
of which composes into a transaction, so a later call failing cannot un-write an
earlier one. What the shipper guarantees is its own bookkeeping: `shipped_at`
is stamped only when every adapter's `ship()` returned cleanly, so a raise
replays the **whole** batch on the next tick and the part the sink already
accepted arrives twice.

That is the deliberate direction — a duplicated audit event is identifiable
(every shipped event carries the `audit_log` row's own `id`) and reconcilable on
read, whereas a missing one is unrecoverable evidence. `base.py`'s module
docstring is the contract, and
`test_cloudwatch_ship_is_at_least_once_when_a_later_call_fails` pins the
behaviour so it can't be mistaken for atomicity again.

#### A 200 that dropped rows is a failure

`PutLogEvents` can succeed while silently discarding events, reporting them in
the response's `rejectedLogEventsInfo` (too old for the log group's retention
period, or too far in the future). The adapter reads that block and raises
`AuditShippingRejected`, so the shipper leaves `shipped_at` NULL, the sweep's
consecutive-failure streak climbs on `GET /api/health/sweeps`, and
`retention_sweep`'s `audit_rows_overdue_unshipped` counts the rows. Marking
them shipped would put a green light on evidence the WORM store never took —
the same failure mode the boot-time `test_connection` probe below exists to
prevent. The raised message carries only AWS's index fields, never a row body.

If this fires, the usual cause is a backlog older than the log group's
retention period: raise the retention on `FEOH_AUDIT_SHIPPING_CLOUDWATCH_GROUP`
(or ship the backlog to `s3_objectlock`, which has no age limit) and the next
tick drains it.

### `s3_objectlock` — S3 with Object Lock (Compliance mode)

Each batch becomes a single gzip-compressed JSONL object:

```
s3://<bucket>/audit/<tenant_db>/<YYYY>/<MM>/<DD>/<ISO-stamp>-<uuid>.jsonl.gz
```

**What the boot check requires.** `test_connection()` reads
`get_object_lock_configuration` and returns `False` — which refuses the boot,
see § Startup probe below — unless all three of these hold:

| Property | Required | Why it is checked |
| --- | --- | --- |
| `ObjectLockEnabled` | `Enabled` | Only settable at bucket creation; cannot be turned on later. |
| Default retention `Mode` | `COMPLIANCE` | GOVERNANCE lets a principal holding `s3:BypassGovernanceRetention` delete audit evidence early. COMPLIANCE locks even root until retention expires. `/legal/dpa` Annex II publishes compliance mode, so GOVERNANCE would make a published page false. |
| Default retention period | ≥ `FEOH_AUDIT_SHIPPING_S3_MIN_RETENTION_DAYS` (2555, ≈ 7 years) | Matches `infra/variables.tf` `audit_retention_days` and the figure the DPA names. S3 reports `Days` or `Years`; a `Years` value converts at 365/year so the check can only under-state the period. |

The mode is **not configurable**. A knob that let a deployment run GOVERNANCE
would make the DPA wrong for whoever turned it, and the point of the check is
that the published claim is true by construction.

**Other caveats:**

- A bucket with Object Lock enabled and **no default retention rule** is the
  quiet failure this check exists for: every PUT lands deletable while
  `get_object_lock_configuration` still reports `Enabled`. This adapter
  deliberately does not stamp `ObjectLockMode` / `ObjectLockRetainUntilDate`
  onto each object — the retention policy has one owner and it is the
  infrastructure — so without the bucket default there is no lock at all.
- Retention period + mode are provisioned by infra (Terraform), not by
  this adapter. See `infra/s3.tf`
  (`aws_s3_bucket_object_lock_configuration.audit_logs`).
- Object Lock cannot be turned off after bucket creation, but the default
  retention **rule** can be edited afterwards, so this is a start-up assertion
  rather than a standing guarantee. `ship()` does not re-check per batch: that
  would be an S3 round-trip per tick.
- Versioning must also be enabled — S3 Object Lock is implemented on
  top of object versions.

**Before this check existed**, `test_connection()` read only the
`ObjectLockEnabled` flag, and this module's own docstring said the bucket was
expected in *Governance* mode while the DPA said compliance. A hand-created
GOVERNANCE bucket, or one with no default rule, passed the boot probe and
shipped audit evidence an administrator could remove. Guarded by
`tests/test_audit_shipping.py` (`…refuses_governance_mode`,
`…refuses_missing_default_retention`, `…refuses_retention_under_the_floor`,
`…reads_a_years_retention`).

## Startup probe — a sink that can't hold the evidence stops the boot

When `FEOH_AUDIT_SHIPPING_ENABLED` is on and `FEOH_DEBUG` is off, the
lifespan builds every configured adapter and `await`s its
`test_connection()`. A `False` raises and the process does not start.

This closes a control that existed only on paper. Nothing in production
called `test_connection()` — it had test-only callers — so with a bucket
that lacked Object Lock every `put_object` succeeded, `_ship_tenant`
stamped `shipped_at` on the batch, and `retention_sweep` then reported
`audit_rows_overdue_unshipped: 0`. The SOC 2 evidence trail read green
end to end with no WORM guarantee anywhere behind it, and nothing in the
system could tell.

Refusing to boot (rather than warning) mirrors the unknown-provider guard
beside it: shipping to an unverified sink is worse than not shipping,
because it marks the rows shipped. Both guards are deployed-only —
`FEOH_DEBUG=true` local dev is unaffected, and so is any environment with
`FEOH_AUDIT_SHIPPING_ENABLED` off.

## How to add a new shipping provider

```python
# backend/app/services/audit_shipping/splunk_adapter.py

from app.services.audit_shipping.base import AuditLogRow, AuditShippingAdapter
from app.services.audit_shipping.dispatcher import register_audit_shipping_adapter


@register_audit_shipping_adapter("splunk")
class SplunkAdapter(AuditShippingAdapter):
    provider_name = "splunk"

    def __init__(self, config: dict):
        super().__init__(config)
        # read endpoint, HEC token, etc. from settings

    async def ship(self, rows: list[AuditLogRow]) -> None:
        # raise on failure — the shipper uses that to decide whether to
        # mark `shipped_at`. At-least-once semantics are fine.
        ...

    async def test_connection(self) -> bool:
        ...
```

Then:

1. Import the new module in `audit_shipping/__init__.py` so its
   registration decorator runs.
2. Add the provider name to `FEOH_AUDIT_SHIPPING_PROVIDERS`
   (comma-separated with existing ones).
3. Add a test to `tests/test_audit_shipping.py`.

## Tests

- `tests/test_audit_log_shipper.py` — sweep orchestration (tenant iteration,
  partial-failure tolerance, the no-adapters short-circuit, the loop) plus the
  realdb WORM invariant (rows stamped only when every sink ACKed).
- `tests/test_audit_shipper_poison_row.py` — the isolation pass: a row a sink
  refuses is quarantined with the PII-free marker and the **newer** rows for
  that tenant ship on the same tick; a sink outage is never quarantined (nothing
  stamped, nothing stripped, probing bounded); progress before a fatal row is
  still stamped; the substitution is per-adapter.
- `tests/test_audit_shipping.py` — adapter-level behaviour + the registry.

## Schema

Migration `0010_audit_log_shipping`:

- Adds `shipped_at TIMESTAMPTZ NULL` to the tenant-local `audit_log` table.
- Adds a partial index `ix_audit_log_shipped_at_null ON audit_log(created_at)
  WHERE shipped_at IS NULL` so the shipper's "unshipped rows, oldest first"
  query stays cheap as the audit log grows.
- Guarded by `_has_table("audit_log")` — no-op on the control plane.

### The index was only ever on MIGRATED tenants

For 82 revisions that index existed in the migration and nowhere else, and a
tenant only gets it if Alembic ran against it. Fresh tenants don't: they are
built by `Base.metadata.create_all` in
`services/tenant_provisioning._create_tenant_tables`, which produces exactly the
indexes the ORM declares — and `AuditLog` declared none. So every
freshly-provisioned tenant ran this 60-second sweep as a full sequential scan of
its largest table, forever, while a migrated tenant did not. Measured at 1.2 M
audit rows: **39.740 ms / 30 003 buffers → 0.040 ms / 1 buffer** for a
caught-up tick that returns no rows at all.

`0092_list_and_audit_indexes` closes it by declaring the index on
`AuditLog.__table_args__` — under **0010's name**, deliberately, because a second
identical partial index would be pure write overhead on every audit row for the
rest of the platform's life. 0092 also restates the `CREATE INDEX IF NOT EXISTS`
so an already-provisioned tenant picks it up when it reaches that revision, but
does not list it in its downgrade: reverting 0092 must not remove revision
0010's index. `tests/test_list_and_audit_indexes.py` pins all three properties.

Two more `audit_log` indexes land in the same revision — `(action, created_at)`
for the SOX signature sweep, the dashboard's approval-timing leg and the adaptive
feedback reads, and `(created_at)` for the auditor's date-range export (which has
no `action` predicate and so cannot use the composite). Both are also what the
retention sweep's overdue-row count reads. Sizes and before/after numbers are in
[`database.md`](database.md) § Index coverage on list + audit reads.

## The auditor export reads the same table, and must page it too

The shipper is not the only thing that walks `audit_log` at volume. The
SOX evidence surface — `GET /api/audit/export` (`app/api/audit.py`) — reads the
same table over a caller-chosen range, and the range that matters is an annual
one: a scratch tenant carries ~41 600 rows in a single 30-day window.

It is subject to the same "bounded, not whole-table" rule as the shipper, and
for the same reason, but it reaches it differently — the shipper takes a
`FEOH_AUDIT_SHIPPING_BATCH_SIZE` batch per tick and comes back next tick,
whereas an export has to deliver every row of its range in one response:

* rows are read through a server-side cursor (`yield_per`, the mechanism
  `GET /audit/verify-signatures` in the same module already used) selecting
  plain columns rather than the `AuditLog` entity, so nothing accumulates in the
  session's identity map;
* JSON and CSV bodies are rendered in bounded chunks as rows arrive, so peak
  server memory is a page rather than a period. Measured with the body
  discarded as it is sent (so the number is the server's, not a test client's
  copy of the response): peak allocation went from 18.0 MiB at 5 000 rows and
  70.6 MiB at 20 000 — **linear in the range** — to a flat **1.5 MiB at both**.
  JSON also got faster (3 244 ms → 1 573 ms at 20 000 rows); CSV traded about
  20% wall clock (1 340 ms → 1 611 ms) for 48× less peak memory, which is the
  right way round for a report an auditor runs occasionally on a shared worker.
* those chunks land in a **`SpooledTemporaryFile`** (`_spool_body`) rather than
  going straight out on the wire, and the tenant session is **closed before the
  response is returned** (`_drain_spool` then streams the finished file back in
  `_SPOOL_READ_BYTES` blocks and unlinks it when the last one is read, or when
  the client hangs up). See the connection-hold note below for why.

  That CSV regression was a *database* cost, not an application one, and it is
  addressed in the same round. Streaming bounds what the app holds, but the
  export's `ORDER BY created_at` had no index to read, so Postgres sorted the
  whole range first — `EXPLAIN (ANALYZE)` showed an `external merge` spilling
  4 392 kB to a temp file before the cursor could emit row 1, which is the
  opposite of incremental. `ix_audit_log_created_at` (added by
  `0092_list_and_audit_indexes`, above) removes the sort node entirely: the plan
  becomes a plain index scan and the export is genuinely incremental end to end.
  The two changes were found independently — one by measuring the export, one by
  reading the shipper — and land together.

**Do not "simplify" it back to a `LIMIT`.** A capped export is a silently short
one, and a short export is evidence somebody signs off on — strictly worse than
a slow one. Nothing in that path truncates, and a run that dies part-way cannot
pass for a complete one either: the response is chunked (no `Content-Length`),
so an aborted body has no terminating chunk and any conforming client raises,
and the JSON dialect additionally never emits its closing `]`.

Two consequences of streaming are worth knowing before editing that route:

* The `audit.exported` row is committed **before** the cursor opens (the access
  has happened by the time bytes leave, so an aborted download must still be on
  the trail). The export is therefore pinned to a snapshot — `created_at <`
  Postgres' own `now()`, read before anything is written — or the body would
  carry the record of itself, and rows a concurrent request committed meanwhile,
  neither of which the row's own `count` includes.
* That `count` is a real `COUNT(*)` over the same predicate, folded into one
  aggregate alongside the `DISTINCT actor_id` the actor-name lookup needs, so
  the range is scanned once and the query count stays fixed no matter how large
  it is.

**The connection is released before transmission, and that took a spool.**
Streaming the cursor straight into the response bounded the memory and, in the
same stroke, extended the connection hold from "while the rows are read" to
"until the last byte reaches the client" — the request kept one of the tenant's
`pool_size=5, max_overflow=10` connections checked out for the download's whole
duration, so a handful of concurrent annual exports over slow links could
exhaust the pool while Postgres itself sat idle. Draining the rendered chunks
into a `SpooledTemporaryFile` first and `await db.close()`-ing before the
`StreamingResponse` is returned keeps both properties instead of trading one for
the other: peak RSS stays bounded by `_SPOOL_MAX_MEMORY_BYTES` (1 MiB; past that
the rest rolls to disk, so a year-long range costs temp-file space, not memory)
and the connection is back in the pool before a byte moves. `ix_audit_log_created_at`
(above) is the other half of the same fix — it removed the sort that made the
read itself long.

Two consequences, both deliberate:

* A database error part-way through the range now surfaces **before** the first
  byte, as a clean 500, instead of truncating a body the client had already
  begun saving. That is the same principle as the un-terminated chunk and the
  missing JSON `]`: an incomplete SOX export must never look complete.
* The spool file must be closed on **both** exits — the last block read, and the
  client hanging up mid-download (Starlette closes the body generator, so
  `_drain_spool`'s `finally` is what unlinks it). An abandoned download is the
  path that leaks quietly, because nothing surfaces an error.

Streaming still rests on FastAPI keeping its `yield`-dependency teardown *after*
the response body drains, which is internal ordering rather than a documented
contract — the spool makes the export no longer *depend* on that for its session
(it has already closed it), but the dependency still tears down late.
`tests/test_audit_export_streaming.py` drives the raw ASGI app against a real
database, so a future FastAPI bump that reordered it fails loudly there rather
than silently handing the generator a closed session.

**Both connections are released, by two different mechanisms.** The tenant
session is closed explicitly (`_spooled_response`). The *control-plane* session
— the one the batched actor-name lookup uses — is never closed by this route at
all; it is released because `database.commit_before_response` ends its read
transaction on the exit stack FastAPI unwinds *before* sending (decisions §20).
That indirection is load-bearing and invisible at the call site: a control read
added after the spool, or a FastAPI bump that reordered that stack, would hold
one of the CONTROL pool's connections for the client's bandwidth — and unlike
the tenant pool, that one is shared by every tenant on the process. The
`format=pdf` dialect has the same obligation for a different reason: it cannot
stream, so it materialises the entries, but it closes the tenant session
*before* `render_audit_report_pdf` runs rather than holding a connection across
a ReportLab layout that touches no database.

That file guards both halves of the streaming contract — that it is chunked, and
that it returns every row. `tests/test_audit_export_connection.py` guards the
spool: neither the tenant nor the control connection checked out at any body
chunk, on all three dialects (asserted on the engines' own pool
checkout/checkin events), bytes identical to the module's independent
materialising renderer, and the temp file closed on both exits.

## DB-level immutability (SOX) and the `shipped_at` carve-out

Migration `0022_sox_audit_immutable` (DDL in `app/services/audit_immutability.py`)
installs a pair of `BEFORE` triggers on `audit_log`:

- `audit_log_no_delete` — rejects **every** DELETE.
- `audit_log_no_update` — rejects every UPDATE that changes any column **other
  than `shipped_at`**.

This is the durable SOX control: the app already exposes no PATCH/DELETE route
(`tests/test_audit_append_only.py` — see [The audit-coverage
guard](#the-audit-coverage-guard-teststest_audit_append_onlypy) below), but a
rogue ORM call or a direct `psql` session would bypass that — the trigger does
not.

**The `shipped_at` carve-out is load-bearing for this shipper.** The trigger
function compares all non-`shipped_at` columns between OLD and NEW; a pure
`shipped_at = now()` stamp (the only write the shipper performs, step 3 above)
passes, while re-stamping `shipped_at` *alongside* any other edit is rejected.
If you ever change how the shipper marks rows shipped, it must remain a
`shipped_at`-only UPDATE or the trigger will refuse it.

The triggers are installed on **every** tenant DB: migration `0022` fans out
across existing tenants (`scripts/migrate_all_tenants.py`), and
`tenant_provisioning._create_tenant_tables` installs the same DDL on freshly
provisioned tenants (which are created via `create_all`, not Alembic).
Idempotent (`CREATE OR REPLACE` / `DROP ... IF EXISTS`), guarded by
`_has_table("audit_log")`. Covered by `tests/test_audit_immutable.py`,
including an assertion that the shipper's stamp still succeeds.

## The audit-coverage guard (`tests/test_audit_append_only.py`)

The triggers above make the trail **tamper-proof**. They say nothing about
whether a mutation produced a row in the first place — that is what
`tests/test_audit_append_only.py` enforces, and invariant #3 is the rule it
encodes.

### The unit is the HANDLER, not the module

`test_every_tenant_mutating_handler_writes_an_audit_row` sweeps every route
whose handler takes `Depends(get_tenant_db)` and responds to
`POST`/`PATCH`/`PUT`/`DELETE` — the precise marker for "this writes tenant
state" — and requires `dispatch_audit` to be reachable from **that handler's own
source**:

1. `inspect.getsource(endpoint)` contains `dispatch_audit`; or
2. the handler calls a function defined in the **same module** whose source
   does (followed up to `_AUDIT_HELPER_MAX_DEPTH`, so a handler delegating to a
   local `_audit(...)` / `_transition(...)` helper still counts); or
3. `(module, handler)` carries a written-down reason in
   `_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT`.

It used to require `dispatch_audit` anywhere in the **module**, which meant one
auditing handler vouched for every unaudited handler beside it —
`app/api/invoices.py` alone has 21 tenant-mutating routes behind that single
grep, and that is how three unaudited DELETE handlers shipped there and had to
be found by hand.

Calls **out of** the handler's module are deliberately not followed. "That other
file audits" is a design claim about a chokepoint (`workflow_engine.transition_invoice`,
`services/review`, `exception_lifecycle.record_decision`, `services/qms_sync`,
…), and a claim belongs somewhere a reader can re-check it, not inferred by a
source scan that would quietly absorb the day the chokepoint stops auditing. So
those handlers are exemptions with the chokepoint named in the reason.

### Adding a justified exemption

Add one entry to `_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT`, keyed
`("app.api.<module>", "<handler function name>")`, whose value states **why the
mutation is already covered**. The three shapes that hold up today:

| Shape | Example reason |
|---|---|
| Verb is a POST but nothing is persisted | `"CSV export — reads, never writes"` |
| Writes only the caller's own record / a derived cache | `"read receipt on the caller's own notification"` |
| Audits through a named cross-module chokepoint | `"audits via services/review.approve_invoice"` |

A bare or empty reason fails the suite. So does a stale one:
`test_audit_exemption_list_has_no_stale_entries` re-derives the route map and
fails when an exempted handler no longer has a tenant-mutating route (renamed,
deleted, or its dependency changed) or has since started auditing — an
exemption that has stopped being true is worse than none.

### `_OPEN_AUDIT_HOLES` — not exemptions

When the per-handler unit first landed it exposed handlers that genuinely mutate
tenant business state with no audit row anywhere on the path. Those are listed
in a **separate** `_OPEN_AUDIT_HOLES` dict, every reason prefixed
`OPEN HOLE — …, not a justified exemption`, so the suite is green on a known,
enumerated set rather than by widening the real exemption dict. Each entry is
work still to do; fixing one makes its entry stale, which is the prompt to
delete it. **Do not add to that dict** — a newly-surfaced unaudited mutating
handler is a bug to fix.

### Route discovery has a floor

FastAPI 0.138+ keeps nested `_IncludedRouter` objects in `app.routes` instead of
flattening sub-routes, so the historical
`[r for r in app.routes if isinstance(r, APIRoute)]` sees **1** route out of 564
— and every filter built on it yields an empty set, i.e. a guard that passes
having examined nothing. Every sweep in the file now goes through one
`_flat_routes()` helper (`fastapi.routing.iter_route_contexts`, with the flat
list as an older-FastAPI fallback) and asserts `len(routes) >=
_MIN_EXPECTED_ROUTES`. That floor is the real fix: the flattening can move
again, and when it does the suite fails loudly instead of reporting green on an
empty scan. `test_route_flattener_sees_the_whole_app` pins it on its own.

## Operational notes

- **Disabled in local dev**. The default `audit_shipping_enabled=false`
  means your laptop doesn't need AWS credentials.
- **Replays on retry** are expected and safe — both CloudWatch and S3
  treat the same row ID as a new event / new object. Downstream tooling
  (SIEM, auditor queries) should dedup on `audit_log.id` if exact
  uniqueness matters.
- **Tenant scale**. With 1,000 tenants and a 60-second interval, the
  shipper opens + disposes ~1,000 engines per minute. Each spin-up is
  cheap (< 10ms on warm DNS), but if this ever becomes a hotspot the
  cache in `database.get_tenant_engine` is the right place to plug in.
- **Back-pressure**. If shipping falls behind (adapter latency > tick
  interval), the backlog builds up inside `audit_log`. The partial
  index keeps the SELECT cheap; the shipper just needs more ticks to
  catch up. There's no hard cap on backlog size.
- **No alerting yet**. Out of scope for this PR; see the SOC 2 readiness
  doc's "Alerting" row (pending).
