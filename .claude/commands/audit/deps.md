---
description: Cross-workspace dependency audit across four ecosystems (pnpm, pip/uv, pub, Terraform) + Dependabot coverage + GitHub Actions SHA pinning
---

Sweep dependencies across every workspace for known CVEs and version drift; verify Dependabot covers every ecosystem the repo actually ships, and that CI workflow pins aren't a supply-chain risk.

## What this is

Four ecosystems, not one — a JS-only sweep misses most of the attack surface:

- `frontend/` — SvelteKit 3 / Svelte 5 / Vite / Playwright, **pnpm**, lockfile at `frontend/pnpm-lock.yaml` (**not** the repo root).
- `backend/` — Python 3.12+, FastAPI / SQLAlchemy 2 / Alembic. Declared in `backend/pyproject.toml` (+ the pip pin in `backend/requirements-dev.in`), compiled to `backend/requirements.lock` and `backend/requirements-dev.lock` by `uv pip compile --universal --generate-hashes`.
- `mobile/` — Flutter 3.41+ / Dart 3.11+, `mobile/pubspec.yaml` + `mobile/pubspec.lock`.
- `infra/` — Terraform providers/modules.
- `tools/fake-erp/` — the local e2e fixture ERP: its own Dockerfile (digest-pinned base) + `requirements.in`/`requirements.txt`.
- Root `package.json` — pnpm dispatch scripts only, no dependencies of its own.

There is already a scheduled `.github/workflows/audit.yml` running `pnpm audit` weekly and filing an issue on findings. This command does that sweep on demand **plus** the three things the scheduled job does not cover: the non-JS ecosystems, Dependabot config drift, and workflow pinning.

## What to check

1. **Per-ecosystem vulnerability sweep.**
   - `pnpm -C frontend audit --audit-level=moderate`
   - Python: `uv pip compile` locks are hash-pinned — check the pinned set against advisories (`pip-audit -r backend/requirements.lock` if available, otherwise resolve the top offenders by hand). Note the lock is `--universal`, so a finding may apply to a platform this deployment never runs.
   - `cd mobile && flutter pub outdated` (and `dart pub audit` if the SDK provides it).
   - Terraform: provider versions in `infra/` versus current releases.
   For each finding: package, version, CVE, fix version, which lockfile pins it, and whether it is on a **runtime** path or dev-only.

2. **Open audit issue.** `gh issue list --label dependency-audit --state open`. If one exists, surface its title and confirm whether today's findings match — a stale open issue that no longer reproduces is itself a finding.

3. **Dependabot coverage.** Read `.github/dependabot.yml`. Current entries: `github-actions` at `/`, `pip` at `/backend`, `npm` at `/frontend`, `docker` at `/backend` and `/tools/fake-erp`, `pip` at `/tools/fake-erp`, `terraform` at `/infra`.
   - **Known gap to confirm or close: `mobile/` has no entry.** Dependabot supports `package-ecosystem: pub` for Dart/Flutter; without it the mobile app's dependency tree is never bumped and never scanned. Report it every run until it is either added or a decision entry says why not.
   - Flag any *new* workspace or ecosystem added since the last run without a matching entry.
   - Confirm grouping still reduces churn rather than hiding a security bump inside a 30-package group.

4. **Lockfile-sync workflow.** Dependabot bumps a manifest but never the lockfile compiled from it, so every Dependabot PR lands half-applied. Two ecosystems here:
   - frontend: `frontend/package.json` bumped, `frontend/pnpm-lock.yaml` not — breaks `ci.yml`'s `pnpm install --frozen-lockfile`.
   - backend: `backend/pyproject.toml` bumped, `backend/requirements{,-dev}.lock` not — Dependabot's pip-compile support only recognises a `<basename>.txt` paired with `<basename>.in`, and ours are compiled from `pyproject.toml`, so it cannot see them. `backend/tests/test_dependency_lock_sync.py` then fails the PR *by design*, so the drift is loud.
   The compensating workflow is `.github/workflows/dependabot-lockfile.yml`. Verify:
   - Both regenerate jobs are present (pnpm **and** the `uv pip compile` one) and each runs against the correct working directory (`frontend/`, `backend/`) — a version of this workflow that assumes a root `pnpm-lock.yaml` and no Python leg is the base-template shape and is wrong here.
   - The credential-bearing `push` job never checks out the PR head, and the regenerate jobs hold no secret.
   - It uses `DEPENDABOT_LOCKFILE_PAT` (fine-grained, `Contents: Write`), not `GITHUB_TOKEN` — GitHub blocks the latter from retriggering `pull_request` events. If the PAT is expired or revoked, dep PRs silently pile up; that is a **High**.

5. **GitHub Actions pinning.** Grep `.github/workflows/` for `uses: <action>@<ref>`.
   - Every third-party action should be a full **commit SHA** with the version in a trailing comment; this repo's Scorecard workflow (`scorecard.yml`) grades exactly this.
   - A floating ref (`@main`, `@v6`) on any workflow that touches `${{ secrets.* }}` or deploys is **High**: `aws-deploy.yml`, `dependabot-lockfile.yml`, `mobile-release.yml`, `claude.yml`, `terraform.yml`.
   - Also check the pins are not silently *going backwards*: a sync from a template repo can downgrade a SHA (checkout v7 → v6) while looking like a routine diff.

6. **Version-floor hygiene.** For each manual pin or constraint (`backend/pyproject.toml` floors, `frontend/package.json` ranges, any pnpm override): is it still needed, is the range tight, and does a comment or commit message name the CVE or bug it exists for?

7. **Runtime versions agree across the repo.** Node 22 in `ci.yml` / `setup-node` versus `frontend/package.json`; Python 3.12+ in `pyproject.toml` versus the `--python-version` passed to `uv pip compile` (currently 3.14) versus the Dockerfile base; Flutter/Dart SDK constraints in `mobile/pubspec.yaml` versus the CI mobile job. A mismatch here is how a lock resolves for an interpreter nobody runs.

8. **Base-image digests.** `backend/Dockerfile` and `tools/fake-erp/Dockerfile` pin by digest (Scorecard `PinnedDependencies`). Confirm both are still digest-pinned and that the Dependabot `docker` entries that keep them fresh are present.

## Report

- **Critical** — a known-exploited CVE on a runtime path (the deployed backend image, the shipped frontend bundle, the mobile app).
- **High** — a CVE with a fix available; a floating action ref on a secret-bearing or deploying workflow; an ecosystem with no Dependabot coverage at all; an expired lockfile-sync PAT.
- **Medium** — overdue drift with no CVE; a loose constraint; runtime-version mismatch between repo and CI or image.
- **Low** — undocumented pin, floating ref on a low-stakes workflow, grouping that could be tightened.

For each finding: package + version + advisory link + the file to change + the exact upgrade or override command.

## Useful starting points

- `frontend/package.json`, `frontend/pnpm-lock.yaml`
- `backend/pyproject.toml`, `backend/requirements-dev.in`, `backend/requirements{,-dev}.lock`, `backend/tests/test_dependency_lock_sync.py`
- `mobile/pubspec.yaml`, `mobile/pubspec.lock`
- `infra/` provider blocks; `tools/fake-erp/`
- `.github/dependabot.yml`, `.github/workflows/{audit,ci,scorecard,dependabot-lockfile}.yml`
- `backend/CLAUDE.md` § Dependency lock — the canonical `uv pip compile` invocations

## Delegate to

Use a `general-purpose` agent — the work is mostly running each tool in turn and reading the output. Pass this file as the prompt body.

Read-only audit. Recommend upgrades; don't apply them without instruction (a major bump is its own conversation).
