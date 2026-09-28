# Backend — CLAUDE.md

Backend-specific guidance. See root `CLAUDE.md` for project-wide context.

## Where to look (backend docs)

Deep-dive docs live in `backend/docs/`:

| Topic | File |
|-------|------|
| Every router under `/api`, per-route detail | `docs/api-surface.md` |
| What each `services/` module owns | `docs/services-map.md` |
| Pluggable-provider families, per adapter | `docs/adapter-patterns.md` |
| REST API reference | `docs/api-reference.md` |
| PostgreSQL schema + migrations | `docs/database.md` |
| AI extraction adapters | `docs/ai-extraction.md` |
| Inbound structured e-invoicing (UBL / Factur-X / ZUGFeRD) | `docs/e-invoicing.md` |
| Conversational AP assistant | `docs/conversational-assistant.md` |
| AI Cash-Flow Copilot (Phases 1–2 — read-only cash tools + proposed plans + `/api/cash-flow` façade) | `../docs/cash-flow-copilot.md` (repo-root `docs/`) |
| ERP adapters (Merge.dev + direct) | `docs/erp-integration.md` |
| Workflow state machine | `docs/workflow-design.md` |
| Workflow snapshot semantics | `docs/workflow-snapshots.md` |
| Payment runs + ERP sync | `docs/payments.md` |
| Dynamic discounting & early-payment optimization | `docs/dynamic-discounting.md` |
| Recurring / subscription invoices | `docs/recurring-invoices.md` |
| International payments (FX + SEPA + SWIFT) | `docs/international-payments.md` |
| Multi-currency reporting (reporting currency + unrealized FX) | `docs/multi-currency.md` |
| International tax (VAT / GST / withholding) | `docs/international-tax.md` |
| Bank reconciliation | `docs/bank-reconciliation.md` |
| Vendor statement reconciliation | `docs/vendor-statement-reconciliation.md` |
| Positive Pay / payment-fraud file | `docs/positive-pay.md` |
| Analytics + CFO dashboard + CSV + scheduled reports | `docs/analytics.md` |
| Custom (ad-hoc) report builder | `docs/report-builder.md` |
| Virtual cards (Lithic / Nium) | `docs/virtual-cards.md` |
| PO matching (2-way / 3-way) | `docs/po-matching.md` |
| Line-total reconciliation (lines vs the header amount) | `docs/line-total-reconciliation.md` |
| Vendor management | `docs/vendor-management.md` |
| Local AI testing (Ollama) | `docs/local-ai-testing.md` |
| Docker Compose services | `docs/docker.md` |
| Redis | `docs/redis.md` |
| MinIO / S3 | `docs/minio.md` |
| Audit-log shipping (SOC 2) | `docs/audit-log-shipping.md` |
| Background sweeps — health, supervision, the shared loop runner | `docs/background-sweeps.md` |
| Periodic access reviews (SOX) | `docs/access-reviews.md` |
| Audit-log summarization (invoice modal) | `docs/audit-summary.md` |
| Email + in-app notifications | `docs/notifications.md` |
| Email approval (approve/reject from the email, no login) | `docs/email-approval.md` |
| Slack interactive approval (approve/reject from Slack buttons, no login) | `docs/slack-approval.md` |
| Exception queue lifecycle + its append-only audit trail | `docs/exception-lifecycle.md` |
| Exception agents (autonomous resolution) | `docs/exception-agents.md` |
| Adaptive AI workflows | `docs/adaptive-workflows.md` |
| Data enrichment (auto-fill, price variance, vendor scoring) | `docs/data-enrichment.md` |
| PEPPOL AS4 outbound (e-invoice transmission) | `docs/peppol.md` |
| Contract management (CLM) | `docs/contracts.md` |
| Expense management (incl. multi-currency locked-FX reports) | `docs/expense-management.md` |
| Digital signatures on approvals (SOX) | `docs/approval-signatures.md` |
| Retention policies (SOX records management) | `docs/retention.md` |
| Privacy — GDPR/CCPA DSAR export + right-to-erasure | `docs/privacy.md` |
| Public Developer API (API keys + `/api/v1`) | `docs/public-api.md` |
| Platform billing & metering (plans / subscriptions / entitlements) | `docs/billing.md` |
| 1099 tracking + the card-rail (1099-K) exclusion | `docs/tax-1099.md` |

Cross-cutting topics (auth, multi-tenancy, deployment) live at the repo root `../docs/`.


## Stack

- **FastAPI** on **Python 3.12+**, async throughout
- **SQLAlchemy 2** async with asyncpg driver
- **Alembic** for migrations (supports per-tenant execution)
- **PostgreSQL 16**, **Redis 7**, **MinIO** (S3-compatible)
- **Pydantic v2** for request/response schemas
- **ruff** for lint/format (line-length 100, rules: E, F, I, UP)

## First-time setup (from `backend/`)

```bash
docker compose up -d                            # start Postgres, Redis, MinIO
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                         # install with dev deps
python scripts/seed.py                          # seed 2 demo tenants
python main.py                                  # dev server on :8000
```

## Commands (from `backend/`)

```bash
docker compose up -d          # Postgres, Redis, MinIO (core)
docker compose --profile idp up -d keycloak   # opt-in local OIDC IdP (pnpm idp:up); see docs/docker.md
python main.py                # dev server :8000 (auto-reload via uvicorn)
pytest                        # run tests
ruff check . && ruff format . # lint + format

# Migrations
alembic revision --autogenerate -m "description"
alembic upgrade head                                # control plane
FEOH_MIGRATE_TENANT=feoh_acme alembic upgrade head      # single tenant
python scripts/migrate_all_tenants.py               # all tenants
```

## Running backend tooling from a git worktree

`backend/.venv` does not carry into a worktree, and reusing the primary venv can
silently run the **primary checkout's** code. The editable install registers a
`sys.meta_path` finder mapping `app` to the primary tree. It is **appended**, so
it sits after `PathFinder` and `PYTHONPATH` does win — the failure is a
*fall-through*, not a precedence fight: you get the right tree whenever
`sys.path` finds one, and the primary checkout when it does not.

So `python main.py` and `pytest` were always correct (both put the checkout on
`sys.path`), while `python scripts/seed.py`, `alembic`, `uvicorn` and
`pytest --import-mode=importlib` were not. The worst is
`alembic revision --autogenerate`, which emits a plausible migration diffed
against another checkout's models.

Three mechanisms cover it, and none is redundant:

- **Per-script anchors** — every `scripts/*.py` that imports `app` puts its own
  checkout on `sys.path` first. Correct *by default*.
  `tests/test_script_checkout_anchor.py` globs the directory and derives its
  exempt set as "does not import `app`", so a new script inherits the rule.
- **`alembic.ini`** — `prepend_sys_path = %(here)s`, not `.`: the value is spliced
  verbatim and resolved against the **cwd**. `path_separator = os` matters too,
  since the legacy fallback splits on spaces as well as commas.
- **`scripts/worktree/sitecustomize.py`** — for what we cannot edit. It is the
  only one that *removes* the finder, so an unfixable name raises instead of
  importing the wrong tree quietly. Opt-in via `PYTHONPATH`, and only the FIRST
  `sitecustomize` on the path is imported, so anything ahead of it shadows it.

For pytest specifically, prefer:

```
cd <worktree>/backend && PYTHONPATH=<primary>/backend/.venv/lib/python3.14/site-packages /usr/bin/python3 -m pytest tests/test_x.py -q
```

`PYTHONPATH` dirs get no `.pth` processing, so the finder is never installed at
all — a bad `sys.path` becomes a loud `ModuleNotFoundError` rather than a silent
wrong tree. See `docs/decisions.md` §124 and `frontend/tests-e2e/README.md`
§ Running from a worktree.

## Dependency lock (CI hash-pinning)

Local dev installs editable extras (`pip install -e ".[dev]"`) — unchanged.
**CI and the production image** install from hash-pinned locks (every
third-party artifact pinned by hash) to satisfy Scorecard's
Pinned-Dependencies supply-chain check. Two locks, both regenerated from
`pyproject.toml`:

| Lock | Scope | Consumed by |
|------|-------|-------------|
| `requirements-dev.lock` | base + `[dev]` extra + pip | `ci.yml` — `pip install --require-hashes …` then `pip install -e . --no-deps` |
| `requirements.lock` | base runtime only (no extras) | `backend/Dockerfile` — `uv pip install --system --no-cache --require-hashes …` (app runs from source, no editable install) |

Regenerate **both** whenever you change `pyproject.toml` dependencies (or
the pinned pip version in `requirements-dev.in`):

```bash
# from backend/ — uv resolves universally for the runtime Python (3.14)
uv pip compile pyproject.toml requirements-dev.in --extra dev \
  --universal --python-version 3.14 --generate-hashes -o requirements-dev.lock
uv pip compile pyproject.toml \
  --universal --python-version 3.14 --generate-hashes -o requirements.lock
```

`uv` not installed? `pipx run uv pip compile …` works ephemerally. Commit
the regenerated locks in the same change as the `pyproject.toml` edit.

**A stale lock does not fail the install** — that's the trap. The lock is
internally consistent, so `--require-hashes` succeeds and CI stays green
while the image installs whatever the lock says, ignoring the floor you
just raised in `pyproject.toml`. This is not hypothetical: five merged
Dependabot PRs (#111, #113, #114, #115, #117) raised `uvicorn`, `boto3`,
`pgvector`, `joserfc` and `ruff` in `pyproject.toml`, and the image kept
shipping every pre-bump version — including the security-motivated ones —
until it was caught.

Dependabot cannot close this itself. Its pip-compile support only pairs an
`.in` file with a lockfile ending in `.txt`; these locks are compiled from
`pyproject.toml` under a `.lock` name, so Dependabot updates the manifest
and never touches them. (`tools/fake-erp` uses the `.in`/`.txt` pair
precisely so Dependabot *can* maintain it there.)

### Automated sync on Dependabot PRs

`.github/workflows/dependabot-lockfile.yml` closes the gap on Dependabot PRs.
On a Dependabot PR touching `pyproject.toml` or `requirements-dev.in` it runs
**exactly the two `uv pip compile` commands above** (no `--upgrade`, so the
commit stays scoped to the bump), and if the output differs it commits
`requirements.lock` + `requirements-dev.lock` back onto the Dependabot branch.
An already-correct lock commits nothing. The same workflow does the pnpm half —
see [frontend/CLAUDE.md](../frontend/CLAUDE.md) § The lockfile.

**Keep the recipe above and the workflow's `Regenerate` step byte-for-byte in
step.** Two spellings of the same command is how they drift, and the recipe is
what a contributor falls back to.

Two things to know before trusting it:

- **The synced commit's CI is parked awaiting approval.** A `GITHUB_TOKEN` push
  cannot start a workflow run unattended (GitHub blocks that path to prevent
  loops — which is also what stops the workflow re-triggering itself). What it
  does instead is not "no run": the `synchronize` event still creates the CI /
  Security / gitleaks runs against the new head and parks them in
  `action_required`, so the PR keeps reading red until a maintainer approves
  them. Approve the parked runs to get a verified green — the PR checks tab's
  "Approve and run", or `gh api -X POST
  repos/<owner>/<repo>/actions/runs/<run_id>/approve` for each run
  `gh run list --branch <dependabot-branch>` shows as `action_required`. An
  empty commit or the PR's "Update branch" also works but is heavier. Do **not**
  `@dependabot rebase`: that force-pushes Dependabot's branch and drops the
  lockfile commit. (Observed on #379 / #381, 2026-09-07 — the first real bumps
  this workflow synced.)
- **It is a convenience layer, not the guard.**
  `tests/test_dependency_lock_sync.py` is still what makes drift loud, and the
  manual recipe is still the fallback. If the workflow fails, is skipped (a
  human pushed to the branch, so `github.actor` is no longer `dependabot[bot]`),
  or has not yet reached `main`, recompile by hand and commit — nothing about
  the failure mode changes.

The earlier version of this file was removed in #325 because its push job gated
on a `DEPENDABOT_LOCKFILE_PAT` Dependabot secret that no repo in the estate has
ever set, and the gate treated the missing PAT as skip-and-succeed — a
completely inert workflow that looked identical to a working one. The restored
version needs no credential at all: it triggers on `pull_request_target`, so the
one job that pushes can use the `GITHUB_TOKEN` with `contents: write`, and there
is no secret left to leave unset. The trade-off it buys back — never running a
resolver and a write token in the same job — is documented at the top of the
workflow.

Doing it by hand still lands cheapest in batches: six pip PRs (#334, #335,
#337, #339, #346, #347) sat red for a week on exactly this before being
regenerated in one pass. Prefer that — bump every open floor, recompile once,
and let Dependabot close the superseded PRs — over one lock commit per bump.

`tests/test_dependency_lock_sync.py` is the guard: it checks each declared
requirement against the version its lock pins, so a manifest bump without a
regenerated lock fails loudly. It compares constraints to pins — it never
re-resolves and never hits the network, so it can't go red just because a
new release appeared on PyPI overnight.

### `.dockerignore` — what does NOT enter the image

`backend/Dockerfile` ends in `COPY . .`, so `backend/.dockerignore` is the
only thing standing between your working tree and a shipped layer. It
excludes, in order of how much they matter:

1. **`.env` / `.env.*`** (except the committed `.env.development`) and
   `*.sops` — a gitignored local `.env` holds real credentials, and an
   image layer is readable by anyone who can pull it.
2. **`.venv`** — the local dev virtualenv. Copied in, it lands at
   `/app/.venv` as a second Python install on whatever versions that
   laptop had; Trivy then reports CVEs against packages the image never
   runs (this is exactly how a stale `pip` showed up in a scan).
3. Tool caches, `tests/`, `docs/`, egg-info — pure layer weight.

Excluding the venv and the uv download cache took the image from **1.26 GB
to 649 MB**. If you add something the running container genuinely needs,
check it isn't caught by a pattern there — `app/`, `alembic/`,
`alembic.ini`, `main.py`, `pyproject.toml`, `requirements.lock` and
`scripts/` are the deliberate keeps (`deploy/deploy.sh` and
`deploy/add-tenant.sh` run `scripts/*.py` inside this image).

### pip is not in the runtime image

The install layer ends with `python -m pip uninstall -y pip`. `python:3.14-slim`
ships pip as the **only** package in site-packages; this image installs with
`uv` and runs the app from the copied source, so nothing invokes pip and no
runtime path imports it. All it contributed was a `dist-info` for Trivy to match
CVEs against — and it did, reporting CVE-2026-13346 (pip < 26.2.0, "arbitrary
file installation via malicious package indexes") against an installer the image
never calls. Deleting it retires that alert and every future pip CVE with it,
instead of bumping the base digest each time one lands.

`uv` stays on the PATH, so `uv pip install --system <pkg>` is still how you add
something to a running container for debugging. `tests/test_container_supply_chain.py`
guards the removal.

## CI test sharding

The full suite (~10,100 tests against a real Postgres/Redis/MinIO) would run
~60 min as a single serial job — the longest job in CI. In `ci.yml` it's split
into:

- **`backend-lint`** — `ruff check` + `ruff format --check` only. No services,
  ~30s, fails fast on a formatting/lint miss.
- **`backend-test`** — a `strategy.matrix` of 8 shards, each on its own runner
  booting its OWN Postgres + Redis + MinIO and running a deterministic slice via
  [`pytest-split`](https://pypi.org/project/pytest-split/): `pytest --splits 8
  --group ${{ matrix.shard }}`. Each shard is a separate process + DB, which is
  why this is safe where in-process `pytest -n auto` is not — the suite's realdb
  fixtures hit event-loop-per-worker hazards under xdist. ~60 min ÷ 8 ≈ ~7 min/shard.

**Shard count tracks the suite's growth, and the 40-minute `timeout-minutes` is
not the dial.** A GitHub runner can degrade to ~3x slow without hanging — that
is what the cap's headroom is for — so the per-shard median has to stay near
~7 min for a bad runner to still finish. It was 4 shards until 2026-09-17, by
which point the suite had grown to ~15 min/shard and four runs in two days were
cancelled at 40 min on a slow runner, a different shard each time. When the
median creeps toward ~15 min again, double the shards; don't raise the cap.

`pytest-split` partitions by a committed `backend/.test_durations` baseline when
present; absent, it falls back to an even split by **test count** (still correct
and deterministic — every test runs in exactly one shard — just less
wall-clock-balanced). To regenerate the baseline for better balance (e.g. after a
large test-surface change), run the full suite once with the DB stack up:

```bash
# from backend/, stack up (docker compose up -d) and venv active
pytest --store-durations          # writes backend/.test_durations
```

Commit the updated `.test_durations` alongside the test changes. Bumping the
shard count means editing the `matrix.shard` list, the `--splits N` flag, and
both `name:` occurrences (the job's `shard N/8` and the test step's) together in
`ci.yml`.

**The baseline's decay is guarded, not trusted.** Nobody regenerates it as a
matter of course — it has one commit in its whole history — so
`scripts/check_test_durations.py` (`pnpm check:test-durations`, run in CI's
`Backend lint` job) fails when the fraction of collected tests carrying no
duration entry passes `MAX_MISSING_FRACTION`. A test the baseline has never seen
is split at the *mean*, so the partition stays exhaustive while quietly ceasing
to be weighted by anything real; the guard is what makes that visible before a
shard is reaped. It lives in the lint job because collection needs no services
(~15s). **That ceiling is a ratchet — only ever lower it.** Raising it to get
green is the one change the guard exists to prevent; the fix is
`pytest --store-durations`.

### Per-test timeout (pytest-timeout)

Before this, a genuinely hung test had **no** backstop below the shard-level
`timeout-minutes: 40` in `ci.yml` — GitHub reaps the runner and the log ends at
`Terminate orphan process`, with no traceback and no indication of which test
was running. `[tool.pytest.ini_options]` in `pyproject.toml` now sets:

- `timeout = 600` — a 10-minute per-test ceiling. This is a debugging aid, not
  a performance gate: it exists only to catch a test that will never finish,
  and must never fire on a slow-but-legitimate one. The arithmetic, from
  `backend/.test_durations`: the slowest recorded test is 14.65s
  (`test_rename_databases_script.py`); recorded durations understate real CI
  wall time by ~2.2x (see the `.test_durations` entry above), so ~32s is that
  test's realistic CI-equivalent worst case; 600s is ~18.6x that figure — well
  past the "well over 10x" floor a ceiling like this needs, even accounting for
  a degraded (~3x slow) runner.
- `timeout_method = "thread"` — a watchdog thread, not `SIGALRM`. `signal` only
  interrupts at a Python bytecode boundary and can't reach a stuck C-level
  wait (a blocked asyncpg/libpq call, a wedged socket) — exactly the shape a
  real hang takes in this `asyncio_mode = "auto"` suite over Postgres/Redis/
  MinIO. `thread` doesn't depend on interrupting whatever the main thread is
  blocked on: at the ceiling it dumps every thread's stack to stderr, then
  hard-kills the process. The whole run dies rather than just the one test —
  an acceptable trade, since CI's 40-minute shard cap was going to kill the
  process anyway; the difference is that the death now carries a traceback.
- `faulthandler_timeout = 540` — pytest's built-in `faulthandler` plugin (no
  extra dependency) dumps every thread's stack after this many seconds
  regardless of `timeout`. Set below `timeout` so that dump completes and
  flushes to stderr before pytest-timeout's own thread-mode dump-and-kill fires
  at 600s.

Proven locally: `@pytest.mark.timeout(1)` on a `time.sleep(5)` test, run from
`backend/` so `pyproject.toml`'s ini options apply, dumped the full stack
(showing the test blocked in `time.sleep`) and then hard-exited the process —
no normal pass/fail summary, matching the intended "diagnose, then die" shape.

## Test databases (the `realdb` harness)

Most of the suite is mock-based. Tests that request the `realdb` fixture
(`tests/conftest.py`) run against a live Postgres and a **pair of real tenant
databases** — the only way to prove cross-tenant isolation, the SQL filters, and
commit durability. Before each such test the harness truncates the tenant
business tables, restores the Default entity, resets the shared control-plane
`Organization` row, and reaps every other backend on those databases.

**The tenant pair is exclusive to one pytest process.** Because the reset both
TRUNCATEs and `pg_terminate_backend`s, two pytest runs sharing a pair delete each
other's rows and kill each other's connections — which surfaces as
`asyncpg.ConnectionDoesNotExistError` / `ConnectionResetError` in whatever
unrelated file happened to be mid-query, i.e. as flakiness rather than as the
collision it is.

Exclusivity is therefore claimed, not assumed. On first use each process takes a
**slot** — a Postgres session-level advisory lock — and uses the tenant pair
(and, see below, the control-plane database) named for it:

| Slot | Tenants | Control-plane DB |
|------|---------|-------------------|
| 0 (first / only process) | `feoh_pytesta`, `feoh_pytestb` | `feohledger_pytest` |
| 1, 2, … (each further concurrent process) | `feoh_pytesta1`, `feoh_pytestb1`, … | `feohledger_pytest1`, … |

**The harness's `Organization`/`User`/`Role` rows live in their OWN per-slot
control-plane database, never the real, shared `feohledger`** (`settings.database_url`)
— unlike the tenant pair, this is true for slot 0 too. `control_db_name_for_slot`
in `tests/conftest.py` derives the name from the configured control-plane DB
(`<base>_pytest<slot>`), and every control-plane operation the harness performs
— seeding roles/orgs/users in `_ensure_test_tenants`, `RealDB.control_sessionmaker()`,
the `get_control_db` override in `RealDB.client()` — targets it instead. This is
what closes a real defect (see `docs/known-issues.md`): a locally-running dev
backend's background sweeps (e.g. `extraction_reaper.run_reaper_loop`) enumerate
**every** `Organization` row in whatever database `settings.database_url` names,
with no filter. When the harness's test orgs lived there too, a dev server
sweeping the real control plane would discover and mutate them mid-test — stray
`TRUNCATE` stalls and invoices flipped to `failed` out from under a running
assertion. Giving every slot (0 included) its own control-plane database makes
the harness's tenants invisible to any server pointed at the default one. It
also removes the last cross-process contention: two concurrent slots no longer
share the control plane's unique constraints (org slugs, user emails) either.

Consequences worth knowing:

- **The common path pays a small, one-time-per-process cost, not a per-test
  one.** A lone `pytest` run — and every CI shard, each with its own fresh
  Postgres — still takes slot 0 and reuses the historical tenant names, but
  (like the tenant pair) it also provisions/self-heals its own
  `feohledger_pytest` the first time any test in the process requests `realdb`.
  Measured: roughly 2-3 extra seconds on that first call, not per test —
  negligible against a multi-thousand-test suite's total wall-clock.
- **A second concurrent run just works.** It provisions its own tenant pair
  *and* its own control-plane database on first use (one-off, per session) and
  cannot disturb the first. Run several agents or terminals at once without
  coordinating.
- **Crash-safe, nothing to clean up.** Postgres releases the lock when the
  holding connection dies, so a killed run frees its slot; the databases
  (tenant pair and control-plane DB alike) are reused by the next process to
  claim it.
- **Schema drift self-heals at session start** (issue #219) — for the tenant
  pair AND, the same way, for the per-slot control-plane database. Because
  they're long-lived and provisioned via `create_all(checkfirst=True)` — which
  never adds a column to an existing table — a database predating a later
  model change used to stay (or, absent this fix, would stay) silently stale
  until something read the missing column. The harness drops and recreates
  each pre-existing database's schema from the current `Base.metadata` once per
  pytest session (on the first `_ensure_test_tenants` call, keyed like the slot
  claim, via the shared `_rebuild_pytest_schema` helper), so a local slot can
  never lag the ORM. Fresh provisioning (CI shards, a new slot) skips the
  rebuild — it's already current by construction. Guarded by
  `test_realdb_harness.py`.
- **Never hardcode a test tenant's slug or a seeded login** — they carry the slot
  number. Use `realdb.info("a").slug` / `realdb.email("a", "admin")`.
- A control-plane row a test creates still needs a unique value per SESSION
  within its own slot's database (derive it from the slug or a uuid) — that
  database is long-lived across separate pytest invocations of the same slot,
  even though it's no longer shared with any other slot.

The harness resets tenant tables plus `Organization.settings` / `parent_org_id`,
and — **on teardown, not setup** — the control-plane billing rows: every
`Subscription` its own orgs hold, and every `Plan` that is not one of
`DEFAULT_PLAN_CATALOG`'s tiers (`_reset_control_billing`). Those two tables live
in the slot's control-plane database, which the per-test TRUNCATE never reaches
and `_rebuild_pytest_schema` clears only once per session, so a throwaway plan
stayed visible to every later test in the run — and `GET /api/billing/plans` is
a catalogue listing, so that is observable rather than inert. Six billing files
hand-rolled the same purge at *setup*, which by construction can only reap the
previous run's rows and always leaves the last one behind; one of them
(`test_public_api_keys`) wrote only the subscription half, so its `meter_test_*`
plans accumulated unchecked. **Seed a throwaway plan through
`RealDB.purge_plans(prefix, org_ids=…)`** — the single owner of that child graph
(the control-plane counterpart of `deleteVendorsWhere` / `deleteWorkflowsWhere`,
`docs/decisions.md` §127). Like those it takes a **prefix, not a predicate**, and
its seatbelt is derived from `DEFAULT_PLAN_CATALOG` so a free-form WHERE can't
empty the catalogue every entitlement lookup reads. Guarded by
`tests/test_realdb_harness.py`.

It does **not** delete extra control-plane `users` rows a test creates — those
accumulate, so a test must not assume a fixed user count for a test org.

**The literal default `settings.database_url` still gets its own baseline**
(`_ensure_default_control_schema`, once per process) — CONTROL_TABLES schema
plus the four seeded system roles, but never any Organization/User/UserRole
row. CI's backend-test job never runs Alembic migrations against `feohledger`
(the Postgres service just creates the empty database); before the per-slot
control DB existed, slot 0 *was* that literal connection string, so its
ordinary bootstrap incidentally satisfied this. `test_tenant_provisioning.py`
deliberately exercises the real `settings.database_url` production path
end-to-end (self-contained — it creates and cleans up its own org/user rows),
and this harness's own isolation regression test needs the `organizations`
table to exist so "no matching row" actually proves isolation instead of
erroring on a missing table. Locally this is a no-op against a real, migrated
`feohledger` (`create_all(checkfirst=True)` + `ON CONFLICT DO NOTHING`); it
only does real work against a genuinely fresh Postgres (CI, or a from-scratch
local instance).

`tests/test_realdb_harness.py` guards both properties.

## Date-sensitive tests — run them under a skewed timezone

The backend resolves "today" as `app/utils/dates.utc_today()`, never the
server's local date, and `tests/test_utc_today.py` AST-scans the converged
modules to keep it that way. **A test is not exempt from that rule.** When a
test anchors its fixtures on `date.today()` (local) and the code under test
compares against the UTC date, the two disagree for the whole window each day
where the local calendar date differs from UTC's — several hours daily anywhere
west of UTC, and the entire working day in Asia-Pacific.

CI runners are UTC, so the suite reads green there no matter how wrong the test
is. The failure surfaces only on a contributor's laptop, as an
unreproducible-looking date assertion. Don't wait for a clock — force it:

```bash
# UTC+14, so the LOCAL date is a day AHEAD of UTC. Deterministic, any hour.
TZ=Pacific/Kiritimati pytest -q
```

Run that whenever you add or touch a test that compares a date against
something the app computed. It turns the entire class of bug from an
intermittent flake into a hard failure. Five test modules were wrong this way
(past-due + future-date fraud flags, the W-9 received-date stamp on both the
`/tax` and `/portal` surfaces, aging-band boundaries, and the upcoming-payment
overdue inequality); `UTC_TODAY_TEST_MODULES` in `tests/test_utc_today.py` is
the allowlist that stops them regressing.

Reading the local date in a test is not automatically a bug: 31 other test
modules do it self-consistently — fixture and assertion from the same sample —
and are deliberately not on that allowlist. What matters is whether the value
gets compared against one the app derived.

## Project structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app, CORS, router includes, lifespan
│   ├── config.py            # Pydantic Settings (FEOH_ prefix env vars)
│   ├── database.py          # Control engine + per-tenant engine pool
│   ├── redis.py             # Redis connection + token blocklist
│   ├── tenant.py            # X-Tenant-Slug → tenant DB session
│   ├── api/                 # FastAPI routers (one per domain) + deps.py
│   │                        #   (deps.py: JWT auth, get_current_user, get_org_id)
│   ├── models/              # SQLAlchemy ORM models
│   ├── schemas/             # Pydantic request/response models
│   └── services/            # Business logic
│       ├── extraction_adapters/   # AI/OCR providers (pluggable)
│       ├── erp_adapters/          # ERP connectors (pluggable)
│       └── card_adapters/         # Virtual card providers (pluggable)
├── alembic/                 # Migration config + versions
├── scripts/                 # seed.py, create_tenant.py, delete_tenant.py, migrate_all_tenants.py
├── docker-compose.yml       # Postgres, Redis, MinIO
└── pyproject.toml           # Dependencies, ruff config, pytest config
```

## Database architecture

Two-database pattern: a shared **control plane** (`feohledger`) holding
organizations, users, roles and platform billing, and one **tenant DB per
organization** (`feoh_<slug>`) holding every business table. The full model
inventory — which table lives where, with notable columns — is
**`backend/docs/database.md` § Model inventory**. `CONTROL_TABLES` is the
enforced classification list; a new model that isn't classified fails a coverage
gate.

Two traps worth knowing up front: `ExtractionUsage` and `CardRebate` read like
control-plane billing data but are **tenant-local** (`docs/decisions.md` §57),
and business tables carry a nullable `entity_id` for multi-entity scoping.
**Connection management** (`database.py`):
- `get_control_db()` → AsyncSession for control plane
- `get_tenant_db()` → AsyncSession for tenant (via `X-Tenant-Slug` header)
- Engine pool: `pool_size=5, max_overflow=10` per tenant; `pool_size=10, max_overflow=20` for control
- All engines disposed on app shutdown

**Commit-before-response (durability).** Both providers call
`database.commit_before_response(session, request)`, which registers the
success-path commit on the exit stack FastAPI unwinds *before*
`await response(scope, receive, send)`. Their post-`yield` `commit()` is now a
conditional backstop (`if session.in_transaction()`), not the primary path —
FastAPI runs that teardown **after** the client already has its `201`, so
relying on it acknowledged writes that weren't durable yet. Consequences:

- **Don't "simplify" the providers back to a bare post-`yield` commit** — that
  silently reinstates the bug. `tests/test_commit_before_response.py` pins the
  ordering and drift-guards the FastAPI internal (`fastapi_function_astack`).
- **A test that overrides `get_control_db` / `get_tenant_db` must mirror this.**
  The `realdb.client()` overrides do; they exist to swap the *engine*, not the
  commit semantics. An override with the old body makes every test in that file
  exercise a code path production no longer uses.
- **Any new session provider needs the same call.** Rationale + the rejected
  alternatives: `../docs/decisions.md` §20.

## Invoice workflow state machine

```python
VALID_TRANSITIONS = {
    new:                {pending, ready_for_review, approved, done},
    pending:            {ready_for_review, approved, failed},
    ready_for_review:   {approved, rejected},
    approved:           {sending_to_erp, payment_scheduled, done},
    rejected:           {ready_for_review, new},
    sending_to_erp:     {sent_to_erp, failed},
    sent_to_erp:        {posted_in_erp, done},
    posted_in_erp:      {payment_scheduled, done},
    payment_scheduled:  {paid, approved},      # void → back to approved
    paid:               {done, approved},      # void → back to approved
    done:               {},                    # terminal
    failed:             {pending, sending_to_erp},
}
```

`payment_scheduled → approved` and `paid → approved` are back-edges
used by the void-payment path (`POST /api/payments/{id}/void`) to
re-enter the payment queue. Everything else is forward-only.

Step types: `extraction` → `approval` → `erp_export` → `done`

`workflow_engine.py` functions: `validate_transition()`, `transition_invoice()`, `get_invoice_for_update()` (SELECT...FOR UPDATE), `create_workflow_instance()`, `advance_workflow()`, `is_step_enabled()`.

**Snapshot pattern**: `WorkflowInstance.steps_config_snapshot` freezes the active definition at invoice creation. All runtime logic reads the snapshot, not the live definition.

**Notification hook**: `transition_invoice()` is also the single chokepoint for invoice-event notifications — after the audit write it calls `notification_dispatch.notify_event()` keyed off the resulting status (`approved`/`rejected`/`paid`). The `invoice_assigned` event is fired separately from `review.assign_reviewer`. All best-effort (never breaks the transition). **The outbound legs — every email and the chat post — run AFTER the caller's transaction commits**, via `services/post_commit.enqueue_post_commit` and SQLAlchemy's `after_commit`; only the in-app `Notification` rows ride the caller's commit, because those are DB writes. Before this the fan-out was awaited inside the still-open transaction, so a hung chat webhook held `payment_erp_sync`'s / `review.approve_invoice`'s `FOR UPDATE` row lock on a live invoice for its full 10-second timeout, and N recipients multiplied the email leg linearly. A transaction that rolls back now sends nothing. See `docs/notifications.md` § The outbound legs run POST-COMMIT.

## Key background services

Fourteen long-lived asyncio tasks start in `app/main.py`'s lifespan, each behind
its own `FEOH_*_ENABLED` gate. **What each one does, plus the shared loop runner,
health registry and locking rules: `backend/docs/background-sweeps.md`.**

Three rules govern all of them:

- **A sweep never moves money.** Sweeps flag, generate, alert, archive or sync —
  funding stays behind the CFO-gated payment run.
- **Batch size is a page, not a cap.** A sweep pages with a keyset cursor until
  each tenant is exhausted. A bare `LIMIT` re-serves the same lowest-id rows
  every tick and starves the tail whenever the sweep doesn't change the state it
  filters on.
- **Every tick runs through `services/sweep_health.py`**, so a dead or failing
  sweep is visible at `GET /api/health/sweeps` instead of silent.

## Adapter patterns

**Full per-family reference — providers, real vs fail-closed skeleton, the
per-org settings key that selects each, and the registry decorator:
`backend/docs/adapter-patterns.md`. Read it before adding or changing an
adapter.**

Families under `backend/app/services/`:

| Family | Directory |
|--------|-----------|
| Extraction | `extraction_adapters/` |
| ERP | `erp_adapters/` |
| Cards | `card_adapters/` |
| Payments | `payment_adapters/` |
| Positive Pay formatters | `positive_pay_adapters/` |
| FX rates | `fx_adapters/` |
| Supplier financing | `financing_adapters/` |
| Sanctions / KYC | `sanctions_adapters/` |
| Vendor enrichment | `enrichment_adapters/` |
| Audit shipping | `audit_shipping/` |
| TIN validation | `tin_validation_adapters/` |
| 1099 e-filing | `tax_filing_adapters/` |
| Exception-agent resolvers | `exception_agents/` |
| PEPPOL | `peppol_adapters/` |
| Punch-out | `punchout_adapters/` |
| Billing | `billing_adapters/` |
| Email (outbound) | `email_adapters/` |
| Email intake (inbound) | `email_intake_adapters/` |
| Chat notifications | `chat_notification_adapters/` |
| Embeddings | `embedding_adapters/` |
| QMS / quality inspections | `qms_adapters/` |

Three rules hold across every family:

- **`mock` exists and is the default** (guard rail 7). A new integration ships
  its mock and a safe local default in the *same* change; `pnpm dev` must never
  need a real SaaS credential.
- **A named provider with no registered adapter fails closed** — it must never
  silently resolve to `mock`, which would turn "unconfigured" into a clean pass.
  An empty config still resolves `mock`; a *wrong name* raises.
- **A real provider with no credential fails closed too** — no hardcoded secret
  fallback (see the root `CLAUDE.md` § Project invariants).

To add one: copy `mock_adapter.py`, implement the interface, register with the
family's decorator.
## Webhook security (`services/webhook_security.py`)

Every inbound webhook handler — payments, cards, ERP, email-intake, PEPPOL inbound — verifies the provider's HMAC over the raw request body and dedupes (by event id, or — for PEPPOL inbound — by the AS4 MessageId at the DB layer) before mutating state (project invariant #9). Shared helpers:

- `verify_hmac_sha256(secret, raw_body, provided_hex)` — constant-time HMAC-SHA256 check via `hmac.compare_digest`. Empty / missing secret or signature fail closed.
- `is_event_already_processed(provider, event_id, ttl_seconds=86400)` — Redis `SET NX EX` dedup. First delivery returns `False`; replays within the TTL window return `True` so the handler short-circuits.
- `extract_signature_header(headers, *candidates)` — case-insensitive multi-candidate header lookup (different providers use different header names).

Per-tenant secrets:

| Endpoint | Settings path |
|---|---|
| `/api/payments/webhook/...` | `Organization.settings.payments.webhook_secret` (verified inside the adapter's `parse_webhook`). The route rejects `provider == "mock"` outright before any tenant lookup — the `mock` adapter's `parse_webhook` does no signature verification and `mock` is the default provider for un-configured tenants, so serving it publicly would accept forged status transitions (mock never delivers real webhooks). Mirrors `cards.card_webhook`'s `lithic`/`nium` allowlist and the billing route's boot-time mock refusal. |
| `/api/cards/webhook/{provider}` | `Organization.settings.cards.webhook_signing_secret` |
| `/api/erp/webhook/{erp_type}` | `Organization.settings.erp.webhook_signing_secret` |
| `/api/email-intake/inbound/{provider}` | `FEOH_EMAIL_INTAKE_SIGNING_SECRET` (process-level HMAC key; verified in `email_intake.verify_signature`). Dedupe is `is_event_already_processed("email_intake", message_id)`, claimed right after tenant resolution and released via `release_event_claim` if invoice creation fails downstream (mirrors `api/cards.py`'s claim/release discipline) so a redelivery can retry. Recipient-token match uses `hmac.compare_digest`. |
| `/api/peppol/inbound/{tenant_slug}` | `FEOH_PEPPOL_INBOUND_SIGNING_SECRET` (process-level HMAC key; verified by `peppol_receive.verify_inbound_signature`). Dedupe is the DB `uq_peppol_message_id` index, not Redis. |
| `/api/catalogs/punchout/return/{tenant_slug}` | `FEOH_PUNCHOUT_RETURN_SIGNING_SECRET` (process-level HMAC key; verified in `catalogs._verify_return_signature`). Correlation is the BuyerCookie matched to a pending `PunchoutSession`. |
| `/api/approvals/slack/interactivity` | `FEOH_SLACK_SIGNING_SECRET` (process-level HMAC key; verified in `slack_approvals._verify_slack_signature` over `v0:{X-Slack-Request-Timestamp}:{raw_body}`, with a `±FEOH_SLACK_REQUEST_MAX_AGE_SECONDS` replay window). Per-action dedupe is the single-use action-token `jti` in Redis (the email-approval mechanism), not `is_event_already_processed`. Returns an opaque 200 ack (Slack-friendly) on every path, not 204. See `docs/slack-approval.md`. |

Every webhook handler returns **204 silently** on every rejection path (bad signature, unknown tenant, missing event id, unknown card / invoice / payment, disabled master switch, unparseable / malformed inbound document) — except the Slack interactivity webhook, which returns an opaque **200 ack** on every path (Slack requires 2xx to acknowledge a button click; the ack text is identical across success and rejection, so it doesn't enumerate), and the email-intake webhook, which is a hybrid: pre-signature rejections (unknown provider / bad signature / unparseable body — nothing sensitive resolved yet) still return 204, but every *decision* **after** the signature verifies (unknown/disabled intake token, duplicate delivery, no usable attachments, or genuine success) returns the SAME opaque 200 ack (`email_intake._ack()`) — because unlike the other handlers, a 204-vs-200-with-a-body split there would itself be the token-enumeration oracle (the platform-wide signing secret is shared across all tenants, so anyone who can sign a request can watch for the response to change once they guess a valid intake token). Distinct 4xx responses / differing response bodies would enumerate tenant slugs or card/intake tokens.

**A *decision* is not the same as OUR OWN failure, and only the decision is acked.** A decision is a final answer about this message; a failure of ours (S3 unreachable, tenant DB down, Redis flapping, a concurrent transition racing the state-machine guard) is not an answer at all — the message is still unprocessed work. `email_intake` and `erp_webhook` both release their Redis dedup claim on those paths precisely so the provider's retry can reprocess, but both used to then ack success, so the retry never came: the release was preparing for something that could not happen, and the vendor's invoice / the ERP status update was dropped permanently behind a log line. Both now return a **bodyless 5xx** (`_retry_please()` in each module → `503`) on their own failures only, matching `api/billing_webhook.py`, which already re-raised to a 5xx for the same reason. This narrows the intake-token oracle to "while the platform is already broken" — a real but far smaller exposure than losing invoices on every blip — and the bodyless response still carries no detail, no stack trace, no tenant. Do NOT extend the 5xx to decision paths: an ERP retrying forever on an event we have correctly and permanently refused is the mirror-image failure.

Tests: `backend/tests/test_webhook_security.py`, `tests/test_payment_webhook_security.py`, `tests/test_peppol_inbound.py`, `tests/test_slack_approvals.py`, `tests/test_email_intake.py`, `tests/test_email_intake_processing.py`, `tests/test_erp_webhook_retry_on_our_failure.py`.

## Security utilities

- **Passwords**: a single shared `pwd_context` in `app/utils/passwords.py` **implements** `bcrypt_sha256` (HMAC-SHA256 pre-hash → bcrypt) to side-step bcrypt's 72-byte truncation — directly against the `bcrypt` package, because passlib (last released 2020) cannot import against bcrypt 4.1+ and froze us on 4.0.1 (`../docs/decisions.md` §151). Every call site (auth, admin, portal, vendors, tenant_provisioning, scripts/seed) imports from there — never construct a second hash context and never `import bcrypt` anywhere else; `tests/test_password_hashing_offloaded.py` AST-scans `app/` for both. Stored `$bcrypt-sha256$` (v2 + legacy v1) and pre-upgrade plain `$2b$` hashes all still verify, pinned byte-for-byte against passlib-generated fixtures in `tests/test_bcrypt_sha256_compat.py`. Complexity rules in `validate_password_complexity` (min 12 chars, upper/lower/digit).
- **Filename sanitiser**: `app/services/storage.py::_safe_filename` strips path separators, `..`, leading dots (no dotfiles), and control / non-printable characters. Used by `upload_invoice_file` before interpolating the filename into the S3 key. Without it, a vendor portal POST with filename `../../other-org/secret.pdf` could land under another tenant's prefix.
- **File download cross-tenant check**: `GET /api/invoices/file/{file_key:path}` verifies the key's first segment equals the requesting user's `organization_id`. Same 404 for wrong-org and missing-file so the response doesn't enumerate prefixes.
- **CSV formula-injection defense (CWE-1236)**: `app/services/report_export.py::csv_safe_cell` prefixes a leading `= + - @` / tab / CR (except a bare signed number) with a single quote so a spreadsheet renders the cell as text, not a formula. Attacker-controlled fields (AI-extracted `vendor_name`, `User.full_name`) reach CSVs a CFO/admin opens in Excel. Use `safe_csv_writer(buf)` (the sanitizing `csv.writer` stand-in) at **every** export writerow site; for `csv.DictWriter` sites sanitize the row values through `csv_safe_cell`. Covers the analytics/report exporters, audit export, ad-hoc report builder, invoice bulk export, and workflow export.

## Audit immutability + access auditing (SOX)

The `audit_log` table is **append-only at the database level**: migration `0022_sox_audit_immutable` installs `BEFORE` triggers (DDL in `app/services/audit_immutability.py`) that reject every DELETE and every UPDATE touching a column other than `shipped_at`. The `shipped_at` carve-out lets `audit_log_shipper.py` stamp shipped rows; everything else is frozen, so a rogue ORM call or a direct `psql` session can't tamper with the trail. Installed on every tenant DB — migration fan-out for existing tenants, `tenant_provisioning._create_tenant_tables` for fresh ones (which use `create_all`, not Alembic). See `docs/audit-log-shipping.md`.

Two request-path helpers in `app/services/audit_access.py` (thin wrappers over `dispatch_audit`, not reimplementations):
- `log_access(...)` — writes a `<entity_type>.viewed` row for SOX access-control auditing. Instrumented reads: vendor detail (`vendor.viewed`), payment detail (`payment.viewed`), card PAN reveal (`card.details_viewed`), the audit-trail view (`audit.viewed`), and every auditor export (`audit.exported`). The `details` payload records the field-**names** accessed, never the values — no tax id / bank number / PAN ever enters the audit trail (PII-out-of-logs).
- `build_field_diff(before, after, fields)` — produces `{field: {old, new}}` for SOX change history on invoice edits + approve-with-corrections. Money serialises as **string-Decimal**, never float.

The auditor-export surface is `app/api/audit.py` (`/api/audit/export`, `/api/audit/invoice/{id}` — GET-only, admin/CFO). `/api/audit/export` also serves a formatted **PDF** SOX audit-trail report via `?format=pdf` (cover + event-count summary + chronological table; `app/services/audit_report_pdf.py`, pure-function modelled on `remittance_pdf.py`; renders only the field-NAME-sanitised entries). See `docs/api-reference.md` § Audit Trail.

**Periodic access reviews (SOX)** — `app/api/access_reviews.py` (`GET /api/access-reviews` + `POST /api/access-reviews/acknowledge`, admin/CFO). Compute-on-read (no migration): `app/services/access_review.py` flags users holding an elevated role (`admin`/`ap_manager`/`cfo`) whose last *mutating* audit action is older than `FEOH_ACCESS_REVIEW_DORMANT_DAYS` (default 90), or who never acted, as DORMANT. The review list is itself a sensitive read (`access_review.viewed`); acknowledge writes `access_review.completed` + stamps `Organization.settings.access_review`. See `docs/access-reviews.md`.

**Digital signatures on approvals (non-repudiation):** every `invoice.approved` audit row carries an HMAC-SHA256 "timestamp + user hash" in `details.signature` over the canonical approval facts (invoice id + exact Decimal amount + actor + decision + timestamp). Signed in `services/review.approve_invoice` (`services/approval_signature.py` — pure); re-verifiable at `GET /api/audit/invoice/{id}/verify-signatures` (admin/CFO), where a post-approval tamper of the amount/actor/timestamp → `valid: false`. Key `FEOH_APPROVAL_SIGNING_KEY` — empty → signing skipped, NON-secret committed dev value, real key via sops (no hardcoded fallback). See `docs/approval-signatures.md`.

**Retention policies (records management):** per-record-class windows on `Organization.settings.retention` (`GET/PUT /api/retention-policy`, admin); the `retention_sweep` background loop archives overdue terminal invoices via a `meta.archived_at` marker and, for the WORM `audit_log` class, verifies shipment instead of deleting — it never deletes audit rows (composes with the immutability trigger). See `docs/retention.md`.

## Dispatch modes

Extraction, ERP push, and audit logging support two execution modes:
- **local** (default) — in-process. See the loop rule below for *how*.
- **lambda** — sends message to SQS, processed by Lambda handler

Files: `*_dispatch.py` (router), `*_lambda.py` (Lambda handler).

**In `lambda` mode the SQS send is offloaded, not inlined.** boto3 is
synchronous: building the client resolves the credential chain (which can reach
IMDS) and `send_message` is a full HTTPS round trip. All three `_send_to_sqs`
helpers are reached from an `async def` — `dispatch_extraction` from the invoice
upload route AND the public email-intake webhook, `dispatch_erp` from the ERP
send path, `dispatch_audit` / `dispatch_auth_audit` from every audited mutation
including **every login attempt** — so each call site hands it to
`asyncio.to_thread`, the same arrangement `services/storage` and the
audit-shipping adapters use. `tests/test_sqs_dispatch_nonblocking.py` is the
drift guard (a thread-identity assertion per entry point plus an AST scan that
fails on a bare `_send_to_sqs(...)` inside any coroutine in those modules).

### The event-loop rule (read before adding a dispatcher)

`app/database.py`'s `control_engine` and `_tenant_engines` belong to the event
loop that first drives them — in a running app, uvicorn's. **An asyncpg
connection cannot cross event loops**, and the failure is not contained: it
raises `RuntimeError: got Future attached to a different loop` *and* can return
the half-used connection to the pool the **request path** draws from, after
which unrelated endpoints hang on it. That is exactly how a broken
`payment_erp_sync` presented — as `PATCH /api/organization` timing out, in e2e
specs that had already passed their own assertions.

What makes this easy to reintroduce: a dispatcher creating its own engines
(they all did) is **not sufficient**. `transition_invoice` fires notification,
audit and webhook hooks, and each opens its *own* control-plane session — and
`dispatch_audit` its own tenant engine — by reaching for the module global.
That is code a dispatcher never calls directly and cannot hand a session to.

So there are exactly two correct shapes:

| Shape | When | Who |
|---|---|---|
| `asyncio.create_task` on the caller's loop | the work is `await`-only I/O | `erp_dispatch`, `payment_erp_sync` |
| worker thread + own loop, wrapped in `database.dispatch_engine_scope(...)` | the work genuinely blocks | `extraction_dispatch` |

`extraction_dispatch` keeps its 3-worker pool because extraction runs PyMuPDF
rendering and Tesseract OSD — synchronous CPU work that would stall the request
loop — and because the pool doubles as the concurrency limiter that keeps bulk
uploads under the AI providers' rate limits. It declares its loop-local engines
once via `dispatch_engine_scope`; every `control_session_factory()` /
`get_tenant_engine()` underneath then resolves to those instead of the globals,
with no per-call-site change. Engines the scope creates itself are disposed on
exit; ones passed in are the caller's to dispose.

`control_session_factory` is therefore a **function**, not the
`async_sessionmaker` it used to be — every call site already spelled it
`control_session_factory()`, so nothing changed for callers. Code that needs to
rebind the underlying sessionmaker (the pytest harness's
`.configure(bind=...)`) must target `_default_control_session_factory`.

Fire-and-forget tasks are held in a module-level set: `asyncio` keeps only a
weak reference to a running task, so one with no other referent can be
collected mid-await and vanish silently.

Guarded by `tests/test_dispatch_engine_scope.py` (the indirection reaches
`notification_dispatch` / `audit_dispatch` without either being edited, and a
worker's scope never leaks into the request context) plus the loop-identity
tests in `tests/test_erp_dispatch.py` and `tests/test_payment_erp_sync.py`.

## Authentication (`api/deps.py`)

- JWT HS256 signed with `FEOH_SECRET_KEY`, 30-min expiry (configurable)
- Token payload: `sub` (user_id), `org` (org_id), `jti` (unique ID for blocklist)
- `get_current_user()` — FastAPI dependency, returns User or 401
- Logout adds `jti` to Redis blocklist with TTL matching token expiry
- `get_current_user` also stashes the requesting `jti` on `user.session_jti` (transient, like `effective_permissions`) — that's what lets the session routes mark the caller's own entry and spare it from "sign out everywhere else"

### Sessions (`services/session_management.py`, `app/redis.py`)

Each sign-in registers its JTI in `active_jtis:<user_id>` (sorted set, scored by issue time) plus a companion metadata hash `session_meta:<user_id>` (IP, coarse device label from the pure `describe_user_agent`, sign-in method). The two are always mutated together, so a session's metadata can never outlive its membership. Beyond the concurrent cap (`FEOH_MAX_CONCURRENT_SESSIONS`) and the forced logout on role change / password reset / deactivation, this backs:

- `GET /api/auth/sessions`, `DELETE /api/auth/sessions/{jti}`, `POST /api/auth/sessions/revoke-others` — the account holder's own remedy for a leaked token. Every op is keyed on `user.id`, so membership in the caller's set IS the authorization; a foreign JTI is the same opaque 404 as an unknown one. No step-up (they only remove access). Audited `auth.session.revoked`, PII-free.
- `POST /api/admin/users/{id}/revoke-sessions` — standalone admin force-logout (`user.manage`, org-scoped, idempotent, audited `user.sessions_revoked`).

`list_sessions` prunes entries whose token already expired (the set's TTL is refreshed on every login, so it can outlive the tokens inside it) without blocklisting them — there is nothing left to revoke. The **raw** User-Agent is never stored or logged. See `../docs/authentication.md` § Session management.

### RBAC (`require_roles`)

Every authenticated endpoint declares the roles it accepts:

```python
from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, require_roles

user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER))
```

- Any-of semantics: user passes if they hold at least one listed role.
- 403 with `{"detail": "Your role does not permit this action."}` on miss.
- Denials log at WARN level for monitoring.
- New endpoints without an auth dependency fail `tests/test_rbac.py` — coverage gate.
- Routes with no JWT dependency live in one of two allowlists in the same test file:
  `PUBLIC_BY_DESIGN` (genuinely credential-free — health, public-config, `/portal/branding`,
  SSO/SAML config, signup start; each is DRIVEN with a real credential-free request) or
  `ALTERNATE_AUTH` (gated by something other than the JWT — login, MFA challenge, webhooks,
  SCIM bearer, signed action tokens; each names its gate). Listing a route no longer exempts it.
- Full permission matrix: `../docs/authentication.md` § RBAC.

### MFA (`services/mfa.py`, `services/webauthn.py`)

- TOTP (pyotp) + email-OTP backup + **WebAuthn/passkeys** (`py_webauthn`). Master switch `FEOH_MFA_ENABLED` (default `false` for local dev) gates all three.
- Per-user TOTP secret on `User.mfa_secret`; org-wide enforcement via `Organization.settings.mfa.required`.
- **Enrollment is two-phase and never disturbs a live factor.** `POST /api/auth/mfa/enroll` (and the portal twin) mints a *candidate* secret into Redis (`mfa:pending_enroll:<user_id>` / `mfa:vendor_pending_enroll:<id>`, `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS`, default 900s) — `mfa_secret` / `mfa_enabled` / `mfa_enrolled_at` are written ONLY by `/mfa/enroll/verify`. **Changing an existing factor is a step-up**: enroll-start, `POST /api/auth/mfa/passkey/register`, `DELETE /api/auth/mfa/passkey/{id}` and `POST /api/auth/mfa/disable` take an optional `{password?, code?, assertion?}` body and require one to check out whenever a factor is already live — the shared `pwd_context` password or a code from the CURRENT authenticator (both via the pure `mfa.step_up_verified`), or a **WebAuthn assertion** from an already-registered passkey (`api/auth._step_up_satisfied`, which needs the DB + Redis so it can't live in the pure helper). A "live factor" is an enabled TOTP secret OR any registered passkey, so both doors are gated symmetrically; a genuinely first factor needs none. The assertion is the proof an **SSO-only** account uses — with no password and no TOTP it would otherwise be locked out of its own factor management (it is never *exempted*; exempting would let a stolen JWT plant an attacker-controlled passkey). Its challenge comes from `POST /api/auth/mfa/step-up/passkey {operation}` and is **purpose- and operation-bound**: login and step-up challenges live in different single-use Redis slots (`webauthn:auth_challenge:<uid>` vs `webauthn:stepup_challenge:<operation>:<uid>`), so a step-up assertion can't mint an access token, a login assertion can't authorize a factor change, and a `passkey_register` assertion can't authorize a `passkey_delete` — the only thing separating the two ceremonies is the challenge, since `clientDataJSON.type` is `webauthn.get` for both. `purpose` is a required kwarg on `begin_authentication`/`finish_authentication`. The supplier portal has no passkeys (`WebAuthnCredential` → control-plane `users.id`, `VendorUser` is tenant-scoped), so `/portal/auth/mfa/*` stays password-or-code. Every step-up is throttled 5/min **per account** (`_throttle_step_up`, not per-IP — the attacker holds the token) and writes a PII-free `auth.mfa.step_up.failure` / `portal.mfa.step_up.failure` audit row on failure; `/mfa/disable` on both surfaces rides the same helpers. Without all of this, a leaked access token alone could strip or swap the second factor, silently and unthrottled.
- **Passkeys are a separate code path** (`services/webauthn.py`), additive + opt-in. Credentials live in the control-plane `webauthn_credentials` table (`WebAuthnCredential`, migration 0063, in `CONTROL_TABLES`) — one row per registered authenticator, keyed by `user_id`. Register/list/delete + authenticate endpoints under `/api/auth/mfa/passkey/*`, plus the step-up-challenge endpoint `POST /api/auth/mfa/step-up/passkey`; the authenticate ceremony is gated by the login-issued MFA challenge token (public, pre-access-token), register/list/delete and step-up-start require JWT. The per-ceremony challenge is stashed single-use in Redis (`webauthn:reg_challenge:<user_id>`, `webauthn:auth_challenge:<user_id>`, `webauthn:stepup_challenge:<operation>:<user_id>`); the signature counter is verified + bumped (clone-detection). RP ID / origins configurable (`FEOH_WEBAUTHN_RP_ID` / `FEOH_WEBAUTHN_ORIGINS`; dev defaults `localhost` / `http://localhost:7777`). Public key + counter are not secret in the password sense and never logged.
- Login returns either `TokenResponse` or `MFAChallengeResponse`. Challenge token is a short-lived JWT with `typ: mfa_challenge` — verified at `POST /api/auth/mfa/verify` (totp/email) or the passkey authenticate endpoints. `methods` lists the offered factors (`totp` / `passkey` / `email`); a passkey-only user trips the gate.
- Email-OTP hashes live in Redis (`mfa:email_otp:<user_id>`), short TTL, single-use.
- SSO sign-in skips our MFA challenge — IdPs handle their own MFA.
- Full reference: `../docs/authentication.md` § MFA + § Passkeys.

### SSO — OIDC + SAML (`api/auth_sso.py`, `api/auth_saml.py`, `services/sso.py`)

- Per-tenant config in `Organization.settings.sso`, discriminated by `protocol`
  (absent / `"oidc"` → OIDC; `"saml"` → SAML). `resolve_sso_config` /
  `resolve_saml_config` each return `None` for the other protocol.
- **Whether password sign-in is closed is `services/sso.is_sso_only`, and only
  that.** It needs `enabled` + `sso_only` + an IdP block that resolves; login,
  the step-up, `/auth/me` and the public config echo all call it. Never test the
  two flags yourself (`sso_only_requested` is the *request*, for validation and
  logging). Because it runs on every password sign-in, the resolvers must raise
  `SSOConfigError` and nothing else for any malformed block, and stay local (no
  DNS, no discovery fetch). `docs/decisions.md` §204.
- Both protocols share the identity tail in `services/identity_provisioning.py`
  (`jit_provision` + `extract_and_check_email`) and the session-mint tail — only
  IdP-response *verification* differs.
- SAML verification (`auth_saml.py`) is `python3-saml` pinned to a hardened
  posture: `wantAssertionsSigned`, SHA-256-only, issuer/audience/destination +
  mandatory InResponseTo, per-tenant replay dedup, IdP cert pinned (no
  fingerprint/embedded), XXE-hardened parsing. SP signing keypair (optional) →
  `FEOH_SAML_SP_*` via sops. Local IdP: Keycloak (`pnpm saml:seed`).
- Full reference: `../docs/authentication.md` § SAML SSO + `../docs/local-sso-saml.md`.

## Organization settings (JSONB)

Stored in `Organization.settings`:
```json
{
  "company": { "name", "tax_id", "address", "phone", "website", "logo_url",
               "vat_registration_number", "companies_house_number" },
  "invoice_defaults": { "currency", "payment_terms", "number_prefix", "default_gl_account", "default_cost_center" },
  "erp": { "type", "integration_method", "credentials": { ... }, "webhook_signing_secret": "..." },
  "extraction": { "program_type": "platform"|"byok", "provider", "api_key", "model" },
  "cards": { "enabled": true|false, "program_type": "platform"|"byok", "provider", "region": "US"|"EU"|...,
             "default_expiry_days": 30, "webhook_signing_secret": "...", ... },
  "payments": { "provider", "credentials": { ... }, "webhook_secret": "...", "cfo_approval_above": Decimal,
                "require_run_segregation": true },
  "mfa": { "required": true|false },
  "chat_notifications": { "enabled": true|false, "provider": "slack"|"teams"|"mock",
                          "webhook_url": "...", "events": { "invoice_approved": true, ... } },
  "sso": { ... },
  "fraud_rules": { ... },
  "exception_agents": { "autonomy_level": "conservative"|"balanced"|"aggressive", "amount_tolerance_pct": 2.5 }
}
```

The three `webhook_*_secret` fields are HMAC keys used by the inbound webhook handlers — see "Webhook security" above.

## Exception types

Canonical roster: `services/exception_lifecycle.EXCEPTION_TYPES` — `duplicate`, `po_mismatch`, `fraud_flag`, `extraction_failed`, `unverified_vendor`, `review_rejected`, `amount_exceeded` (legacy — no longer raised, kept for historical rows), `missing_data`, `quality_hold`, `price_variance`, `contract_noncompliant`, `erp_reconciliation`, `line_total_mismatch`, `payment_compliance_hold`. The `exception_type` column is a plain `String(50)`, so that tuple — not a DB enum — is the source of truth; `api/exceptions.EXCEPTION_TYPE_LABELS` must cover it exactly, and `tests/test_exception_type_labels.py` AST-scans `app/` to fail when a type is raised without joining the roster and getting a label.

**Every lifecycle event is audited, and `record_decision` also enforces segregation of duties.** `services/exception_lifecycle` is the single chokepoint: `create_exception` writes `exception.raised`, and the human queue (`api/exceptions`, single + bulk), the agent coordinator, the compliance-hold release/dismiss path (`api/payments.py::_resolve_compliance_hold_exception`) and the supplier-resubmission path (`api/portal.py`) all resolve/escalate/dismiss through `record_decision`, which writes `exception.resolved` / `.escalated` / `.dismissed` (+ `exception.assigned` on routing). It is also where the queue's SoD refusal lives (`segregation_refusal` — see the root `CLAUDE.md`), precisely *because* there are that many doors: the two non-queue ones early-return (neither type is payment-blocking, and the portal's actor is a `VendorUser` with no control-plane id), so a refusal can never strand a supplier resubmission or desynchronise the queue from a released hold. Rows are correlation-keyed to the **invoice**, so they land on its SOX trail; an invoice-less exception self-correlates on its own id. `details` is PII-lean and carries a `payment_blocking` flag derived from `api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES` itself — clearing a `duplicate` / `fraud_flag` / `line_total_mismatch` is the human sign-off that lets a payment run proceed, and the mutable `exceptions` row (single-valued, not WORM-shipped, no append-only trigger) can't be that record. Escalation records the note but never stamps `resolved_by` / `resolved_at`. Every `/api/exceptions` mutation is entity-scoped like the reads. See `docs/exception-lifecycle.md`.

Severity: `error`, `warning`, `info`. Auto-detected by `invoice_warnings.py`. `erp_reconciliation` is opened by the ERP webhook (`api/erp_webhook.py`) in two cases, both idempotent per open exception and PII-free: (1) the ERP reports an invoice VOIDED/CANCELLED that we already advanced past the point where `→ failed` is a legal transition (`sent_to_erp` / `posted_in_erp` / `payment_scheduled` / `paid`) — money may be in flight, so it is flagged for human reconciliation instead of auto-transitioned; (2) the ERP reports `Paid` for a `payment_scheduled` invoice whose `Payment.settled_amount` shows the rail settled SHORT of what AP authorized — the same `settlement_coverage` hold `payment_erp_sync` applies, so a validly-signed ERP `paid` can't walk a short-paid invoice to `paid` and strand `/settlement/accept`. It is also opened by `payment_erp_sync` on a failed sync leg. `payment_compliance_hold` is opened by `api/payments.py` whenever `_execute_single_payment` parks a payment at `pending_compliance` (no screenable vendor, or the sanctions/KYC adapter itself returns a `hold` verdict) — dedup'd per `(invoice_id, "payment_compliance_hold", "open")` so a retried execution never double-opens it, and resolved by `POST /api/payments/{id}/compliance/release` or `/dismiss`. See `backend/docs/payments.md` § Sanctions / compliance hold resolution.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/seed.py` | Creates 2 tenants (acme, techflow) with full sample data (vendors, invoices, POs, payments, exceptions) + a `WorkflowInstance`/`WorkflowStep` per invoice (so the approval queue + assistant pending-approvals tool aren't empty — `ready_for_review` invoices get an active approval step assigned to the org admin) + calls `seed_extras` so contracts / credit memos / discount offers / expenses are populated too |
| `scripts/seed_extras.py` | Additive, idempotent per-tenant seed for the contract (`/contracts`), credit-memo (`/credit-memos`), discounting (`/discounts`) and expense (`/expenses`) pages. `seed_extras(session, org_id)` is reused in-line by `seed_tenant`; the CLI (`--tenant feoh_acme`) tops up an already-seeded tenant without a wipe. Skips if the tenant already has contracts. |
| `scripts/seed_payable_invoices.py` | Tops up a tenant's payment queue with N approved invoices (`--tenant`, `--count`) — re-run after executing a payment run drains the queue. |
| `scripts/create_tenant.py` | CLI wrapper around `services.tenant_provisioning.provision_tenant` — provisions a single tenant (org + admin user + DB + tables) |
| `scripts/delete_tenant.py` | CLI wrapper around `services.tenant_deletion.delete_tenant` — deletes one tenant completely (object-storage prefix, tenant DB, control-plane rows), in that order. `--dry-run` inventories; otherwise it needs `--confirm <slug>` or `--yes`. Driven by `deploy/remove-tenant.sh`, which adds the Caddy and backup legs. The deletion `/legal/dpa` § 13 promises. |
| `scripts/migrate_all_tenants.py` | Runs `alembic upgrade head` on every tenant DB |

## Self-service tenant signup

Two-step flow under `/api/signup`:

| Endpoint | Purpose |
|----------|---------|
| `POST /api/signup/start` | Rate-limit check, captcha verify, slug-format check, slug-availability check. Creates an `EmailVerification` row (24h TTL) and sends a verification email. No tenant resources created yet. |
| `GET /api/signup/slug-check?slug=…` | Cheap inline availability check for the signup form. |
| `POST /api/signup/complete` | Consumes the token, re-checks slug availability, provisions the tenant via `services.tenant_provisioning.provision_tenant`, generates a temp password, sends the welcome email with tenant URL + credentials, marks the verification consumed. |
| `POST /api/auth/change-password` | Authenticated. Validates current password, enforces complexity, sets the new hash and clears `User.must_change_password`. |

The welcome email contains the tenant URL (`FEOH_TENANT_URL_TEMPLATE`, e.g. `https://{slug}.app.com`) and a 16-char URL-safe temp password. The user is forced to change it on first login (`User.must_change_password` is `true` until they hit `/api/auth/change-password`).

**Pluggable services:**

- `services/email_adapters/` — `console` (local dev, logs to stdout) and `ses` (AWS SES) via `FEOH_EMAIL_PROVIDER`. Same registry pattern as extraction/ERP adapters.
- `services/tenant_provisioning.py` — reusable async `provision_tenant()` used by both the CLI and the API.
- `services/rate_limit.py` — Redis sliding-window limiter, keyed on `(endpoint, subject)` where `subject` defaults to client IP but can be an explicit value (e.g. email). Signup uses three limits: per-IP `/start` + `/complete` (`FEOH_SIGNUP_RATE_LIMIT_PER_HOUR`, default 5), per-email `/start` (`FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR`, default 3, anti email-bombing), and per-IP `/slug-check` (`FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR`, default 120, anti-enumeration).
- `utils/slug.py` — regex + reserved-word blocklist + DB uniqueness check. `provision_tenant` runs `validate_slug_format` itself, before any DB work, so every provisioning path gets it — including `scripts/create_tenant.py` / `deploy/add-tenant.sh`, which pass an operator-typed slug straight through.
- `utils/hcaptcha.py` — server-side siteverify. Skips when `FEOH_HCAPTCHA_SECRET` is empty (local dev).
- `utils/passwords.py` — `generate_temp_password()` + `validate_password_complexity()` (min 12 chars, upper/lower/digit).

The captcha sitekey is exposed to the frontend via `GET /api/public-config` so the SvelteKit build doesn't need to bake it in, alongside `signup_enabled`.

`FEOH_SIGNUP_ENABLED` (default `true`) is the off switch: a router-level dependency makes every `/api/signup/*` route a bare 404 before validation, DB or rate limit, and the SPA renders "signup is closed" from `public-config` (`docs/self-service-signup.md` § Turning signup off).

Relevant env vars: `FEOH_SIGNUP_ENABLED`, `FEOH_ENVIRONMENT` (deployed envs with signup on refuse to boot with an empty `FEOH_HCAPTCHA_SECRET`), `FEOH_EMAIL_PROVIDER`, `FEOH_EMAIL_FROM`, `FEOH_AWS_SES_REGION`, `FEOH_PUBLIC_URL`, `FEOH_TENANT_URL_TEMPLATE`, `FEOH_HCAPTCHA_SECRET`, `FEOH_HCAPTCHA_SITEKEY`, `FEOH_SIGNUP_RATE_LIMIT_PER_HOUR`, `FEOH_SIGNUP_EMAIL_RATE_LIMIT_PER_HOUR`, `FEOH_SLUG_CHECK_RATE_LIMIT_PER_HOUR`.

## Secrets management (SOPS + AWS KMS, in the private infra-secrets repo)

Local dev needs no secret setup: `backend/.env.development` is **committed** with safe, no-risk local defaults and is loaded by `main.py` (the local-dev entrypoint) via `python-dotenv`, so a fresh clone runs immediately. A gitignored `backend/.env` holds personal overrides and wins over the committed defaults. The backend also runs straight off `app/config.py` defaults even with neither file present.

Deployed-environment secrets are encrypted with [SOPS](https://github.com/getsops/sops) under the project's AWS KMS key — `alias/feohledger-sops`, created in the FeohLedger AWS account by the estate account bootstrap — and live in the **private** estate repo `Absence0760/infra-secrets`, under `feohledger/`. **Never in this repo, not even encrypted:** it is public, and ciphertext in public history is archived and scraped forever (`docs/decisions.md` §12, §165). The root `.gitignore` ignores `*.sops` / `*.sops.yaml`, and CI's `env-isolation.yml` fails on a tracked one. The estate pattern and onboarding are `~/github/project-mgmt/docs/secrets-management.md`.

**File layout:**

```
backend/
├── .env.development     # local dev defaults — committed (safe, no-risk only); loaded by main.py
└── .env                 # personal local overrides — gitignored; wins over .env.development

~/github/infra-secrets/feohledger/   # PRIVATE sibling clone — not part of this repo
├── prod.sops.yaml.example           # plaintext template: key names only, never values
└── prod.sops.yaml                   # the encrypted prod secrets (created with the first real one)
```

**Edit the encrypted file** from the `infra-secrets` clone, authenticated to the FeohLedger account (that repo's `.sops.yaml` picks the key):

```bash
aws sso login --profile feohledger
AWS_PROFILE=feohledger sops feohledger/prod.sops.yaml   # decrypts → $EDITOR → re-encrypts on save
```

**The deployed VM** (`deploy/`, `docs/minimal-deployment.md`) reads `infra-secrets/feohledger/prod.sops.yaml` — flat YAML keyed by env var name, authored from `deploy/prod.sops.yaml.example` — copied onto the box as `deploy/prod.sops.yaml`, never committed here. `deploy/decrypt-env.sh` (run by `deploy.sh`) decrypts it host-side to the gitignored `deploy/.env` with the instance profile's `kms:Decrypt`, and refuses a file missing a required key or holding a value compose's `env_file` would silently rewrite (`docs/decisions.md` §171).

**Terraform** reads no secret yet. When the first one lands it is read in place with the `carlpett/sops` provider, never through a committed or decrypted tfvars — `infra/README.md` § Secrets.

**Adding a collaborator:** grant them `kms:Decrypt` (and usually `kms:Encrypt`, `kms:GenerateDataKey`) on `alias/feohledger-sops` via IAM, plus read access to the private repo. No re-encryption needed — IAM is the source of truth.

**Rotation** (the KMS key and every secret it protects): `docs/secrets-rotation.md`.

## Conventions

- **Async only** — all DB operations use SQLAlchemy 2 async. Don't introduce sync DB calls.
- **ruff** — `ruff check .` and `ruff format .` before committing. Line length 100.
  `[tool.ruff.format] exclude = ["*.md"]` keeps the formatter on Python source:
  ruff 0.16 started formatting Python blocks embedded in Markdown, and the docs'
  snippets are illustrative (elided `...` bodies, comment alignment used for
  emphasis) rather than runnable. Lint rules are unaffected.
- **Schemas** — Pydantic v2 models in `app/schemas/` for all request/response types.
- **Money leaves Python as a `Decimal`, never a `float()`.** On a response field
  use `schemas/money.MoneyAmount` / `OptionalMoneyAmount`; in a handler that
  returns a bare `dict` use `schemas/money.json_money`. Both emit the same JSON
  number a bare `float(...)` did, so neither moves the wire shape — the point is
  that the Python value stays exact, and that the one legitimate hop is
  greppable. A money-named field annotated `float` is the same defect written
  differently (pydantic fills it from a `Decimal` silently) and fails the same
  guard: `tests/test_money_serialization_exact.py`. Percentages are **not** money
  — they have their own pair in `schemas/percent.py`. Inbound money that decides
  where money goes uses `ExactMoneyInput` (a JSON number is already a float by
  the time pydantic sees it).
- **No dotenv in Lambda paths** — `main.py` imports dotenv for local dev; Lambda entry points must not.
- **Tenant isolation** — always resolve tenant via dependency injection (`get_tenant_db()`), never hardcode DB names.
- **Row locking** — use `get_invoice_for_update()` for any status transition to prevent race conditions.
