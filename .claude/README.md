# .claude/

Project-scoped sub-agents, slash commands, and hooks. Checked into git so every contributor (and every future Claude session) gets the same review surface.

## Sub-agents

Grouped by team, one folder each under `agents/` (Claude Code discovers agents
recursively, so the folder is organisation only — an agent is still invoked by
its `name:`). The session doing the work builds; agents review, audit, or
produce one well-defined artefact. Most are read-only; the exceptions are
marked **edits**.

### `engineering/` — the per-change gate

| Agent | What it does |
|---|---|
| [`code-reviewer`](agents/engineering/code-reviewer.md) | Reviews the working diff against root `CLAUDE.md`, the per-area `CLAUDE.md` files and the project invariants. `CLEAN` / `NEEDS_CHANGES` with file:line findings. Run by `/check`, `/safe-edit`. |
| [`money-path-reviewer`](agents/engineering/money-path-reviewer.md) | Domain reviewer for anything that decides whether, how much, in which currency and by whose authority money moves — state machine, SoD sets, payment runs / settlement / void, credit memos, FX labelling, PO matching, payment-blocking exceptions. Cites `docs/decisions.md`. Run by `/check` and `/safe-edit` when the diff reaches the money path. |
| [`test-gap-checker`](agents/engineering/test-gap-checker.md) | Maps each changed source file to the pytest / vitest / Playwright / flutter test it should ship with. |
| [`doc-hygiene-checker`](agents/engineering/doc-hygiene-checker.md) | Maps the diff to the docs (README, `docs/*`, per-area `docs/`, `CLAUDE.md` files) it invalidated. |
| [`migration-coordinator`](agents/engineering/migration-coordinator.md) | For an Alembic revision: chain, id length, control-plane-vs-tenant gate, idempotent DDL, local apply + round-trip, model ↔ migration parity (fresh tenants use `create_all`), and the hand-synced layers (models, schemas, `frontend/src/lib/types`, `mobile/lib/models`). Run by `/safe-migration`, `/check`. |
| [`flake-doctor`](agents/engineering/flake-doctor.md) | **Edits.** Reproduces, root-causes and source-fixes a flaky/failing Playwright spec (14-shard CI, per-worker `e2e<N>` tenants). Never masks. Run by `/flake-doctor`. |

### `design/` — screens

| Agent | What it does |
|---|---|
| [`ui-polisher`](agents/design/ui-polisher.md) | **Edits.** Rebuilds one SvelteKit page / component to `frontend/docs/ui-patterns.md`. Doesn't commit. `/polish-ui <route>`. |
| [`mobile-ui-polisher`](agents/design/mobile-ui-polisher.md) | **Edits.** Same for one Flutter screen / widget against `mobile/CLAUDE.md`. `/polish-ui mobile:<screen>`. |
| [`ui-reviewer`](agents/design/ui-reviewer.md) | Review mode for both front ends: nothing lost, money / dates / strings localised, design-system fit, WCAG 2.2 AA, tests. `/polish-ui review <target>`, and `/check` when the diff touches UI. |

### `audit/` — periodic sweeps

| Agent | What it does |
|---|---|
| [`repo-security-auditor`](agents/audit/repo-security-auditor.md) | Trust boundaries — tenant isolation, auth, money path, secrets, PII. Its "Known bug shapes" section is the institutional memory; append to it when a regression is fixed. Pass the area as the prompt's first sentence. `/audit-security`, `/audit/*`. |
| [`compliance-auditor`](agents/audit/compliance-auditor.md) | Privacy / data-protection posture: DSAR export + erasure, retention, sub-processors, cookies, regional availability, accessibility. `/audit/gdpr` and siblings. |

### `personas/` — bug-hunting points of view

Seventeen `persona-*` agents, each walking the app as one kind of user and
writing `reviews/<persona>.md`. Run with `/persona`; protocol and how to add one
in [`personas/README.md`](personas/README.md).

### `legal/`

| Agent | What it does |
|---|---|
| [`saas-legal-doc-reviewer`](agents/legal/saas-legal-doc-reviewer.md) | Pre-counsel review of the published legal pages (`frontend/src/routes/legal/`). Not legal advice. |

### Adding or changing an agent

- Put it in the team folder it belongs to; a new team gets a folder once it has more than one member.
- Name real paths, commands and rules, and cite where each rule lives (`CLAUDE.md` guard rail N, `docs/<file>.md` § …). Don't copy a rule's reasoning into the agent — point at the doc. A placeholder path is a bug.
- Say whether it is read-only, and what it writes if not.
- Update this file, and any command that invokes it.

## Slash commands

| Command | What it does |
|---|---|
| [`commands/check.md`](commands/check.md) | Pre-commit gate. Spawns `code-reviewer` + `test-gap-checker` + `doc-hygiene-checker` in parallel against the working diff and aggregates findings. Advisory only. Use before every non-trivial commit. |
| [`commands/safe-edit.md`](commands/safe-edit.md) | Implements `<task>` with a coder ↔ reviewer loop (max 2 review cycles). Costs 2-3x a normal edit. Use for money-path / tenant-isolation / auth / migration / webhook changes. |
| [`commands/audit-security.md`](commands/audit-security.md) | On-demand security audit. Invokes `repo-security-auditor` against a focus area (tenant isolation, money path, webhooks, secrets, PII, migrations, infra). Heavier than `/check`. |
| [`commands/audit-webhooks.md`](commands/audit-webhooks.md) | Focused audit of every inbound webhook handler against invariant #9 (HMAC verification + event dedup + silent rejection). |
| [`commands/audit-money-path.md`](commands/audit-money-path.md) | Focused audit of every money-moving path against invariants #1 (Decimal/Numeric), #2 (idempotency), #3 (append-only audit trail). |
| [`commands/bug-hunt.md`](commands/bug-hunt.md) | Go wide for real correctness bugs across an area (or self-selected high-yield targets), reproduce each with a probe, fix at the root, lock with a regression test, sweep siblings. Lands fixes; multi-round; commits scoped. |
| [`commands/audit-and-fix.md`](commands/audit-and-fix.md) | Deep-audit **one** named area, fix the real issues at the root, and ship tests with the fix. The fix-and-land counterpart to the read-only `/audit-*` sweeps. |
| [`commands/perf-hunt.md`](commands/perf-hunt.md) | Hunt real performance problems (N+1, missing indexes, recompute storms, oversized payloads, render thrash). Measure before/after, fix the root cause, guard the structural win. New indexes go through `/safe-migration` fan-out. |
| [`commands/ux-hunt.md`](commands/ux-hunt.md) | Drive the SvelteKit app like a user; fix objective interaction defects (dead-ends, URL-state round-trip, empty/loading/error states, keyboard traps, invalid-transition controls) with a failing-then-passing e2e. Reports the subjective calls. |
| [`commands/coverage-hunt.md`](commands/coverage-hunt.md) | Proactively backfill tests for behaviour that works but isn't tested — area-scoped, no bug required. The build-side counterpart to the diff-scoped `test-gap-checker`. |
| [`commands/fix-ci.md`](commands/fix-ci.md) | Fix a failing GitHub Actions CI job (backend / frontend / mobile / e2e). Root-causes, reproduces locally with the same command CI used, fixes at source, lands coverage. No retry/timeout/skip band-aids. |
| [`commands/flake-doctor.md`](commands/flake-doctor.md) | Triage and source-fix a flaky/failing Playwright e2e spec via the `flake-doctor` agent. Never masks a flake with sleeps, retries, or inflated timeouts. |
| [`commands/endpoint-inventory.md`](commands/endpoint-inventory.md) | Generator (read-only): writes `reviews/endpoint-inventory.md` — a canonical table of every FastAPI route (method / path / auth / tenant-scope / params / response) read from `backend/app/main.py` + `app/api/`. Feeds integrators and `/audit/auth`. |

## Hooks

| Hook | When it runs | What it does |
|---|---|---|
| [`hooks/git-scope-guard.py`](hooks/git-scope-guard.py) | PreToolUse on `Bash` | **Multi-session safety.** Concurrent Claude sessions share one checkout, so any whole-tree git op sweeps up another session's in-flight edits. This guard denies the unscoped commands (`git add -A`/`.`/`-u`, bare `git commit`, `git commit -a`/`--amend` with staged changes, `git stash` without `-- <path>`, `git stash clear`, `git reset --hard`, `git checkout`/`restore .`, `git rm .`, `git clean -f`) and points each denial at the path-scoped alternative. Path-scoped ops (`git add <path>`, `git commit -m "…" -- <path>`) pass through untouched. Covered by [`hooks/git-scope-guard.test.py`](hooks/git-scope-guard.test.py). |
| [`hooks/security-patterns.sh`](hooks/security-patterns.sh) | PostToolUse on `Edit` / `Write` / `MultiEdit` | Grep-based pattern checks for security regressions. Catches the textually-stable bug classes (bcrypt scheme, naive datetime, raw filename interpolation, exception-in-log, jwt.decode outside the central helper, direct status assignment, Float on money column, secret-shaped response fields, raw fetch in Svelte components, console.log in committed source, TODO without owner). Each rule names the bug class it prevents and the safer alternative. Bypass with `# noqa: <rule-name>` on the line with a rationale. |

Wired in [`settings.json`](settings.json). `security-patterns.sh` exits 2 on a finding so stderr surfaces as a system-reminder for the next turn; `git-scope-guard.py` emits a `deny` decision so the racy command never runs.

`settings.json` also carries a `permissions` block: an **allow** list that pre-approves the safe, high-frequency dev commands (read-only git/gh, `pnpm`/`pytest`/`ruff`/`flutter` test+lint) so routine work doesn't stall on prompts, and a **deny** list that hard-blocks the shared-checkout footguns the scope guard's path-matching can't always see — `git commit --no-verify` (skips the project's git hooks), `git push --force`/`-f`, `git reset --hard` — plus the usual `sudo` / `rm -rf /` / `curl | sh` blanket denies.

## Where to reach in which order

| Layer | What it catches | Cost |
|---|---|---|
| `hooks/security-patterns.sh` | Stable textual shapes — caught on every Edit, before the next turn | ~50ms per edit |
| `/check` | Diff-level review against documented conventions | ~30s |
| `/audit-security` | Trust-boundary sweep across the area named | ~1–2min |
| `/audit-webhooks` | Four-question gate on every webhook handler | ~1min |
| `/audit-money-path` | Three-invariant gate on every money-moving path | ~1min |
| `/safe-edit` | Coder ↔ reviewer loop for high-blast-radius changes | 2-3x normal edit |

Daily floor: hooks + `/check` on every PR. Per-PR gate: `/audit-security` for security-sensitive changes. Hard cases: `/safe-edit` for money path / migrations / auth changes.

## What lives here vs. what doesn't

- **In `.claude/`**: agent and command definitions, hooks, and project-scoped `settings.json`. Useful to every contributor and every Claude session against this repo.
- **Not in `.claude/`**: `settings.local.json` (per-user), runtime locks (`*.lock`), or anything user-specific. The repo `.gitignore` keeps those out.

## Adding a new rule to the hook

Each rule in `hooks/security-patterns.sh` is a block with this shape:

```bash
# ----- RULE: <stable-name> -------------------------------------------
# Why: <one paragraph — what bug class does this prevent? Cite a real
# incident if there's one in the repo's history.>
while IFS= read -r m; do
  ln="${m%%:*}"
  register "<rule-name>" "$ln" \
    "<short why for the in-line report>" \
    "<the safer alternative; usually 'import X from app.utils.Y'>"
done < <(hits '<grep -E pattern>')
```

Rules MUST:
- Have a stable name that's used in `# noqa: <name>` bypasses.
- Match a pattern that's both detectable and meaningful — loose enough to catch future bugs of the same class, tight enough that the false-positive rate is < 10%.
- Suggest a concrete fix — never just "this is bad."
- Be scoped to the file types where the pattern is meaningful (Python-only rules go inside the Python `if`).

## Adapting these as the project grows

The agents cite concrete file paths. As the codebase shifts, edit the agents to:

- Cite the actual file paths for the auth middleware, tenant-scoping helper, migration directory, test directory.
- Replace generic invariants with the concrete library / type / column type the project chose.
- Add project-specific invariants as new ADRs get written (the project's ADRs live under `docs/` once they exist).
- Append new "Known bug shapes" entries to `repo-security-auditor.md` whenever a real regression gets fixed — that section is the institutional memory.

The pattern is "keep the framework, swap in the specifics." Don't rewrite from scratch each time the stack shifts.
