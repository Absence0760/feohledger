---
description: Hunt for real performance problems — N+1 queries in list endpoints, missing/unused indexes, O(n²) loops (PO-match / duplicate-detection recompute), aggregate/dashboard math, oversized payloads, render thrash — measure each before and after, fix the root cause, and guard with a test where it fits. Commits scoped.
argument-hint: "[optional scope — a route, query, layer, or page, e.g. 'GET /api/invoices', the analytics dashboard math, the invoice list; omit to profile the hot paths]"
---

Find performance problems that **actually bite at realistic scale** and fix them at the root. The discipline that separates this from premature optimization: **measure before you touch anything, and measure again after** — a fix with no before/after number is a guess, not a perf fix. This is the actionable, fix-and-land counterpart to the read-only audits under `/audit/*` (and to `/audit-money-path` for money paths, `code-reviewer` for tenancy).

`$ARGUMENTS` is an optional scope (a route, query, layer, or page). If empty, profile the hot paths (step 1).

## Operating rules (non-negotiable — root `CLAUDE.md` guard rails)

- **Measure first, measure after.** Quantify the cost before changing anything (`EXPLAIN (ANALYZE, BUFFERS)` via `psql` against a realistically-seeded tenant DB for SQL, row-count-scaled timing for Python loops, payload bytes, Playwright trace / `performance` marks for the frontend) and report the before→after delta. No measurement, no claim.
- **Correctness is not negotiable for speed.** A faster path must return identical results and preserve every invariant — especially tenant isolation. AP is database-per-tenant: never drop the `get_tenant` / `get_tenant_db` chokepoint for a raw cross-tenant query to save a hop, never widen a query past its tenant scope, never cache across tenants, never hardcode a tenant DB name. (Rails 7, 11.)
- **Fix the root cause, not the symptom.** Add the missing index / batch the N+1 / hoist the invariant work out of the loop — don't paper over a slow path with a cache that then needs invalidation, unless caching genuinely is the right answer (and then invalidation is part of the fix). (Rail 4.)
- **Prove the scale matters.** A microsecond on a 10-row dev table is noise. Reason about (or seed) realistic row counts — `pnpm seed` and the `scripts/` seeders add data; pump more rows into `feoh_acme` if dev tables are too small to show the problem. State the n at which the problem bites. Skip changes that only help at sizes the product never reaches.
- **Docs-as-code; commit scoped.** A new index ships in an Alembic migration via `/safe-migration` discipline (`IF NOT EXISTS`, fans out to **every** tenant DB, `migration-coordinator` in the loop); doc any changed perf-relevant convention. Fix and any test as separate path-scoped commits (`git commit -m "…" -- <paths>`). (Rail 12; git workflow.)

## Where the cost has actually lived here

- **N+1 in list endpoints.** Invoice / vendor / payment list routes (`backend/app/api/*.py`) that loop and fire a per-row query (or a per-row subquery that re-scans). Look for correlated subqueries in `SELECT` lists and `for row in rows: await db.execute(...)`.
- **Aggregate / dashboard math.** The analytics CFO dashboard and KPI aggregates (`/api/analytics`, `/api/dashboard`) — aging, spend, trends, rebates — that re-walk large tables or recompute totals row-by-row instead of one set-based query. Quantify at a realistic invoice/payment count before deciding it's worth batching/deferring.
- **Recompute storms.** Duplicate-detection / similarity search (`backend/app/services/invoice_warnings.py`, embeddings) and PO matching (`backend/app/services/po_matching.py` — invoked after **every** extraction and on **every** invoice mutation). An O(n²) re-scan over sibling rows per event is the classic shape here.
- **Missing, redundant, or unused indexes.** A `WHERE` / `JOIN` / `ORDER BY` column with no supporting index (seq scan at scale) — invoice `status`, `vendor_id`, tenant-FK columns are the usual suspects; a composite index with the wrong column order; duplicate indexes; indexes nothing queries.
- **Oversized payloads.** An endpoint returning unbounded rows or fat columns the caller doesn't need — invoice list shipping full line items / extraction JSON / audit-log detail; payment list shipping full payment-method detail. Cap, paginate, or project. (Watch PII/banking fields: trimming them out of a list payload is a correctness *and* invariant win.)
- **The in-process worker pool.** The pool of 3 threads draining the extraction / ERP / audit queues runs each engine at `pool_size=1, max_overflow=0` to stay under PostgreSQL's connection limit — a hot loop that opens a fresh tenant engine per item, or a per-item query that should be batched, starves the pool. Measure queue drain time at a realistic backlog.
- **Frontend render thrash.** A `$derived` / `$effect` recomputing a big sort/filter on every keystroke; an un-keyed `{#each}` re-rendering a long list; client-side filtering of a server-paginated set that silently hides rows (a correctness *and* perf smell). All under `frontend/src/lib/`.
- **Repeated invariant work in a loop** — recompiling a regex, re-parsing, re-fetching config per iteration instead of once.

## Procedure

1. **Pick + measure the target.** If `$ARGUMENTS` is given, profile it. If empty, rank the hot paths (extraction/PO-match on every invoice mutation, `GET /api/invoices`, invoice detail, analytics/dashboard aggregates, duplicate detection, the worker-pool drain) by call frequency × data volume. For the top pick, capture a **baseline number** the way that path is actually exercised: `EXPLAIN (ANALYZE, BUFFERS)` against a realistically-seeded tenant DB (`feoh_acme` — seed more rows if dev is too small to show the problem), a row-count-scaled timed loop, payload byte size, or a Playwright trace. Report it.
2. **Find the root cause.** Map the query plan / the loop's complexity / the payload shape. Confirm it's the actual bottleneck, not an assumption — the slow line is rarely the one you'd guess.
3. **Fix at the root.** Add the index (in an Alembic migration, fanned out to every tenant), batch the N+1 into a single set-based query, hoist invariant work, project/paginate the payload, memoize the frontend derivation. Keep the result byte-for-byte identical (diff before/after output on the same input). Money stays `Decimal`; tenant resolution stays through `get_tenant_db`.
4. **Re-measure.** Same workload, same method, same seeded DB. Report before→after (plan node, ms, bytes, rows scanned). If the delta is noise at realistic scale, **revert** — a non-improving change is not a fix.
5. **Guard it** where a test fits: a pytest asserting the result is unchanged and (where meaningful) that the query issues one round-trip not N; a Playwright spec for a frontend win; a migration's index covered by the schema. Don't write a flaky wall-clock assertion — assert the **structural** win (one query not N, bounded rows), not a raw millisecond threshold.
6. **Verify + review.** Lint/type gate (`ruff check .` + `ruff format .`; `pnpm check` for frontend); new/nearby tests pass (`backend/tests/test_*.py` via `pytest`; the one spec via `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium` from `frontend/`; report counts). Index migrations: apply locally and fan out via `/safe-migration` (`migration-coordinator`). For anything touching tenancy or a money/gate signal, run `code-reviewer` (and `/audit-money-path` if a money path changed).
7. **Commit** scoped (migration, fix, test as separate path-scoped commits as applicable).

## Report

```
## /perf-hunt — <scope>

**Target + baseline:** <path> — <how measured> → <before number (plan/ms/bytes/rows)>

**Problem:** <root cause: N+1 / seq scan / O(n²) recompute / fat payload / render thrash> — bites at n ≈ <scale>

**Fix:** <index / batched query / hoist / paginate / memoize> — result identical (verified)

**After:** <same measurement → after number> | **delta:** <e.g. 1200ms → 40ms, seq scan → index scan, 30 queries → 1>

**Guard:** <pytest/Playwright/migration that locks it — or "structural; covered by <x>">

**Verification:** <lint/type gate; tests N/N; migration applied + fanned out>

**Commits:** <hash + subject>

**Deferred / recommended:** <bigger wins out of scope, with the approach named — or "nothing outstanding">
```

## Tone

Numbers or it didn't happen. Lead with before→after. If the suspected hot path turned out fine at realistic scale, say so and move on — don't ship an optimization that doesn't move a measured number.
