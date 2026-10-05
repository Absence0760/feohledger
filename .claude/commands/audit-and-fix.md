---
description: Pick one app area (random if none given), audit it for real bugs, fix at the root, and back the fix with as much pytest/Playwright/flutter coverage as the change warrants. Commits scoped.
argument-hint: "[optional area — a path, glob, module, or feature, e.g. backend/app/services/po_matching.py or 'the payment-run execute path']"
---

Deep-audit a single area of FeohLedger, fix the real issues you find, and ship tests with the fix. Unlike `/audit-*` (read-only sweeps that write a review doc) and `/check` (advisory pre-commit gate), this command **lands changes**: fix + tests + scoped commits.

`$ARGUMENTS` is the area to audit. If empty, you pick one (see step 1).

## Operating rules (non-negotiable)

- **Fix the root cause — never mask.** No inflated timeouts, sleeps (`page.waitForTimeout`), retries, loosened assertions, `test.skip` / `xfail`, or swallowed errors to make something pass. If you can't fix it now, surface it explicitly and file a tracked follow-up. (Root `CLAUDE.md` guard rails 4–5; "Fix bugs at the source".)
- **Be honest when there's no bug.** If the area is sound, say so plainly and make the deliverable the *test coverage gap* you closed — do **not** invent a "fix" to justify the command. A no-bug-found result with new tests is a success.
- **Respect tenancy, money, secrets.** Don't bypass tenant isolation (`get_tenant` / `get_tenant_db` — the JWT `org` cross-check is the chokepoint), don't hardcode a tenant DB name, don't type money as `float`, don't drop idempotency on a money write, don't log PII / banking data. If the area is auth, tenant isolation, migrations, the money/payment path, webhook handlers, PII, or approval/RBAC, treat it as **load-bearing** (mandatory review pass in step 5).
- **Docs-as-code.** If you change a behaviour, command, env var, port, or convention, update its docs in the same commit (guard rail 12).
- **Commit each logical unit, path-scoped.** Fix and tests are separate commits. Use `git commit -m "…" -- <paths>` (the scope-guard hook blocks bare/whole-tree commits, `git add -A/.`, and `git commit -a`). No AI/co-author trailer.

## Procedure

### 1. Resolve the area

- **If `$ARGUMENTS` is given:** that's the scope. Resolve it to concrete files (a path, glob, module name, or a feature described in prose — map prose to files first).
- **If `$ARGUMENTS` is empty:** pick one yourself. Favour **self-contained, testable, bug-prone, under-covered** code over breadth. A good heuristic:
  - Map source modules to their dedicated test files: backend `backend/app/services/*.py` + `backend/app/api/*.py` vs `backend/tests/test_*.py`; frontend routes/components vs `frontend/tests-e2e/<area>/*.spec.ts`; mobile `mobile/lib/` vs `mobile/test/`.
  - Rank candidates by: **few/no dedicated tests** × **hot path or recent bug activity** (`git log --oneline -20 <file>`) × **logic density** (not pure config/types).
  - Skip: generated files, pure type/schema-only files, `app/config.py`, Alembic migration files, seed scripts, anything needing live cloud creds, and areas already covered by a recent commit in this session.
  - State your pick and *why* in one line before diving in. Treat "random" as "an area I haven't been steered to" — variety across invocations is the point, so don't keep landing on the same module.

### 2. Audit for real issues

- Use a recon pass to **map the area before judging it**: the data model / call sites / invariants it must hold. For anything non-trivial, spawn an `Explore` agent to map callers, schema, and the invariants — or `repo-security-auditor` / `code-reviewer` for a strict written review. Don't guess at a hot path's contract.
- Hunt for **correctness** bugs first (wrong results, broken invariants, race conditions, inconsistent logic across paths, edge cases: null/empty/overflow/unicode/concurrent), then security/tenancy, then robustness. Stylistic nits are out of scope unless they hide a bug.
- For each candidate finding, **confirm it's real** by tracing the code — read the model/migration/caller across files, don't assert from a single file. For a money path, trace the type end-to-end; for a tenant query, confirm it resolves through `get_tenant_db`; for a webhook, confirm HMAC + dedupe. Discard plausible-but-wrong findings.

### 3. Fix the real issues at the root

- Apply the durable fix. Match surrounding code style, comment density, and idiom (ruff line length 100; SQLAlchemy 2 async; Svelte 5 runes).
- If a quick patch and the durable fix diverge, name the durable fix even if you ship the patch (guard rail 5).
- Keep the fix tightly scoped to the issue — resist refactoring the whole area.

### 4. Add as much coverage as the change warrants

There are three test layers — pick the right one per the change (there is **no** ts/node test layer):

- **Backend logic / DB / HTTP behaviour** → pytest `backend/tests/test_*.py`. Needs `pnpm db:up` (Postgres + Redis + MinIO) for anything touching the DB. Lint with `ruff check .` / `ruff format .` from `backend/`.
- **Frontend user-visible behaviour** → Playwright `frontend/tests-e2e/<area>/*.spec.ts` (no Svelte component unit tests). Needs the full stack + seed (`pnpm dev:all`, `pnpm seed`). Run one spec from `frontend/`: `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium`. Typecheck with `pnpm check`.
- **Mobile behaviour** → `flutter test` in `mobile/`; lint with `flutter analyze`.
- Write tests that **lock in the fixed behaviour and the invariant it restores** (a regression would fail them), plus the obvious edge cases. If the area was sound, still backfill the missing coverage — that's the deliverable.
- If something is genuinely untestable, say *why* rather than skipping silently (guard rail 2).

### 5. Verify + review

- Run the type/lint gate for the layer you touched (`ruff check .` / `pnpm check` / `flutter analyze`) and the **new** tests.
- Run the **nearby existing** tests that exercise the same path to prove no regression — report the pass/fail counts faithfully.
- If the change is **load-bearing** (auth, tenant isolation, migrations, the money/payment path, webhook handlers, PII, approval/RBAC), run the `code-reviewer` agent on the diff — and `repo-security-auditor` for a security-sensitive area — and apply or push back on its findings before committing. For migration work, route through `/safe-migration` / the `migration-coordinator` agent (the change must fan out to every tenant DB).

### 6. Commit (scoped)

- Commit the **fix** and the **tests** as separate path-scoped commits (conventional-commit style, no AI/co-author trailer). If a behaviour/doc changed, the doc edit rides with the commit that caused it.
- If you only added tests (no bug), one `test(...)` commit is fine.

## Report

End with a tight summary:

```
## /audit-and-fix — <area>

**Picked:** <area> — <one-line why> (omit the "why" if the user named it)

**Issue(s) found:** <each real bug: what was wrong + the root-cause fix>  — or "none; area is sound"

**Tests added:** <files + what they lock in (layer: pytest/Playwright/flutter, count)>

**Verification:** <lint/type gate result; new tests N/N; nearby existing suites N/N; review verdict if run>

**Commits:** <hash + subject, one per line>

**Deferred / recommended:** <anything you intentionally didn't do, with the long-term fix named + tracked follow-up — or "nothing outstanding">
```

## Tone

Don't narrate the agent fan-out. Lead with the pick and the verdict. If you found and fixed a real bug, state it plainly; if you didn't, say the area was sound and the coverage was the gap — don't dress up a non-finding.
