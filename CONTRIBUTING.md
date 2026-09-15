# Contributing

Thanks for considering a contribution. This file describes how to work in this repo.

## Before you start

- Open an issue (or comment on an existing one) describing what you want to change. For non-trivial changes, get rough agreement on the approach before opening a PR — it's easier to redirect a sentence than a 500-line diff.
- Look at recent commits in the area you're touching for style cues.
- Per-workspace dev setup lives next to the code: `backend/CLAUDE.md`, `frontend/CLAUDE.md`, `mobile/CLAUDE.md`. First-time bootstrap is in `docs/getting-started.md`.

## Branching

Work on a feature branch off `main`:

```
git checkout -b feat/<short-slug>      # for features
git checkout -b fix/<short-slug>       # for bug fixes
git checkout -b chore/<short-slug>     # for tooling / housekeeping
git checkout -b docs/<short-slug>      # for docs only
```

Keep branches short-lived. If you're working on something that'll take more than a couple of days, rebase onto `main` regularly to avoid drift.

## Commits

Use conventional-commit-style messages:

```
feat(scope): add the thing
fix(scope): stop the crash on Y
chore(scope): bump dependency Z
docs(scope): clarify the setup steps for X
```

Scope is the area you're touching (e.g. `frontend`, `backend`, `infra`, `auth`, `audit`, etc.). Keep the subject line under 70 characters; put rationale in the body if the change isn't self-evident.

## Tests + docs are part of the change

Per the rule in `CLAUDE.md`: every PR that touches code also touches tests and docs in the same diff. If a change is genuinely untestable (config, pure styling, a one-line constant), say so in the PR description — don't skip silently.

## Running the checks locally

Each workspace has its own toolchain. The fastest way to invoke any of them is via the root `pnpm` dispatch scripts (see `pnpm run` for the full list); the native commands below still work for anyone who prefers them.

```
# Via root pnpm scripts (any directory in the repo)
pnpm lint:backend             # ruff check .
pnpm format:backend:check     # ruff format --check .
pnpm test:backend             # pytest
pnpm lint:frontend            # pnpm check (svelte-check + tsc)
pnpm test:frontend            # Playwright (backend must be up on :8000)
pnpm lint:mobile              # flutter analyze
pnpm test:mobile              # flutter test
pnpm lint                     # all three sequentially
```

```
# Native per-workspace commands (the dispatch scripts above wrap these)
cd backend && ruff check .
cd backend && ruff format --check .
cd backend && pytest
cd frontend && pnpm check
cd frontend && pnpm test:e2e
cd mobile && flutter analyze
cd mobile && flutter test

# Repo-wide
pre-commit run --all-files    # gitleaks + the other hygiene hooks
```

The backend scripts (lint, test, format) assume your backend venv is activated — `source backend/.venv/bin/activate` first, or run the native command from inside an already-activated shell.

Or, if Claude Code is available: `/check` runs the relevant gates against the working diff and reports.

### Guard workflows that only run in CI

Three checks have no local equivalent in the list above, because each needs
either a production build or the PR diff. You can still run them by hand:

| Guard | What it protects | Run locally |
|---|---|---|
| **Web bundle budget** (`web-bundle-budget.yml`) | The frontend is static and served from S3 behind CloudFront, so bundle weight is paid by every cold visit and nothing else in CI notices it. Fails on a total or per-chunk ceiling. | `cd frontend && PUBLIC_API_URL=http://localhost:8000 pnpm build`, then measure `build/**/*.{js,css}` gzipped |
| **Compliance drift** (`compliance-drift.yml`) | A migration adding personal data without the matching DSAR-export / erasure / RoPA update. Nothing fails today — it is only wrong the day a data-subject right is exercised. **Advisory (`warn`) — never fails the build.** | `pnpm check:compliance-drift` (detector) / `pnpm test:compliance` (its tests) |
| **Env isolation** (`env-isolation.yml`) | This repo is public and commits `*.env.development`. Asserts those files still point at the local stack, keep their placeholder secrets, and that no other env file is tracked. | Read the workflow — it is three self-contained shell steps |

Raising a bundle ceiling is a legitimate outcome; the rule is that you append a
dated entry to the change log inside `web-bundle-budget.yml` saying what you
measured and why the growth is warranted. Don't edit the number silently.

## Opening a PR

- Title: same conventional-commit format as commits.
- Description: fill in the `pull_request_template.md`. The "Money / data safety checklist" is there for a reason — even ticking the boxes is a useful prompt to think through each item.
- Mark as **Draft** while CI is still running; flip to ready when checks are green.
- Don't squash on merge unless you're cleaning up a noisy WIP series — preserving meaningful commits in `main` makes `git blame` more useful.

## Reviewing a PR

- Pull the branch locally, run the test suite, exercise the change manually if it's user-visible.
- The `/code-reviewer` agent (in `.claude/agents/`) can produce a first-pass review. Use it as a starting point, not a substitute for human eyes.

## Security findings

If you discover a vulnerability, **do not** open a public issue or PR. Email the maintainer directly — `SECURITY.md` has the contact and the response SLA.
