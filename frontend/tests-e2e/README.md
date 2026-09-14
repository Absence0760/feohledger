# frontend/tests-e2e/

Playwright end-to-end tests for the frontend.

## Parallelism + isolation

Two modes:

**CI: 14 shards × 1 worker.** `.github/workflows/ci.yml` fans the
suite out across 14 GitHub runners via Playwright's `--shard=N/14`.
Each shard is its own job with its own Postgres + Redis + seeded
backend (1 tenant: `e2e1`). Inside a shard, `workers=1` means the
shard's slice of the suite runs sequentially — no within-shard
parallel-worker contention. Specs in a shard share the shard's
tenant, but the serial execution order makes state mutations
predictable (the same model the suite was originally written
under).

**Local: workers=4 by default.** Run from anywhere with
`pnpm test:e2e` and four Playwright workers fan out against four
seeded tenants (`e2e1`..`e2e4`) for a faster inner loop. Each
worker is pinned to its own tenant via the worker-scoped
`tenantSlug` fixture in `fixtures/helpers.ts`; two workers running
the same spec can't collide because they're operating against
different Postgres databases.

| Worker index | Tenant slug | Base URL                     |
| ------------ | ----------- | ---------------------------- |
| 0            | `e2e1`      | `http://e2e1.localhost:7777` |
| 1            | `e2e2`      | `http://e2e2.localhost:7777` |
| 2            | `e2e3`      | `http://e2e3.localhost:7777` |
| 3            | `e2e4`      | `http://e2e4.localhost:7777` |

(Those base URLs are the defaults; set `E2E_WEB_ORIGIN` to move them — see
"Running from a worktree" below.)

If a local flake suggests within-worker spec interference, run
serially: `PLAYWRIGHT_WORKERS=1 pnpm test:e2e`. That matches
the CI-shard behaviour.

**`E2E_TENANT_OFFSET`** (default `0`) adds a fixed offset to every
worker's tenant index. It exists so that several *independent*
Playwright processes (e.g. parallel authoring sessions, each with
`PLAYWRIGHT_WORKERS=1`) can each be pinned to a distinct tenant instead
of all colliding on `e2e1`: process `k` sets `E2E_TENANT_OFFSET=k` and
its lone worker resolves to `e2e<k+1>`. Normal single- or multi-worker
runs leave it unset.

### Match CI's backend env when running locally

The CI e2e backend sets two env vars that the default `pnpm dev:backend`
does **not**; without them a long serial run (or several concurrent
independent processes) flakes on the shared auth surface:

- `FEOH_RATE_LIMIT_ENABLED=false` — the login endpoint is otherwise capped
  at 10/60s per IP, and every worker shares the loopback IP.
- `FEOH_MAX_CONCURRENT_SESSIONS=100` — the default cap of 5 evicts the
  per-worker cached storage-state JTI onto the blocklist once a spec
  re-logs-in as the same user ≥5 times (e.g. an `afterEach` re-auth),
  surfacing as a spurious `401` on later API setup calls.

Start the local e2e backend with both set (CI uses these exact values):
`FEOH_RATE_LIMIT_ENABLED=false FEOH_MAX_CONCURRENT_SESSIONS=100 python main.py`.

### Specs that don't follow the worker-tenant pattern

| Spec | Why it's pinned |
| ---- | --------------- |
| `auth/tenant-isolation.spec.ts` | Asserts `acme` cannot read `techflow` data — needs both fixed slugs. |
| `auth/cross-tenant-writes.spec.ts` | Asserts mutation requests with mismatched JWT + header are 403'd. Needs `acme` + `techflow`. |
| `auth/signup.spec.ts` | Uses the no-tenant landing (`http://localhost:7777`). |

## Layout

```
tests-e2e/
├── playwright.config.ts             config; webServer boots vite on E2E_WEB_ORIGIN's port; workers default 4
├── fixtures/
│   ├── env.ts                       web origin + API base — see "Running from a worktree" below
│   ├── helpers.ts                   per-worker fixtures + signIn / tenantPsql / etc.
│   ├── services.ts                  reachability gates for the opt-in compose services
│   └── globalSetup.ts               pre-run guard — see "Workflow shape guard" below
├── a11y/                            axe-core accessibility regression guard (WCAG 2.2 AA)
├── meta/                            source guards over the specs themselves — see "Row teardown" and "Ports" below
├── auth/                            login, signup, RBAC, tenant isolation
├── admin/                           user lifecycle, bulk-delete, custom roles
├── invoices/                        list, detail, edit, bulk recode, status transitions
├── payments/                        queue, runs, void/cancel, CFO approval, execute
├── workflows/                       lifecycle, step config, approval matrix, etc.
├── exceptions/                      queue, assign, filter, resolve
├── erp/                             merge-dev / netsuite / dynamics adapter e2e (gated — needs fake-erp)
├── credit-memos/                    list, pagination
├── organization/                    settings, fraud rules, GL accounts sync
├── purchase-orders/                 list, sync
├── goods-receipts/                  list
├── vendors/                         verify
└── smoke/                           nav smoke + grouped sidebar nav / section tabs / bell + profile popover (section-nav, sidebar specs)
```

`testDir: '.'` walks recursively, so adding a new spec under any of
these folders picks up automatically.

## Accessibility guard (`a11y/`)

`a11y/axe.spec.ts` is the automated WCAG 2.2 Level AA regression guard. It
runs [`axe-core`](https://github.com/dequelabs/axe-core) (via
`@axe-core/playwright`) against the key surfaces — dashboard, `/invoices`,
`/vendors`, `/payments`, `/exceptions`, the invoice detail modal, the AP
login page, and the supplier portal login — at the
`wcag2a,wcag2aa,wcag21a,wcag21aa,wcag22aa` tag set and asserts **zero
violations**. On failure it attaches a readable summary (rule id, impact,
help URL, offending nodes) so the CI log is actionable.

Because `testDir: '.'` walks recursively, the guard runs as part of the
normal `pnpm test:e2e` (and each CI shard) — so a change that reintroduces a
machine-detectable barrier fails CI. For a fast focused run:

```bash
pnpm test:e2e:a11y      # frontend/package.json — targets just tests-e2e/a11y
```

It uses the same per-worker `e2e<N>` tenant + admin storage-state fixtures as
every other spec (the two login surfaces opt out of the storage state). The
shared `a11y/axe-helper.ts` owns the tag set + the `expectNoA11yViolations`
assertion; reuse it when adding a route to the guard. Automated tooling only
covers the machine-detectable criteria — the manual screen-reader passes and
the conformance docs (`docs/accessibility.md`, `docs/accessibility-vpat.md`)
cover the rest.

## Workflow shape guard (`fixtures/globalSetup.ts`)

Runs once, in the main process, before any test/worker starts (wired via
`playwright.config.ts`'s `globalSetup`). It asserts every tenant (`acme`,
`techflow`, `e2e1..N`) has exactly one `is_default=true` workflow definition,
`is_active=true`, with its `approval` + `erp_export` steps enabled — the
shape `backend/scripts/seed.py` creates.

This exists because the workflow-mutating specs (`workflows/*.spec.ts`,
`workflow-builder.spec.ts`) flip a definition's `is_active` / step `enabled`
flags mid-test and restore them in a `finally` block — reliable against a
normal test failure, but not against a killed process or a timed-out test
whose continuation never gets scheduled. On a long-lived local dev database
that can leave a tenant stranded on a disabled definition, which then
surfaces as a confusing 409 in some *unrelated* later spec. The guard fails
fast, at the start of the run, naming the exact tenant and field — see
`docs/known-issues.md` for the full incident writeup.

Skip it (e.g. exercising one non-tenant spec by hand against an unseeded DB)
with `FEOH_E2E_SKIP_WORKFLOW_SHAPE_CHECK=true`.

## Row teardown (`deleteInvoicesWhere` / `deleteVendorsWhere` / `deleteWorkflowsWhere`)

`invoices` is referenced by **16 foreign keys and none of them cascade**, so a
bare `DELETE FROM invoices WHERE …` only succeeds while that invoice happens to
have no children. Around twenty specs hand-listed the two or three child tables
their invoice normally acquired, and `invoices/upload-refetch-failure` started
failing teardown the moment extraction began succeeding and writing line items —
a child its list didn't know about, in a spec whose subject is not teardown.

`fixtures/helpers.ts::deleteInvoicesWhere(predicate, slug?)` owns the whole
child graph: second-level children first (workflow steps, chat messages, bank
transactions and virtual cards hanging off payments), then every direct child,
then the invoices. `predicate` is the WHERE body, so scope by id,
`invoice_number LIKE`, `vendor_id`, `recurring_template_id` — anything.

```ts
import { deleteInvoicesWhere } from '../fixtures/helpers';

test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${MARKER}%'`));
```

### Three owners, three signatures

`invoices` was the first table to earn an owner; two more followed for the same
reason, and **their signatures differ** — read the one you need rather than
assuming.

| Helper | Takes | Scope seatbelt |
|---|---|---|
| `deleteInvoicesWhere(predicate, slug?)` | a WHERE body | none — the predicate is the whole scope |
| `deleteVendorsWhere(predicate, slug?)` | a WHERE body | none |
| `deleteWorkflowsWhere(namePrefix, slug?)` | a **name prefix** | `is_default = false`, baked in |

`deleteWorkflowsWhere` takes a prefix rather than a predicate on purpose. A
tenant's seeded default workflow must survive every teardown, and the fallback
definition the engine mints when an org has zero active ones must too — so the
`is_default = false` seatbelt belongs to the helper, not to each caller's
predicate. Handing it a free-form WHERE would let a caller write the one
teardown that empties a tenant.

`deleteVendorsWhere` reaches further than its name suggests: a vendor's graph
contains invoices, so it delegates to `deleteInvoicesWhere` rather than
restating it. Rows that exist only because of the vendor are deleted; independent
records that merely point at one of its rows have the link cleared.

### The source guard

`meta/teardown-guard.spec.ts` scans every `.ts` file under `tests-e2e/`
(comments stripped, so prose about the pattern is fine) and fails if any file
other than the helper itself issues `DELETE FROM invoices`, `DELETE FROM
vendors` or `DELETE FROM workflow_definitions`. Without it the twentieth spec
re-introduces the pattern, and the failure lands weeks later as a foreign-key
error in someone else's file. Its message quotes the real signature per table,
because a guard that tells you to pass a predicate to the one helper that takes
a prefix sends you to write exactly what it just refused.

## Local dev loop

Playwright auto-starts the frontend dev server via its `webServer`
block; the backend has to be running separately.

```bash
# terminal 1 — backend
pnpm db:up                    # Postgres + Redis (+ MinIO)
cd backend && python scripts/seed.py    # creates acme + techflow + e2e1..e2e4
pnpm dev:backend              # uvicorn on :8000

# terminal 2 — playwright
pnpm install:frontend         # one-time
cd frontend && pnpm exec playwright install chromium    # one-time browser install
pnpm test:frontend            # workers=4 by default
PLAYWRIGHT_WORKERS=1 pnpm test:frontend    # serial run, easier to debug a flake
```

The `pnpm db:up` / `dev:backend` / `test:frontend` invocations are
the root dispatch scripts; see the repo's root README for the rest.

## Running from a worktree

A worktree isolates **files**. It does not isolate ports, it does not carry
`backend/.venv`, and it does not carry `frontend/node_modules`. Each of those
three fails *silently* — the suite goes green, against a tree you did not edit
— so all three are worth fixing before you trust a result measured from one.

### Ports (`E2E_WEB_ORIGIN` / `PUBLIC_API_URL`)

The dev server's port and the backend's origin are **configurable**, and both
default to today's values — an ordinary run needs nothing:

| Variable          | Default                 | What it points at                              |
| ----------------- | ----------------------- | ---------------------------------------------- |
| `E2E_WEB_ORIGIN`  | `http://localhost:7777` | The SvelteKit app, without a tenant subdomain.  |
| `PUBLIC_API_URL`  | `http://localhost:8000` | The FastAPI backend (also passed to Vite).      |

Set both when a second session runs the suite at the same time — most often a
git **worktree** (root `CLAUDE.md` § "Running concurrent sessions"):

```bash
E2E_WEB_ORIGIN=http://localhost:7801 PUBLIC_API_URL=http://localhost:8001 \
  E2E_TENANT_OFFSET=1 pnpm test:e2e
```

Why it matters: `playwright.config.ts` sets `reuseExistingServer` locally, so a
worktree that keeps the default port finds the **primary checkout's** dev server
already listening and tests *that* build — silently, with every spec passing
against code you did not change. The alternative a session reaches for is taking
the port from whoever owns it, which is worse: two round-24 agents hit this and
one stopped and restarted a server it did not own. Worktrees isolate files, not
ports.

`fixtures/env.ts` resolves both values and derives everything else from them —
the per-tenant subdomain origins (`tenantOrigin(slug)`, and the `ACME_BASE` /
`TECHFLOW_BASE` / `NO_TENANT_BASE` constants), the post-login landing pattern
(`TENANT_ROOT_URL`), and the `--port` the `webServer` block passes to
`vite dev` / `vite preview`. Nothing else should read a port.

`meta/origin-guard.spec.ts` is what keeps that true. It scans every `.ts` file
under `tests-e2e/` (comments stripped, `fixtures/env.ts` exempt as the owner)
and fails on four shapes: a literal `:7777`, a literal `:8000`, a hand-built
``http://${slug}.localhost``, and a direct read of `E2E_WEB_ORIGIN` /
`PUBLIC_API_URL`. One re-introduced literal is enough to pin a single spec to
the wrong port and have it cross servers mid-suite, which is a green run against
a tree you did not edit — so the guard treats all four as the same defect. It
also pins the behaviour the derivation depends on: that `TENANT_ROOT_URL` is
anchored to the tenant root rather than matching any page below it, and that
`escapeRegExp` escapes backslashes, so one tenant's pattern can never be
satisfied by another tenant's origin.

Two things stay shared and are NOT covered by these variables:

- **The databases.** Every session still shares one Postgres, so pin a distinct
  tenant with `E2E_TENANT_OFFSET` (above) and give the second backend its own
  `FEOH_DATABASE_URL` if you want full isolation.
- **The opt-in Docker services** in `fixtures/services.ts` (Keycloak :8088,
  Mailpit :8025, LocalStack :4566, stripe-mock :12111, fake-erp :12112). They
  are one compose stack per machine, like Postgres — a second suite reuses them
  rather than starting its own.
- **The backend's own idea of where the frontend lives.**
  `FEOH_TENANT_URL_TEMPLATE` (`http://{slug}.localhost:7777` in
  `.env.development`) is what the *backend* stamps into invite links, portal
  links and the SSO callback base — `E2E_WEB_ORIGIN` does not reach it. Two
  specs assert against that template (`organization/tenant-url.spec.ts`,
  `organization/custom-domain-rejection.spec.ts`), so a second session on a
  non-default web origin must set `FEOH_TENANT_URL_TEMPLATE` on its backend to
  match, or those two fail while everything else passes.

### The backend's Python imports (`backend/.venv` is the primary checkout's)

**Symptom:** everything on screen names the worktree, and the code that ran
came from the primary checkout. No error, no warning — the assertions simply
measured a tree that was never edited, which makes a green result meaningless
rather than merely wrong.

`backend/.venv` does not carry into a worktree (its paths are absolute), so a
worktree reuses the primary one. That venv holds an **editable install**:
`pip install -e ".[dev]"` writes
`site-packages/__editable___backend_0_1_0_finder.py`, whose
`MAPPING = {'app': '<the checkout pip was run in>/backend/app'}` is baked in at
install time and answers `import app` with that absolute path.

**Why `PYTHONPATH` is not the problem** — a claim to the contrary has been
written down twice in this repo and is wrong, so it is worth being precise.
setuptools *appends* that finder:

```python
def install():
    if not any(finder == _EditableFinder for finder in sys.meta_path):
        sys.meta_path.append(_EditableFinder)      # append — after PathFinder
```

`sys.meta_path` ends up `[…, PathFinder, _EditableFinder]`, so `PathFinder` —
which is the thing that reads `sys.path` — is consulted **first**. Anything
genuinely on `sys.path`, `PYTHONPATH` entries included, beats the finder.

The real failure is a **fall-through**: when `<worktree>/backend` is on
`sys.path` at all, you get the right tree; when it is not, `PathFinder` finds
nothing, the appended finder answers, and you silently get the other checkout.
That distinction is what decides which commands are safe:

| Command, run from `<worktree>/backend` | `sys.path[0]` | Which `app`? |
| --- | --- | --- |
| `python main.py` | the script's dir = `<worktree>/backend` | worktree ✅ |
| `pytest tests/x.py` | pytest prepends `<worktree>/backend` | worktree ✅ |
| `alembic upgrade head` / `revision --autogenerate` | `alembic.ini` prepends `<worktree>/backend` | worktree ✅ |
| `python scripts/<anything>.py` | the script anchors `<worktree>/backend` | worktree ✅ |
| `uvicorn app.main:app` | the venv's `bin/` | **primary ❌** |
| `pytest --import-mode=importlib tests/x.py` | no prepend at all | **primary ❌** |

The middle two ✅ rows are safe *by design*, and each fixes itself:

- `backend/alembic.ini` sets `prepend_sys_path = %(here)s`, because
  `alembic/env.py` imports `app.config` and `app.models` — an autogenerate
  against the wrong tree writes a migration file that looks entirely plausible
  and is wrong. `%(here)s`, not the `.` alembic's own template suggests: the
  value is spliced onto `sys.path` verbatim, so `.` follows the process CWD
  rather than the ini.
- **Every** `backend/scripts/*.py` that imports `app` carries a two-line anchor
  in its import prologue (`sys.path.insert(1, <this backend/>)`, guarded on
  presence) — 13 of the 14 today. `pnpm seed` and `pnpm migrate:all` are the
  first commands a contributor runs, so they must be right without anyone
  having opted into the shim. Index 1, never 0 — `seed.py` imports
  `seed_extras` bare and needs `scripts/` to stay first.
  `backend/tests/test_script_checkout_anchor.py` **globs** the directory rather
  than naming files, so a new script that imports `app` and forgets the anchor
  fails the suite by name; a script that imports no `app` is exempt by that
  same derivation.

The other two ✅ rows are safe by *accident*: `python main.py` because a
script's own directory lands on `sys.path`, and `pytest` only because the
default `prepend` import mode walks up past `tests/__init__.py` to
`<worktree>/backend` — the last row is that same console script with that one
default changed, silently testing the primary checkout.

**The fix — one env var, and it covers every row:**

```bash
PYTHONPATH=<worktree>/backend/scripts/worktree <primary>/backend/.venv/bin/python scripts/seed.py
```

`backend/scripts/worktree/sitecustomize.py` is a checked-in shim. `site`
auto-imports a `sitecustomize` module at interpreter startup **if it is
importable then** — which `sys.path[0]` is not, because the script's directory
is inserted later; `PYTHONPATH` is the mechanism that works, and it is why a
plain `cd` into that directory does nothing. Once loaded the shim puts the
checkout you are running *from* on `sys.path` and removes the `__editable__*`
finder, so a name it could not fix raises `ModuleNotFoundError` instead of
resolving somewhere else. It is a **no-op inside the checkout the editable
install points at**, and a no-op when it cannot work out which checkout you are
in, so exporting it for a whole shell is safe and it is inert in the primary
checkout. Guarded by `backend/tests/test_worktree_sitecustomize.py`.

Export it once, from `<worktree>/backend`, and every command below is covered:

```bash
export VENV=<primary>/backend/.venv && export PYTHONPATH=<worktree>/backend/scripts/worktree
$VENV/bin/pytest tests/test_x.py -q            # scoped files only — never the full suite
$VENV/bin/python scripts/seed.py
$VENV/bin/alembic upgrade head
$VENV/bin/uvicorn app.main:app --reload --port 8001    # main.py hardcodes :8000, which the primary owns
```

`python main.py` is in the ✅ column and needs nothing beyond the venv's
interpreter (`$VENV/bin/python main.py`) — its own directory lands on
`sys.path` first. The reason to use the `uvicorn` line instead is the **port**,
not the imports: `main.py` hardcodes `:8000`, so a worktree that runs it either
fails to bind or, worse, is the thing that took the port from the primary
checkout. Set `PUBLIC_API_URL` to match whatever port you pick (see *Ports*
above). `uvicorn` is a console script, so it is one of the ❌ rows — the shim
is what makes it load your tree.

`git rev-parse --path-format=absolute --git-common-dir` prints
`<primary>/.git` from inside a worktree, so `<primary>` is that path's
directory — handy when scripting, but spell it out when pasting.

**Verify which tree is actually being served.** Do this once per session; it is
the whole point, and it is two seconds:

```bash
(cd scripts && $VENV/bin/python -c "import sitecustomize, app; print(sitecustomize.__file__); print(app.__file__)")
```

Run it from `scripts/` — that is the discriminating case, the one where nothing
puts the worktree on `sys.path` for you. The first line proves the shim is
active (a `ModuleNotFoundError` means `PYTHONPATH` is not set); the second must
name **your** worktree. Or ask the shim what it sees, which also explains its
verdict:

```bash
$VENV/bin/python scripts/worktree/sitecustomize.py
```

**The alternative, if you would rather not activate anything:** run pytest on
the system interpreter with the venv's `site-packages` on `PYTHONPATH`.

```bash
PYTHONPATH=<primary>/backend/.venv/lib/python3.14/site-packages python3 -m pytest tests/test_x.py -q
```

`PYTHONPATH` directories get no `.pth` processing, so the editable finder is
never installed at all — there is nothing to fall through *to*, and a bad
`sys.path` becomes a loud `ModuleNotFoundError` rather than a quiet wrong
answer. It needs the system python's minor version to match the venv's
(both 3.14 today), and it does not help `python main.py` or any console script,
which is why the shim is the recommendation.

### The frontend's `node_modules`

Not carried over either, and unlike the venv there is no shim — `pnpm install`
in the worktree's `frontend/` before `pnpm check`, `pnpm test:unit` or
`pnpm build`. The pnpm store is warm, so it is quick. Playwright's browser
binaries live in `~/.cache/ms-playwright` and *are* shared, so
`playwright install chromium` does not need repeating.

## Service-backed specs (gated)

Some specs exercise a flow that needs an optional local container the
default `pnpm db:up` stack doesn't include. They live in their own
folders and **skip with an actionable message** when the service is
down (via `skipUnlessReachable` in `fixtures/services.ts`) — so the
normal suite stays green without them, and they run for real once the
container is up.

| Spec | Needs | Bring it up |
|---|---|---|
| `sso/login.spec.ts` | Keycloak + acme SSO seeded | `pnpm idp:up && pnpm idp:seed` |
| `email/signup-email.spec.ts` | Mailpit + backend on `FEOH_EMAIL_PROVIDER=smtp` | `pnpm mail:up`, restart backend with smtp |
| `erp/` (merge-dev / netsuite / dynamics specs) | fake-erp on :12112 + backend on the committed `.env.development` `FEOH_ERP_*` base URLs | `pnpm erp:up`, then `pnpm test:erp` |
| `scim/provisioning.spec.ts` | (none — CI-safe contract test) | always runs |

Backend-only service flows are pytest integration tests, also gated:
`backend/tests/test_localstack_integration.py` (LocalStack — `pnpm
aws:up`) and `test_stripe_mock_integration.py` (stripe-mock — `pnpm
stripe:up`). `test_ollama_integration.py` is local-only (model
inference is too heavy for CI).

These all run in CI's **`service-e2e`** job (see below), which starts
the containers and seeds SSO before running them.

## CI

`.github/workflows/ci.yml`'s `e2e` job runs the same flow on every
push/PR to main, **sharded across 14 parallel GitHub runners** via
Playwright's `--shard=N/14` flag. Each shard:

- pgvector/pgvector:pg16 + Redis 7 as services (per-shard, isolated)
- `FEOH_E2E_TENANT_COUNT=1` — each shard only needs one tenant
  (`e2e1`) since it runs `workers=1`. Skips provisioning the other
  three e2e tenants and shaves ~5 s off seed time per shard.
- Python 3.14 → install backend deps →
  `python scripts/seed.py --lean` (creates acme + techflow + e2e1
  in *this shard's* Postgres) → `python main.py &`
- Wait for `/api/health` to answer 200
- pnpm 9 + Node 20 →
  `pnpm install --frozen-lockfile` →
  `pnpm build` →
  `pnpm exec playwright install chromium` (browser binaries, restored from
  the `~/.cache/ms-playwright` cache) →
  `pnpm exec playwright install-deps chromium` as a **separate, bounded,
  non-blocking** step (see *Why the font deps are best-effort* below)
- `PLAYWRIGHT_WORKERS=1 pnpm exec playwright test --config=… --shard=${{ matrix.shard }}/14`
- Playwright report + backend log uploaded as
  `playwright-report-shard-${N}` / `backend-log-shard-${N}` on failure

### Why the font deps are best-effort

`playwright install --with-deps` used to do both halves in one step. On the
GitHub runner image every Chromium shared library (`libnss3`, `libgbm1`,
`libatk`, `libcups2`, `xvfb`, …) is already the newest version, so the `apt`
half installs nothing but **9 font packages** — CJK, Cyrillic, Thai, Unifont,
FreeFont and the X11 bitmap fonts.

Nothing in this suite asserts on rendering: there are no `toHaveScreenshot` /
`toMatchSnapshot` callers, and `screenshot: 'only-on-failure'` in
`playwright.config.ts` produces a debug artifact, not an assertion. A missing
font therefore cannot change a result — it only means non-Latin glyphs render
as tofu in a failure screenshot. That is worth keeping, which is why the step
still runs, but it is not worth failing a run over.

It is split out, capped at `timeout-minutes: 3`, and marked
`continue-on-error: true` because `install-deps` shells out to `apt-get
update`, which reaches external Ubuntu mirrors. On 2026-08-18 that call hung
for 22 minutes and consumed the entire 25-minute `service-e2e` job budget
(PR #316) — a cosmetic dependency must not be able to time out the job.

Effective parallelism is **14 shards × 1 worker = 14** parallel test
processes spread across 14 separate runner VMs. `fail-fast: false`
keeps the other shards going when one fails, so a flake in shard 2
doesn't hide a real regression in shard 4.

Each shard runs ~20 specs serially (274 / 14). Wall-clock per shard
is dominated by the per-shard setup (~30 s) + the ~20 specs at
~2 s each = ~70 s total. Local `workers=4` runs the whole 274
specs concurrently in ~2 min.

The sharded `e2e` job has only Postgres + Redis, so the service-backed
specs above just skip there. A separate **`service-e2e`** job (single
runner) starts Keycloak + Mailpit + LocalStack + stripe-mock via
`docker compose` after checkout, seeds acme SSO, points the backend at
them, and runs `sso/ email/ scim/` plus the LocalStack + stripe-mock
pytest integration tests — so those flows actually run in CI. Ollama is
excluded (model inference too heavy for CI runners).

A dedicated **`.github/workflows/sso-e2e.yml`** workflow gives SSO its
own focused green/red signal: it boots **only Keycloak** (not the full
four-service bundle), seeds acme SSO, and runs just `sso/`. It triggers
on push/PR that touch the SSO surface (the backend SSO route + service,
the Keycloak realm + seed script, the login routes, the `sso/` specs,
its `fixtures/`) and on manual `workflow_dispatch` — no schedule. Use it
for fast SSO feedback without waiting on the 25-minute `service-e2e`
bundle; `service-e2e` remains the comprehensive multi-service gate.

A dedicated **`erp-e2e`** job in `ci.yml` (modeled on `service-e2e`,
gated by the same `changes.e2e` filter and required by `ci-gate`) does
the same for the ERP suite: it boots only fake-erp (`docker compose
--profile erp`), lets the backend pick up the committed
`.env.development` `FEOH_ERP_*` base URLs, and runs just `erp/` with
`FEOH_REQUIRE_INTEGRATION=1` so a skip there is a hard failure. Locally
the same specs skip with an actionable message when fake-erp isn't
reachable (`pnpm erp:up` starts it), so the normal suite stays green
without the container.

## Seeded credentials

Defined in `backend/scripts/seed.py`. Spec files almost always want
the per-worker fixtures (`tenantAdmin` / `tenantManager` /
`tenantClerk` / `tenantCfo`), not the static `acme` / `techflow`
constants — the static set is for the cross-tenant specs only.

| Tenant | Role | Email | Password |
| ------ | ---- | ----- | -------- |
| acme | admin | `demo@acme.com` | `demo` |
| acme | ap_manager | `demo+apmanager@acme.com` | `demo` |
| acme | ap_clerk | `demo+apclerk@acme.com` | `demo` |
| acme | cfo | `demo+cfo@acme.com` | `demo` |
| techflow | admin | `admin@techflow.com` | `demo` |
| `e2e<N>` | admin | `demo+admin@e2e<N>.localhost` | `demo` |
| `e2e<N>` | ap_manager | `demo+manager@e2e<N>.localhost` | `demo` |
| `e2e<N>` | ap_clerk | `demo+clerk@e2e<N>.localhost` | `demo` |
| `e2e<N>` | cfo | `demo+cfo@e2e<N>.localhost` | `demo` |

## Fixture helpers (`fixtures/helpers.ts`)

Reach for these instead of duplicating boilerplate per spec:

- `test` / `expect` — re-export of the extended Playwright `test`.
  **Always import from `'../fixtures/helpers'`, not `@playwright/test`**,
  so the worker-scoped fixtures (`tenantSlug`, `tenantAdmin`, …)
  resolve correctly.
- Worker fixtures: `tenantSlug`, `tenantAdmin`, `tenantManager`,
  `tenantClerk`, `tenantCfo` — destructure in the test signature.
- `signIn(page, creds?)` / `signInAndWait(page, creds?)` — default
  to the worker's admin when called without `creds`.
- `currentTenantSlug()` — read the worker's slug from outside a
  test arg (rarely needed; prefer destructuring the fixture).
- `authToken(page)` — pull the JWT from `localStorage`.
- `tenantHeaders(token, slug?)` + `authedTenantHeaders(page, slug?)`
  — compose the `Authorization` + `X-Tenant-Slug` headers for an
  authenticated, tenant-scoped API request. Default to the
  worker's slug.
- `tenantPsql(query, slug?)` — `psql -d feoh_<slug>` for the half
  dozen specs that need to clobber DB state the API doesn't expose
  (hard-delete an approved invoice, force-fail a settled payment,
  etc.). Defaults to the worker's DB.
- `deleteInvoicesWhere(predicate, slug?)`, `deleteVendorsWhere(predicate,
  slug?)`, `deleteWorkflowsWhere(namePrefix, slug?)` — the ONLY supported way to
  delete rows in those three tables. See "Row teardown" above; a source guard
  fails the suite if a spec issues its own `DELETE FROM`. Note the third takes a
  name prefix, not a predicate.
- `API_BASE`, `ACME_BASE`, `TECHFLOW_BASE`, `NO_TENANT_BASE`,
  `tenantBase(slug)` — the same origins everyone was redeclaring inline, all
  derived from `E2E_WEB_ORIGIN` / `PUBLIC_API_URL` (see "Running from a
  worktree").
- `TENANT_ROOT_URL` — the post-login landing pattern. Wait on this rather than
  writing a `/:7777\/?$/` regex, which pins the spec to the default port.

## Subdomain trick

The frontend resolves the tenant from the subdomain
(`<slug>.localhost:7777` → `<slug>`). Chromium auto-resolves
`*.localhost` to 127.0.0.1 per RFC 6761, so no `/etc/hosts` edits
needed. To exercise the no-tenant marketing landing, override
`baseURL` to the bare origin — `test.use({ baseURL: NO_TENANT_BASE })`,
importing `NO_TENANT_BASE` from `fixtures/helpers.ts`. Prefer that over a
literal `'http://localhost:7777'`, which pins the spec to the default port and
breaks under `E2E_WEB_ORIGIN` (see "Running from a worktree" above).

### …and the vanity-host origin

A white-label **vanity** host is any host NOT under `PUBLIC_PLATFORM_DOMAINS`,
where the SPA sends no `X-Tenant-Slug` and calls `/api` same-origin. No
`*.localhost` name can play that part — `localhost` is exactly what the harness
declares as the platform domain — so `fixtures/env.ts::VANITY_ORIGIN` is the
loopback **IP literal** instead: never a platform host unless listed verbatim,
served by Vite with no `allowedHosts` entry, and needing no DNS or Chromium
resolver rule. `playwright.config.ts` pins the server to `--host 127.0.0.1` so
the literal always connects — Vite's `localhost` default resolves to `::1` on
some machines, and a literal reaches only the address that was bound.
`test.use({ baseURL: VANITY_ORIGIN, storageState: { cookies: [], origins: [] } })`
— the stored admin token is keyed by origin and does not travel there.

Two pieces of plumbing make it work, and both must stay in step:
`playwright.config.ts` passes `PUBLIC_PLATFORM_DOMAINS` to the dev server while
CI's `pnpm build` bakes it into the preview bundle (both from
`fixtures/env.ts::PLATFORM_DOMAINS`), and `vite.config.ts` proxies `/api` on the
same origin so the vanity `Host` reaches the backend. See
`tenant/vanity-host.spec.ts`'s header and `docs/white-label.md` § Custom domains.

## Storage-state (future)

The auth-storage-state pattern (sign each user in once, persist to
`.auth/<user>.json`, then `test.use({ storageState })` per spec) is
already the default (`fixtures/helpers.ts::_ensureAdminStorageState`),
just lazy per-worker rather than precomputed for every seeded user
up front — login is fast enough that this is fine. A `globalSetup`
now exists (`fixtures/globalSetup.ts`, see "Workflow shape guard"
above) but for an unrelated pre-run assertion; if per-user
storage-state precompute is ever worth doing, extend that same file
rather than adding a second `globalSetup` — Playwright only runs one.
