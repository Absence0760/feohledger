---
description: Fix a failing CI job from a GitHub Actions run (backend / frontend / mobile / e2e). Root-causes the failure, fixes it without coding around it (no retry/timeout/skip band-aids), reproduces locally with the same command CI used, and lands coverage so the failure can't return silently.
argument-hint: "<GitHub Actions run URL or run ID> [optional: which job — backend | frontend | mobile | e2e]"
---

Fix the failing CI run `$ARGUMENTS`. Find the real cause, fix it at the root, and add coverage.

## The two hard rules (these override convenience)

1. **Do not code around the issue.** A red test that catches a real defect is doing its job — fix the defect, not the test. This is guard rail 4 (fix root cause, never mask) and the "Fix bugs at the source" section of CLAUDE.md. Forbidden "fixes" unless you can prove the failure is pure infrastructure noise *and* name the structural reason the band-aid is the right call:
   - bumping a Playwright `expect` / `toBeVisible` timeout, a retry count, or a `sleep` / `waitForTimeout` to paper over a slow or racy path
   - adding `.skip` / `test.fixme` / `xfail` / `@pytest.mark.skip` / `continue-on-error` / `fail-fast: false` to hide a failure
   - loosening an assertion, widening a tolerance, or deleting the failing case
   - re-running until green
   If you catch yourself reaching for one of these, stop: you've found the symptom, not the cause. A red test guarding a project invariant (money is `Decimal`/`Numeric`; idempotency on money writes; audit append-only; tenant isolation via `get_tenant`; auth before everything; webhooks HMAC + dedupe; `bcrypt_sha256`) is **especially** doing its job — fix the defect.

2. **Add coverage where the gap let the failure through.** Whatever broke, leave behind something that fails loudly and early next time — a pinning pytest case for a backend defect, a Playwright spec for an e2e regression, a `flutter test` for a mobile one, or an explicit assertion / guard for a missing precondition. Coverage ships in the **same commit** as the fix (guard rail 2; CLAUDE.md § Every change must update docs and tests).

## Procedure

### 1. Pull the failure apart

- `gh run view <id>` to see which job(s) failed (`backend`, `frontend`, `mobile`, `e2e`) and at which step. The `changes` path-filter job gates the rest, so a skipped job often just means its paths weren't touched — confirm which job actually went red.
- `gh run view <id> --log-failed`, or for one job `gh run view --job=<job-id> --log-failed`. Grep to the **actual error** — the first thing that broke, not the final `exit 1`. The real signal is usually a `ruff` violation, a `ruff format --check` diff, a pytest assertion/exception, a `svelte-check` error, a `flutter analyze` lint, or a Playwright failure deep in the step output. `gh` rate-limits — anchor your greps rather than dumping whole logs repeatedly.
- Quote the failing job + step name + the error line back to the user so you're both anchored on the same failure.

### 2. Classify it honestly

Decide which of these it is, and say so:

- **Genuine defect** — the app/test/migration is wrong. Fix the defect; pin it with a test.
- **Test bug** — the test asserts the wrong thing, has a race in *its own* setup, or collides with seed data (a unique-constraint clash with the seeded `acme` tenant). Fix the test correctly (not by loosening it).
- **Infra flake** — a CI-environment failure (cold Postgres/Redis/MinIO sidecar, slow image pull, port clash, resource limit). The fix is to **remove the fragile dependency or make the step deterministic**, not to retry it. If retries already exist and still failed, that is proof retries are the wrong tool — find what the step is actually waiting on and gate on *that*, or restructure so the fragile operation never happens.

"It passed on re-run" narrows it toward flake, but does **not** license a band-aid — a flake still has a root cause.

### 3. Read the surrounding context before changing it

CI steps and workflow files carry comments documenting prior incidents and why the current shape exists. Read them. Your fix should make those comments obsolete by removing the failure mode, and you should update or replace the comments to match — don't leave a comment describing a workaround you just deleted. If the root cause might be a schema/migration interaction, pull in the `migration-coordinator` agent before touching DDL.

### 4. Reproduce locally, then verify the fix locally

Wherever the failure can be reproduced on this workstation, do it — it's the difference between a guess and a fix. **Reproduce with the same command CI used**, against CI's actual conditions (pinned tool versions, default behaviour) — not what a comment or doc claims. If your repro only passes because you configured it to match a stale assumption, you've validated the assumption, not the fix.

- **Backend** (`backend` job): `cd backend && source .venv/bin/activate && ruff check . && ruff format --check . && pytest`. For one test: `pytest backend/tests/test_x.py::test_y`. The job runs Postgres + Redis sidecars — bring up the local equivalents with `pnpm db:up` if a test needs them.
- **Frontend** (`frontend` job): `cd frontend && pnpm check` (svelte-check), then `pnpm build` if the build step is what broke.
- **Mobile** (`mobile` job): `cd mobile && flutter analyze && flutter test`. AP has a single `mobile/` Flutter app — there is no twin to mirror.
- **e2e** (`e2e` job): bring up the stack with `pnpm dev:all` then `pnpm seed`, and from `frontend/` run the failing spec: `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium`. (Stack: FastAPI :8000 + SvelteKit :7777 + Postgres/Redis/MinIO; log in as `demo@acme.com` / `demo` at `acme.localhost:7777`.) If you must wipe local state to get a faithful repro and it holds anything beyond standard seed data, **ask before wiping**.

Confirm the failure reproduces *before* the fix and is gone *after*. Capture the evidence (counts, status codes, exit codes) — report it, don't just claim it.

### 5. Apply the fix at the lowest sensible layer

- AP's CI parallelism is by workspace (`backend` / `frontend` / `mobile` / `e2e`), not sharded Playwright. If the same broken pattern appears in a **sibling job** (e.g. a lint rule failing in both `backend` and the e2e setup), fix all of them — don't leave the flake live elsewhere.
- Keep the blast radius proportional: prefer the surgical, version-/behaviour-stable change over a broad upgrade that could destabilise unrelated jobs, unless the broad change is genuinely the root fix.
- Match the file's existing voice; if it documents incidents by run ID, document yours the same way.
- Spin up the `Explore` agent if you need to find every sibling site of the pattern before you fix.

### 6. Sweep docs

If a doc describes the behaviour you changed (a CI job's steps, a command, an env var, a port), update it in the same turn — deferred docs are drift (guard rail 12, docs-as-code; CLAUDE.md § Every change must update docs and tests).

### 7. Commit — then a review pass

- One coherent piece → one **path-scoped** commit, fix + coverage + doc update together: `git commit -m "…" -- <paths>`. Bare `git commit`, `git add -A/.`, `git commit -a`, and whole-tree ops are blocked by `.claude/hooks/git-scope-guard.py` (concurrent sessions share one checkout) — follow the scoped alternative if a command is denied (guard rail 1).
- No `Co-Authored-By` / "Generated with" / AI-attribution trailer in the message — write it as a human would.
- Validate before committing where cheap: `python3 -c "import yaml; yaml.safe_load(open('<workflow>'))"` for workflow YAML, the relevant linter/test for code.
- Consider a `code-reviewer` pass on the diff before you hand back (guard rail 3), especially if the fix touched a money path, auth, tenant isolation, or a webhook.

## Output

End with: the failing job + step + root cause (one or two sentences), the fix and *why it's not a band-aid*, the coverage you added, the local verification evidence (the exact command + its result), and any residual risk worth flagging (e.g. "this is correct only because CI pins ruff X.Y — a version bump would change the assumption"). Keep it tight; the user can read the diff.
