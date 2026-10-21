---
description: Open one draft PR for an ad hoc testing session, fix each issue the operator sends into it (each fix in a background subagent), and mark it ready when they say so. `/draft [topic]` opens, `/draft resume <pr>` picks one up again, `/draft ready` finishes.
argument-hint: "[topic] | resume <pr> | ready"
---

The operator is testing the app by hand and will send issues one at a time.
Every fix lands in **one** draft PR; it stays a draft until the operator says
it's ready. Mode is `$ARGUMENTS`:

- empty or a topic → **Open**
- `resume <pr>` → **Resume**
- `ready` → **Ready**

## Open

1. Branch in a worktree off **`origin/main`**, never local `HEAD`: local
   `main` can run ahead of an unpushed `origin` (`worktree.baseRef: "head"` in
   `.claude/settings.json`; `/fix-issues` § The origin/main base rule), and a
   PR built on it would publish the operator's unpushed work. Worktrees live
   under the gitignored `.claude/worktrees/` (CLAUDE.md § Running concurrent
   sessions — use a worktree):
   ```
   git fetch origin main
   git worktree add .claude/worktrees/adhoc-<slug> -b fix/adhoc-<slug> origin/main
   ```
   `<slug>` is the topic, or today's date (`YYYY-MM-DD`) when there is none.
2. GitHub won't open a PR without a commit, so start with an empty one, then
   push (name the branch):
   ```
   git -C .claude/worktrees/adhoc-<slug> commit --allow-empty -m "chore: open the ad hoc testing fixes branch"
   git -C .claude/worktrees/adhoc-<slug> push -u origin fix/adhoc-<slug>
   ```
3. Open the draft: `gh pr create --draft --base main --title "fix: ad hoc
   testing fixes (<topic or date>)"` with `.github/pull_request_template.md`
   as the body: the Summary says it collects fixes from ad hoc testing,
   Changes starts as `_(none yet)_`, and every checklist row stays (unticked
   until a fix earns it).
4. `backend/.venv` and `node_modules` don't carry into a worktree. Bootstrap
   what the session will need **once**, here, so the fix agents don't race to
   do it: `cd .claude/worktrees/adhoc-<slug>/backend && python3 -m venv .venv
   && source .venv/bin/activate && pip install -e ".[dev]"`, and
   `pnpm -C .claude/worktrees/adhoc-<slug>/frontend i`. If tests will need the
   stack, `pnpm db:up` once; the compose services are shared, so no agent
   restarts or resets them.
5. Reply with the PR link and the worktree path, then wait for the first
   issue.

## Resume

`gh pr view <pr> --json headRefName,isDraft,body`. Find its worktree with
`git worktree list`, or add one for the branch
(`git fetch origin <branch>` then
`git worktree add .claude/worktrees/adhoc-<slug> <branch>`) and bootstrap it as
**Open** step 4 does. If it fell behind, merge `origin/main` into it (merge,
never rebase: a rebased branch needs `git push --force`, which
`.claude/settings.json` denies; `main` requires linear history, so the PR
lands as a squash). Carry on from **Each issue**.

## Each issue

The main session stays free to talk: it triages each issue and hands the
fix to a **background subagent** (the Agent tool, `run_in_background`), one
per issue, then goes back to the operator. It never fixes an issue inline,
except a one-line change it has already diagnosed.

For every issue the operator sends, until they say ready:

1. **Triage (main session).** Restate it in one line. Ask only if two
   readings would build different things; otherwise pick the reasonable one
   and say which. Dig only as far as you need to brief the agent: the
   screen or endpoint, the likely files, what you've ruled out. Check
   `docs/known-issues.md` and `docs/followups.md` first: a known defect gets
   its diagnosis passed on, not rediscovered.
2. **Dispatch.** Start a background `general-purpose` subagent (or the
   specialist the area needs, such as `flake-doctor` for a failing e2e spec)
   with a self-contained brief: the issue in the operator's words
   (screenshots described, since the agent can't see them), what you've
   learned, the worktree path and branch, and these rules:
   - Work only in the PR's worktree; never touch `main` or the main checkout.
     Don't pass `isolation: "worktree"`: the fix has to land on this PR's
     branch, not a fresh one.
   - **Reproduce, then fix the cause** (CLAUDE.md § Fix bugs at the source;
     guard rail 4), with a failing test first where one fits. Honour CLAUDE.md
     § Project invariants. Route by what it touches (guard rail 3):
     `/safe-migration` for anything under `backend/alembic/versions/` (it
     fans out to every tenant DB), `/safe-edit` for auth, tenant isolation,
     the money path, webhook handlers or PII, plus the `money-path-reviewer`
     agent when the money path moves and `ui-reviewer` for a screen.
   - **Tests and docs in the same commit** (CLAUDE.md § Every change must
     update docs and tests; guard rail 12). Run only what the change reaches,
     plus a grep sweep of the callers of anything shared it touched, never a
     full suite: `cd backend && source .venv/bin/activate && pytest
     tests/<file>.py` and `ruff check <paths>`; `pnpm -C frontend check` and
     `pnpm -C frontend test:unit <file>`; `pnpm -C frontend exec playwright
     test --config=tests-e2e/playwright.config.ts <spec>`; `cd mobile &&
     flutter test test/<file>` and `flutter analyze`. A worktree isolates
     files, not ports: run e2e on this worktree's own stack, never a dev
     server another checkout left on `:7777` (CLAUDE.md § Running concurrent
     sessions — use a worktree).
   - **One path-scoped commit**:
     `git commit -m "fix(<area>): <what>" -- <paths>`, then
     `git push -u origin <branch>` (name the branch). The
     `.claude/hooks/git-scope-guard.py` hook refuses whole-tree git; never
     `--no-verify` or `--force` (both denied). No `Co-Authored-By` or
     generated-by trailer. If git reports `index.lock` or the push is
     rejected because another agent pushed first, wait,
     `git pull --no-rebase origin <branch>`, and try again.
   - Don't edit the PR body; report back the cause, the fix, the commit
     hash, the files and the commands run, or why it stopped.
   - If the fix needs a product decision, a real credential or an operator
     action (a Terraform apply, a production data change), stop and report
     rather than guess.
3. **Run agents in parallel when their issues touch different areas**; when
   two would edit the same files, queue the second until the first reports.
   The scope guard is path-granular, so two agents editing one file in the
   shared worktree would commit each other's edits (guard rail 13). Tell the
   operator in one line what was dispatched, then take the next issue.
4. **When an agent reports (main session):** check its commit is on the
   branch (`git log origin/<branch>`) and its diff is what was asked. Add
   one bullet under the PR body's Changes (`gh pr edit <pr> --body-file …`;
   only the main session edits the body, so updates don't race), tick the
   Surface touched boxes and any Money / data safety rows the fix now
   covers. Then tell the operator in two or three lines: the cause, the fix,
   the commit. Relay a stop or a question as it is.

If an issue turns out too large or risky for this PR (a tenant migration with
a backfill, a product call), say so and offer a separate issue or PR rather
than folding it in. Don't let it drop (guard rail 6: a deferral goes to
`docs/followups.md`, re-derive its count with `pnpm check:followups`, plus a
GitHub issue if it warrants one; confirm before creating it).

Don't watch CI after every push. Glance at `gh pr checks <pr>` between issues
and dispatch a fix for a red job this PR caused (`/fix-ci`'s rules: no
retry, timeout or skip band-aids).

## Ready

Only when the operator says so:

1. Wait for every dispatched agent to report, then merge `origin/main` if
   the branch fell behind (as in **Resume**).
2. `pnpm lint` (ruff, svelte-check, the e2e typecheck, flutter analyze),
   plus the targeted pytest, vitest, Playwright and flutter files for
   everything the PR touched. CI runs the sharded full suites.
3. `/check` over the whole branch (`git diff origin/main...HEAD`). Apply the
   findings you agree with (dispatched like any other fix); say why for the
   rest in the PR body.
4. Make the title name what the fixes are about, as a conventional commit
   (`fix(<scope>): <subject>`: subject lowercase, no trailing period, 70
   characters or fewer; `.github/workflows/pr-title-lint.yml`). Use no scope
   when the fixes span areas.
5. `gh pr ready <pr>`, then drive it to green: the one required check is
   `CI gate`, and a red job goes through `/fix-ci` (`/flake-doctor` for a
   flaky e2e spec). Read the open code-scanning alerts on the PR
   (`gh api "repos/{owner}/{repo}/code-scanning/alerts?ref=refs/pull/<pr>/merge&state=open"`;
   CodeQL runs in `.github/workflows/security.yml`, called from `ci.yml`, so a
   red CodeQL job also fails `CI gate`) and fix or justify each.
   Resolve every review conversation, since `main` requires it. Don't merge;
   that's the operator's call.
6. Report the link, the list of fixes, and anything the operator needs to
   decide.
