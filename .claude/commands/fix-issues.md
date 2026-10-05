---
description: Fix N open GitHub issues in parallel — one worktree-isolated agent per issue, each fixing at the root with tests + docs per the project invariants, then opening a PR. Defaults to 5 issues / 5 agents. Pushes fix branches only, never main.
argument-hint: "[count | issue numbers | label filter] [single-pr] — e.g. \"5\", \"#118 #133\", \"label:bug 5\", \"single-pr\". Omit to fix 5 open issues, one PR each."
---

Fix open GitHub issues in this repo using **parallel agents — one per issue** — then open a PR for the work. Scope: `$ARGUMENTS` (if empty, fix **5** open issues, each in its own PR).

This is a **fan-out fix** loop: **select → confirm → fan out (one worktree per issue) → each agent fixes + tests + PRs → collect → report.**

## Why worktrees are mandatory here

The scope-guard is path-granular, not hunk-granular ([CLAUDE.md § Running concurrent sessions](../../CLAUDE.md)): multiple agents editing the same file in one shared checkout would silently capture each other's edits in path-scoped commits. So **every fixer agent MUST run with `isolation: "worktree"`** — its own tree, its own index, its own branch. Do not fan out file-editing agents into the shared checkout.

## The origin/main base rule (this repo's critical twist)

This repo's local `main` runs **ahead of an unpushed `origin`**, and `.claude/settings.json` sets `worktree.baseRef: "head"` — so a fresh worktree starts from local HEAD, which contains commits that are **not published**. A PR branched from there would publish the operator's unpushed work, which is never this command's call. Therefore every fixer agent's **first git action** inside its worktree is to re-root onto the remote:

```
git fetch origin && git checkout -B fix/issue-<N> origin/main
```

The PR diff must contain **only the fix**. Relatedly, at selection time check `git log origin/main..main --oneline`: if an issue is already fixed by an unpushed local commit, **drop it** and tell the operator the fix exists and just needs pushing — don't re-fix it against origin/main.

## When to use this command

**Right fit:**
- "Burn down the issue backlog" — several independent, well-scoped, code-fixable issues (e.g. the bug-hunt backlog issues).
- Issues that are concrete bugs or small enhancements with an obvious root-cause fix.

**Wrong fit — don't run (or drop the issue from the batch):**
- Issues needing a **product decision** first.
- Issues that are **discussions / questions / duplicates** with no code change.
- One large issue that is really a feature epic — use `/safe-edit` on it directly.

## The loop

### 1. Select the issues (orchestrator — not parallel)

Parse `$ARGUMENTS`:
- A bare number (`5`) → how many issues to fix (default **5**).
- Explicit issue numbers (`#118 #133 …`) → fix exactly those.
- A `label:` filter (`label:bug`) → restrict the candidate pool.
- `single-pr` anywhere → consolidate all fixes into **one** PR instead of one-per-issue (see step 5).

List candidates:

```
gh issue list --state open --limit 40 --json number,title,labels,body
```

**Then exclude any issue that already has a fixing PR** — this is mandatory, and it is what keeps a re-run (or a run alongside other sessions) from duplicating or re-fixing work. `gh issue list` does NOT show linked PRs, so query them explicitly. The authoritative signal is GitHub's **`closedByPullRequestsReferences`** (the PRs whose "Fixes/Closes #N" keyword links them to the issue) — NOT a bare cross-reference (a PR that merely *mentions* the issue in passing):

```
gh api graphql -f query='
query($owner:String!,$repo:String!){
  repository(owner:$owner,name:$repo){
    issues(states:OPEN, first:40, orderBy:{field:CREATED_AT, direction:DESC}){
      nodes{
        number
        closedByPullRequestsReferences(first:5, includeClosedPrs:true){
          nodes{ number state url }
        }
      }
    }
  }
}' -f owner=<OWNER> -f repo=<REPO>
```

Derive `<OWNER>`/`<REPO>` from `gh repo view --json owner,name`. Then apply the filter:

- **Skip** an issue if any linked PR's `state` is **`OPEN`** (a fix is in flight — e.g. a previous run of this command, or another session) **or `MERGED`** (already fixed; the issue just wasn't auto-closed). Re-fixing either duplicates work.
- **Skip** an issue already fixed by an **unpushed local commit** (`git log origin/main..main`) — report it as "fixed locally, needs push" instead.
- **Do NOT skip** on a PR whose only linked state is **`CLOSED`** (an abandoned fix attempt) — that issue is fair game again. Say in the report that you're re-attempting it and why.
- **Do NOT skip** on a bare cross-reference / mention alone (a PR saying "related to #N" without a closing keyword). That's why the query uses `closedByPullRequestsReferences`, not `timelineItems(CROSS_REFERENCED_EVENT)`.
- If the user passed **explicit issue numbers**, still run this check and warn (don't silently proceed) when one already has an open/merged fixing PR or a local unpushed fix — the user may not realise it's already handled; let them confirm before you duplicate it.

From the survivors, choose issues that pass the **actionable bar**: a concrete, bounded, code-level fix with a clear root cause; not blocked on a product call; not a duplicate/discussion. Prefer `bug` over `enhancement` when choosing freely. Read each candidate's body — skip anything whose "fix" is really "decide what we want."

If fewer than the requested count survive both filters, take what qualifies and say so — don't pad the batch with issues that need a decision or already have a fix in flight.

### 2. Confirm before spending (checkpoint)

Opening PRs is an outward-facing action. This command pushes the fix branches only (never `main`, never `--force`). Before fanning out, print the selected issue numbers + titles + the one-line fix intent for each, and the PR strategy (one-per-issue vs `single-pr`), and get a go-ahead. If the user invoked the command with explicit issue numbers, treat that as the go-ahead and proceed.

If tests will need the local stack, run `pnpm db:up` **once from the orchestrator** before fanning out — the compose services (Postgres/Redis/MinIO) are shared by every worktree; agents must not restart them.

### 3. Fan out — one worktree-isolated agent per issue (in a single message)

Spawn all fixer agents in one message so they run concurrently. Each is a `general-purpose` agent with `isolation: "worktree"`, scoped to **exactly one issue**. Give each agent this contract:

> You are fixing GitHub issue **#N** in an isolated git worktree of FeohLedger. Work only on this issue.
>
> 1. **Re-root first:** `git fetch origin && git checkout -B fix/issue-N origin/main`. Your worktree starts from local HEAD, which is AHEAD of the published origin — your PR must build on `origin/main` only, never on unpushed local commits.
> 2. **Bootstrap what you need:** `backend/.venv` and `node_modules` do not carry into a worktree. Backend tests → `cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`. Frontend → `pnpm -C frontend i`. Compose services are already up and shared — do not run `docker compose down`/`services:reset`.
> 3. **Reproduce / locate.** Read the issue body. Find the root cause in the code — cite `file:line`. If you cannot reproduce or the fix needs a product decision, stop and report that instead of guessing.
> 4. **Fix at the root.** No band-aids: no inflated timeouts, sleeps, retries, loosened assertions, or swallowed errors (CLAUDE.md § Fix bugs at the source). If the durable fix and a quick patch diverge, ship the durable one.
> 5. **Honor the project invariants** (root CLAUDE.md § Project invariants): money is `Decimal`/`Numeric` never float; idempotency on writes that move money; status changes write append-only audit rows; tenant isolation via `get_tenant`/`get_tenant_db` only (never hardcode `feoh_<slug>`); auth + RBAC/`require_permission` before everything; secrets via sops with no hardcoded fallback; PII/banking data out of logs and error bodies; webhooks verify HMAC + dedupe by event id; passwords via the shared `bcrypt_sha256` context. Schema changes are Alembic migrations that fan out to every tenant DB — run those through the `/safe-migration` rigor. Frontend: Svelte 5 runes, all fetches through `src/lib/api.ts`, reuse `src/lib/components/`.
> 6. **Tests in the SAME commit as the fix** — pytest for backend, Playwright (`frontend/tests-e2e/`) or vitest for frontend, `flutter test` for mobile: a pinning test that fails before and passes after. A fix with no test is not done; if genuinely untestable, say why. Run the narrowest relevant selection — the DB-backed tests share one Postgres with the other agents, so don't run the full suite concurrently.
> 7. **Docs-as-code:** update whatever the change touches (backend/docs/, docs/, the relevant CLAUDE.md) in the same commit.
> 8. **Commit path-scoped, per piece** (`git commit -m "…" -- <paths>`; the git-scope-guard hook blocks whole-tree commits); conventional-commit messages; **no AI attribution of any kind** (no Co-Authored-By, no "Generated with" footer).
> 9. Run the relevant checks (`pytest <targets>`, `ruff check`, `pnpm -C frontend check`, `flutter analyze` — whichever apply) and report pass/fail honestly — do not claim green you didn't see.
> 10. **Report back:** branch name, the commits, files touched, test result, and a proposed PR title (conventional-commit format `type(scope): subject` — the `pr-title-lint` workflow rejects anything else) + body ending with `Fixes #N`. **Do not push** — the orchestrator pushes after verification.

Keep each agent's scope to its one issue so it reads deeply and doesn't wander.

### 4. Verify each agent's work before it becomes a PR

Don't trust a "done" — for each returned fix, sanity-check: the branch is rooted on `origin/main` (`git merge-base --is-ancestor` of the branch's parent), the cited root cause is real, the test genuinely fails-before/passes-after, the docs obligation was met, and no unrelated files were swept in. For a fix touching auth, tenant isolation, migrations, the money path, webhook handlers, or PII (guard rail 3), run the `code-reviewer` agent over the branch diff before shipping it. Bounce anything that swallowed a failure or papered over the bug back to the agent (via `SendMessage` to keep its worktree context) rather than shipping it.

### 5. Open the PR(s)

Pushing the fix branch is authorized by the user invoking this command — never `--force`, never `main`.

**Re-check `closedByPullRequestsReferences` for the issue immediately before opening its PR.** The fan-out takes minutes; another session may have opened a fixing PR in that window. If one appeared, don't open a duplicate — report the collision and drop the fix (or, if yours is clearly better, link both and let the user decide).

- **Default — one PR per issue** (recommended; matches branch protection + the PR-title lint's one-`type(scope)` rule, and keeps unrelated changes independently reviewable/revertable):
  ```
  git push -u origin fix/issue-<N>
  gh pr create --title "<conventional title>" --body "$(printf '…\n\nFixes #N')" --base main
  ```
- **`single-pr` mode** — create one integration branch off `origin/main` and **cherry-pick or rebase each worktree branch's commits onto it sequentially** (no merge commits — `main` requires linear history, and a linear PR branch keeps rebase-merge available), resolve any conflicts, push, and open **one** PR whose body lists `Fixes #N1`, `Fixes #N2`, … for every issue in the batch. Pick the dominant `type` and a representative `scope` for the title. Use this only when the user asked for it — a five-issue PR spanning unrelated areas is harder to review and to revert.

Each PR must pass the single required **CI gate** check; report if any open red. No PR body attribution footer.

### 6. Report

One compact table: issue # → PR URL → test result → CI status. Note any issue that was dropped (not actionable / already fixed locally / fix in flight) and why. Clean up finished worktrees (`git worktree remove`) unless a fix is still being iterated; local `fix/issue-*` branches can stay until their PR merges (the SessionStart unmerged-branch warning will name them — that's expected while PRs are open, not stranded work).

## Guardrails

- **Never re-fix an issue that already has an open or merged fixing PR** (step 1's `closedByPullRequestsReferences` filter, re-checked at step 5) **or an unpushed local fix**. This is the anti-duplication invariant — it must survive re-runs and concurrent sessions.
- **PR branches build on `origin/main` only.** Never publish the operator's unpushed local commits.
- **Fixes only what the issues describe.** No scope creep, no drive-by refactors bundled in.
- **Never merge the PRs** — opening them is the deliverable; merge is the user's call (as is deleting the remote branch afterward — the repo doesn't auto-delete).
- **A dropped issue is a fine outcome.** Reporting "3 of 5 were actionable; the other 2 need a product decision" is better than shipping two guesses.
- If a fix touches a security-sensitive / schema / state-machine surface, run it through the `/safe-edit` rigor (coder↔`code-reviewer` loop) rather than a single pass.
