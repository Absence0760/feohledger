---
description: Triage and source-fix a flaky/failing Playwright e2e test. Brings up the full stack, resolves the target (spec path / CI run / the currently-red shard), then delegates to the `flake-doctor` agent — which finds the root cause, fixes the app (or the test if the test is the bug), and verifies. Never masks a flake with sleeps, retries, or inflated timeouts.
argument-hint: "[spec path | CI run URL | 'red e2e'] — what to triage (empty = the currently-failing e2e on the latest CI run / working tree)"
---

Triage `$ARGUMENTS` with FeohLedger' `flake-doctor` agent and fix it at the source.

## Usage

- `/flake-doctor frontend/tests-e2e/invoices/invoices.spec.ts` — a specific spec.
- `/flake-doctor frontend/tests-e2e/invoices/invoices.spec.ts:42` — a single test by line.
- `/flake-doctor https://github.com/<org>/feohledger/actions/runs/<id>` — pull the failing test(s) from a CI run.
- `/flake-doctor` — auto-detect: read the latest CI run's red e2e job, fall back to the working tree if CI is green.

## Procedure

1. **Resolve the target.** `$ARGUMENTS` is one of:
   - **A spec path** (optionally `:line`) under `frontend/tests-e2e/<area>/**/*.spec.ts` — used as-is. Specs live in area subdirs (`auth/`, `invoices/`, `vendors/`, `payments/`, `purchase-orders/`, `goods-receipts/`, `credit-memos/`, `exceptions/`, `workflows/`, `admin/`, `organization/`, `sso/`, `scim/`, `email/`, `smoke/`); `ls frontend/tests-e2e/` if you need to disambiguate a bare name.
   - **A GitHub Actions run URL** — `gh run view <id> --log-failed` (or pull the failed-jobs annotations) to extract the failing spec(s) and the assertion that blew up. Multiple reds → list them and confirm which to take first; don't fan out silently.
   - **Empty / `red e2e`** — `gh run list --workflow=ci.yml -L 5` to find the latest run whose `e2e` job is failing, then `gh run view <id> --log-failed` to extract the spec. (`claude.yml` may also run e2e — check it if `ci.yml` is green but you suspect a red shard.) If CI is green, fall back to whatever's failing in the working tree.

2. **Bring up the stack** (the e2e run needs the real backend + DB — Playwright only manages the frontend):
   - `pnpm db:up` — docker-compose Postgres + Redis + MinIO.
   - `pnpm seed` — `python scripts/seed.py`; creates the demo tenants (`acme`, `techflow`) and the `e2e<N>` tenants the worker fixture pins to (`FEOH_E2E_TENANT_COUNT`, default 4).
   - Backend on :8000, from `backend/`:
     ```
     cd backend && source .venv/bin/activate && python main.py
     ```
     (`main.py` is the auto-reload dev entrypoint.) The shortcut for the whole web stack is `pnpm dev:all` (`db:up` then backend :8000 + frontend :7777 together).
   - The **frontend (:7777) is owned by Playwright's `webServer` block** in `frontend/tests-e2e/playwright.config.ts` (it runs `pnpm dev`, or `vite preview` when `FEOH_E2E_USE_PREVIEW=true`), with `reuseExistingServer` locally — so let Playwright start it; don't double-bind :7777. The **backend (:8000) and DB are NOT managed by Playwright** and must be up first. baseURL is `<slug>.localhost:7777` (Chromium resolves `*.localhost` to 127.0.0.1, no `/etc/hosts` edits).
   - Confirm the admin storageState exists at `frontend/tests-e2e/.auth/e2e1-admin.json`; if missing, run the Playwright auth setup project once so the authenticated specs have a session.

3. **Delegate to the `flake-doctor` agent.** Spawn it with the resolved target as the **first sentence** of the prompt, e.g.:

   > "Triage `frontend/tests-e2e/invoices/invoices.spec.ts` (failing assertion: `<paste the assertion / error from the CI log or local run>`). Find the root cause, fix it at the source per your spec, then verify. The stack is up (db + Redis + MinIO + backend :8000; Playwright owns the frontend on :7777). Do not commit."

   The agent reproduces the failure, isolates the root cause (app bug, missing readiness signal, or a genuinely-broken test/fixture), applies the **source** fix, writes its findings to `reviews/flake-<scope>.md`, and re-runs to confirm green. Trust its spec — don't narrate its internal steps.

4. **Verify locally** with the same command the agent uses, so the operator can re-run it (run from `frontend/`):
   ```
   pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium
   ```
   For a single test, append `:line` to the spec path or use `-g "<title>"`.

5. **Relay the agent's report.** Surface the root-cause one-liner, the files changed (`git diff --stat`), the path to `reviews/flake-<scope>.md`, and the verifying run's result. Then ask the operator whether to commit.

## Notes

- **Fix at the source, never mask** — the project's hard rule (root `CLAUDE.md` → "Fix bugs at the source", guard rail 4) binds the agent and this command. Forbidden "fixes": inflating an `expect`/`toBeVisible` timeout to absorb a flake, `page.waitForTimeout(N)` between actions, bumping `--retries` or Playwright's `retries`, loosening a strict assertion (`toHaveText` → `toContainText(/.*/)`), or `test.skip`/`fixme`/`fail` against a real bug without a named follow-up. If the page is slow or the signal is unreliable, fix *that* — add a real readiness affordance (a `data-ready` attr backed by an actual state signal, an exposed status, a network response to await) in the app code, not test scaffolding.
- **The agent does not commit.** The operator reviews `git diff` + `reviews/flake-<scope>.md` and commits (path-scoped, never `git push`, no `Co-Authored-By`/"Generated with" trailer — guard rails 1 and the Git workflow). Suggest a `fix(...)` or `test(...)` message; don't pre-stage.
- **Status markers** in `reviews/flake-<scope>.md` follow the repo convention: `[ ]` open / `[x]` fixed / `[~]` deferred. Re-running overwrites the file.
- **Project invariants still bind the fix** (guard rail 11). A source fix that touches a money path, a status transition, tenant resolution, or a webhook must keep money `Decimal`, write the audit row, resolve the tenant via `X-Tenant-Slug` → `feoh_<slug>`, and verify+dedupe webhooks — the flake fix is not an excuse to violate one. **Docs-as-code** (guard rail 12): if the fix changes a behaviour, command, env var, or port, the doc update ships in the same change.
- **Optional polluted-tenant robustness check.** Flakes that vanish on a fresh seed but recur in CI are often cross-spec state in the shared worker tenant — the seed isn't fully deterministic, or the test assumes counts another spec mutates. The backend is multi-tenant by **database-per-tenant** (`feoh_<slug>`, resolved through `backend/app/tenant.py::get_tenant` off the `X-Tenant-Slug` header); locally each Playwright worker is pinned to its own `e2e<N>` tenant via the worker-scoped `tenantSlug` fixture in `frontend/tests-e2e/fixtures/helpers.ts`, and `fullyParallel: false` keeps a file's tests serial within a worker. If a test reads denormalized counts (status chips, KPI totals, `Showing all N`), confirm it scopes to its own tenant and doesn't depend on global ordering or another spec's writes. Re-run against a freshly `pnpm seed`-ed DB to confirm the fix isn't just papering over fixture pollution.
- This is **not** `/check` (pre-commit gate) or a broad audit sweep — it's a single red test, end to end.
