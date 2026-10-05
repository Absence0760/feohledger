---
description: Ship one meaningful improvement to an area of the app — in path-scoped per-piece commits with tests + docs — then run an independent code-reviewer audit and fix every finding until clean. The "do another round" loop.
argument-hint: [area or feature — optional; omit to let the model pick a high-value target]
---

Pick (or take) one area of the app, ship a genuinely useful improvement to it,
then audit your own work with the `code-reviewer` agent and fix what it finds.
Target: `$ARGUMENTS` (if empty, survey for a high-value target and propose it
before building).

This is the repeatable "do another round of this" loop:
**improve → commit per piece → audit → fix → re-audit → report.**

It is the *proactive* counterpart to the hunt commands. `/bug-hunt`,
`/ux-hunt`, `/perf-hunt` and `/coverage-hunt` all start from "something is
wrong"; this one starts from "this could be better" and requires you to justify
that the gap is real before building.

## When to use this command

**Right fit:**
- "Do another round" / "improve some area of the app" with latitude to choose.
- A specific area the user named that has a real gap, a missing interconnection
  between features, or a shipped-but-half-finished surface.
- Anywhere a small, self-contained, verifiable improvement plus a regression
  guard raises quality.

**Wrong fit — push back instead of running:**
- A large, uncertain feature needing a product decision first (use
  `AskUserQuestion` / plan mode; don't free-run).
- A known bug with a known fix — just fix it, or use `/bug-hunt`. This loop is
  for *improvements you scope yourself*.
- A schema change — use `/safe-migration`, which puts `migration-coordinator`
  in the loop. Migrations fan out to every tenant DB; that is not this command's
  risk profile.
- Trivial edits (typos, dep bumps).

## Principles (the bar these rounds are held to)

- **Real gap, not churn.** Pick something with user value: a missing signal, two
  features that should talk but don't, or a surface inconsistent with the rest
  of the app. **Confirm the gap is real by reading the code before building** —
  this codebase is large and much of what looks missing already ships. Check
  `docs/roadmap_shipped.md` and `backend/docs/` first; a "new feature" that
  duplicates an existing one is the most expensive possible outcome here.
- **Read `docs/decisions.md` before proposing a design.** If the shape you're
  reaching for was already considered and rejected, the entry says why. Don't
  re-litigate it silently; if you think the decision should change, say so
  explicitly and make that the conversation.
- **Recommend the long-term solution and do it fully** (guard rails 4–5). No
  band-aids; fix the root cause and extract the reusable piece when there is one.
- **Honour the project invariants** (root `CLAUDE.md` § Project invariants).
  Money is `Decimal`/`Numeric`; money-moving writes are idempotent; status
  changes write audit rows; tenant isolation is enforced at the data layer;
  auth before everything; no hardcoded secret fallbacks; PII stays out of logs
  and error bodies; webhooks verify signatures and dedupe. A round that
  violates one of these is worse than no round.
- **Local-first** (guard rail 7). If the improvement touches an external
  service, it ships with a `mock` adapter and a safe committed default in the
  *same* change. `pnpm dev` must never come to require a real credential.
- **Pin every fix with a test** so it can't regress — pytest for backend logic,
  Playwright for a UI path, `flutter test` for mobile, or a source-scan guard
  for a class of mistake. This covers **any bug surfaced while building**, not
  just the improvement: if the round uncovers a latent defect, fix it at the
  root and pin it in its own commit. A fix with no test is not done.

## The loop

### 1. Choose the target (if `$ARGUMENTS` is empty or vague)

Survey for a high-value, bounded improvement. Read the relevant code to confirm
the gap is real. State the chosen target + why in one or two sentences, then
build. If the best target needs a product call, surface it first rather than
guessing.

Good hunting grounds: `docs/roadmap.md` (open work only — 11 sections, each with
an `**Open:**` line), `docs/followups.md` (deferred items with their trigger),
and any surface where the backend ships an endpoint the frontend never calls.

### 2. Decompose into pieces and build, committing as you go

Per root `CLAUDE.md` § Git workflow, each discrete piece is its own commit, with
its tests in the **same** commit as the code:

- **Backend logic** → prefer a pure function in `app/services/` with a unit
  test, then wire it into the router. Money as `Decimal`; serialize as exact
  strings, never `float`.
- **New router** → `require_roles` / `require_permission` on every endpoint, and
  add it to `tests/test_rbac.py` (the coverage gate fails otherwise). Public-by-
  design routes go in `PUBLIC_BY_DESIGN` or `ALTERNATE_AUTH` with a reason.
- **Frontend** → build from `frontend/src/lib/components/` (Svelte 5 runes
  only). Extract a component the second time you'd duplicate markup. New
  user-facing strings need an `en` key plus every other locale — the
  `messages_parity` vitest enforces it.
- **Mobile** → mirror into the widget library, not per-screen copies.
- **Adapters** → copy `mock_adapter.py`, implement the interface, register with
  the decorator. Fail closed without a credential; never fall back to a default.
- **Docs** → update in the same turn (guard rail 12): the per-area `CLAUDE.md`,
  the matching `backend/docs/*.md`, `docs/roadmap.md` (and move the section to
  `roadmap_shipped.md` if this closed its last open item), and `docs/decisions.md`
  if you made a non-obvious trade-off.

**Commit discipline:**
- Always path-scoped: `git commit -m "…" -- path1 path2 …`. The
  `git-scope-guard.py` hook blocks bare `git commit` / `git add -A`.
- One piece = one commit. `git status` before each; confirm every path is yours.
- No AI attribution / `Co-Authored-By` / robot footer.

### 3. Verify each piece before moving on

Run the cheapest sufficient check: `pytest tests/test_<area>.py` for backend,
`pnpm check` for frontend types, the relevant Playwright spec for a UI path,
`pnpm test:unit` for i18n parity, `flutter test` for mobile. Backend tests
needing a real DB want `pnpm db:up` first. **Don't declare a piece done on an
unrun test.**

### 4. Audit the round with `code-reviewer`

When the build is committed, run the `code-reviewer` agent against your commit
**range** (not the working tree — it's already committed):

> "Review the diff of the last N commits on `main` (`git diff HEAD~N..HEAD`).
> <one-line description of what the round did>. Review for real correctness bugs
> and project-convention violations (the root CLAUDE.md project invariants,
> `docs/decisions.md` entries, fail-closed defaults, tenant isolation, money as
> Decimal, audit-row coverage, PII in logs, i18n parity, comment + abstraction
> discipline). Report concrete diff-level findings with file:line + recommended
> fix. Do not edit."

### 5. Fix every finding — but verify the finding first

For each finding:
- **Confirm it's real before acting.** If it makes a numeric or behavioural
  claim (a threshold, an off-by-one, "this endpoint isn't gated"), check it
  yourself. Agents report plausible-but-wrong findings, and "fixing" one can
  introduce a real bug.
- If real, fix the **root cause**, and if the same mistake exists elsewhere (a
  copied pattern), fix those instances in the same turn — don't leave the broken
  pattern to be recopied.
- If the finding is wrong, say *why* you're not applying it; don't silently skip.
- Pin the fix with a test, then commit it path-scoped as its own `fix(...)`
  commit.

### 6. Re-audit if the fixes were non-trivial; cap at 2 cycles

If step 5 changed real logic, re-run `code-reviewer` on the new commits. Stop
after the second cycle even if minor nits remain — report them instead of
looping.

### 7. Report

Short summary: what the round improved and why it mattered, the audit findings
and how each was resolved (or why dismissed), what's verified (which tests ran
and passed), and anything pre-existing you surfaced but didn't fix — which goes
to `docs/followups.md` or `docs/known-issues.md` per guard rail 6, not just into
the chat. End with a one-line offer to run another round or pick a different
area.

## Tone

- Don't narrate every command. The user reads the diffs.
- Be honest about scope: name what you deliberately deferred (and where it's
  tracked) versus what you finished.
- Keep the end-of-turn summary short — let the commits speak.
