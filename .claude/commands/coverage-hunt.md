---
description: Hunt for untested behaviour and invariants and backfill the right layer of tests (pytest / Playwright e2e / flutter) — no bug required. The proactive, area-scoped counterpart to the diff-scoped test-gap-checker. Commits scoped.
argument-hint: "[optional scope — a module, route, feature, or path, e.g. backend/app/services/po_matching.py, frontend/tests-e2e/invoices, 'the append-only audit log'; omit to pick an under-covered area]"
---

Find behaviour that *works but isn't tested* and lock it in with tests at the layer the repo's conventions call for. Unlike `/bug-hunt` (whose deliverable is a fix) and the diff-scoped `test-gap-checker` agent (which reviews a working diff), `/coverage-hunt` proactively hardens an **area** of already-landed code — the honest "the deliverable is the coverage gap" command. If you trip over a real bug while writing the tests, fix it (that's a `/bug-hunt`-style win) — but the goal here is durable coverage, not a fix.

`$ARGUMENTS` is an optional scope. If empty, pick an under-covered area (step 1).

## Operating rules (non-negotiable — root `CLAUDE.md` guard rails)

- **Test real behaviour, not the implementation.** Assert the observable contract and the invariant a regression would break — not internal call shapes. A test that only restates the code teaches nothing and breaks on every refactor.
- **It must be able to fail.** A test that can't fail is worse than none. Sanity-check by reverting the behaviour mentally (or briefly in fact) — if the test stays green, it's not testing anything. Cover the edges (null/empty/zero/unicode/boundary/concurrent/out-of-order), not just the happy path.
- **No masking, ever.** No sleeps, `waitForTimeout`, inflated timeouts, retries, or loosened assertions to make a test pass. Wait on real signals (a `data-ready` attribute backed by real state, an exposed status, a network response). If a deterministic wait needs a new app affordance, add it as a real readiness signal. (Guard rail 4; "Fix bugs at the source".)
- **Match the layer + the tool.** There are exactly three test layers — pick the one that owns the behaviour:
  - **Backend** (pure logic, services, API) → pytest in `backend/tests/test_*.py`. Run `pytest` from `backend/` with the venv active. Use the `conftest.py` fixtures and match the style of the existing suites (`test_approval_thresholds`, `test_audit_append_only`, `test_duplicate_detection`, `test_compliance`, `test_banking_validation`, `test_cross_border_ach`, `test_csv_import`, …).
  - **Frontend** (user-visible) → Playwright `frontend/tests-e2e/<area>/*.spec.ts` (areas: auth, invoices, vendors, payments, purchase-orders, goods-receipts, credit-memos, exceptions, workflows, admin, organization, sso, scim, email, smoke). Run one from `frontend/`: `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium`. Needs the full stack up + seed.
  - **Mobile** → `flutter test` in `mobile/test/`.
  (See `frontend/tests-e2e/README.md` and the existing `backend/tests/` patterns for conventions.)
- **Deterministic + parallel-safe.** e2e specs may run against shared seed data — prefer read-only assertions, unique nonces for any writes, and don't depend on additive-seed counts being exact. Wait on real readiness signals, never sleeps. (DETERMINISM rules; guard rail 4.)
- **Commit scoped.** `test(...)` commits, path-scoped. (Guard rail 1; Git workflow.)

## Procedure

### 1. Find the gap

- **If `$ARGUMENTS` is given:** that's the area.
- **If empty:** map source → tests and rank by exposure. Backend: `backend/app/services/*.py` + `backend/app/api/*.py` vs `backend/tests/test_*.py`; frontend: routes/components vs `frontend/tests-e2e/`; mobile: `mobile/lib/` vs `mobile/test/`. Rank by **logic density × thin-or-no coverage × hot path**. Skip generated / config / migration / seed files. State your pick + why in one line. (The `Explore` agent is the fast way to map source↔tests across the three layers.)
- For the chosen area, **enumerate the untested behaviours**: each branch, each error path, each documented invariant (the root `CLAUDE.md` "Project invariants" and inline comments are a checklist of promises that should each have a test), each edge case. List them before writing — that list is the work.
- **Invariants worth locking** (AP's promises, each should have a test): money exact (`Decimal`/`Numeric`, never `float`); idempotency on money-moving writes; audit append-only on status transitions (`services/audit_shipping/`); tenant isolation (`get_tenant` / `get_tenant_db`, `X-Tenant-Slug` + JWT `org` cross-check — cross-tenant access must 404/403); auth before everything; webhook HMAC + dedupe (`webhook_security.py`) returns 204 on every rejection path; workflow transitions only via `workflow_engine.py::VALID_TRANSITIONS`; PO matching (`po_matching.py`); duplicate detection (`invoice_warnings.py`); passwords via `bcrypt_sha256`.

### 2. Confirm current behaviour

Run the code / read the contract so the test encodes what the app *actually does today* (not what you assume). For a backend service or pure fn, call it under pytest; for an HTTP route, hit the endpoint; for frontend, drive it in the running stack (`pnpm dev:all` then `pnpm seed`; login `demo@acme.com` / `demo` at `acme.localhost:7777`). If today's behaviour looks wrong, that's a `/bug-hunt` finding — fix it and test the corrected behaviour (don't enshrine a bug in a test).

### 3. Write tests that lock the contract

- One assertion cluster per behaviour/invariant from the step-1 list. Name tests so a failure reads as a sentence about what broke.
- Include the edges and the failure paths (4xx/empty/duplicate/out-of-order/concurrent), not just the happy path.
- For e2e, anchor on stable selectors + real readiness signals; keep writes nonce-tagged and assertions tolerant of additive-seed volume.

### 4. Verify

- Run the new tests (they pass) **and** prove they can fail — flip the behaviour briefly or reason it through explicitly; a test you haven't seen fail is unverified.
- Run the nearby existing suite to prove no collision; report pass/fail counts faithfully.
- Lint gate if you touched any non-test code (e.g. added a readiness attribute): `ruff check .` (backend), `pnpm check` (svelte-check), `flutter analyze` — or the aggregate `pnpm lint`.

### 5. Commit (scoped)

`test(<area>): …` commits, path-scoped (`git commit -m "…" -- <paths>`; the git-scope-guard hook blocks bare / whole-tree commits). If you added a real app affordance for determinism (a readiness signal), that's a separate non-test commit with its doc update. No co-author / "Generated with" trailer.

## Report

```
## /coverage-hunt — <area>

**Picked:** <area> — <one-line why it was under-covered> (omit if named)

**Behaviours/invariants now covered:** <bulleted list — each with its layer (pytest/e2e/flutter)>

**Tests added:** <files + count>

**Bug found while testing:** <if any: what + fix — else "none; behaviour matched the contract">

**Verification:** <new tests N/N; can-fail check done; nearby suites N/N; lint gate if touched>

**Commits:** <hash + subject>

**Still uncovered (recommended next):** <behaviours intentionally left — or "area is now well-covered">
```

## Tone

The deliverable is honest coverage, not a vanity green. Name what's now locked in and what you deliberately left. If you couldn't make a test able to fail, say why and don't ship it.
