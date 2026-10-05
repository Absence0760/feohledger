---
description: Go wide hunting for real correctness bugs across FeohLedger — reproduce each with a probe, confirm it's real, fix at the root, lock it with a regression test, then sweep sibling paths. Multi-round; commits scoped.
argument-hint: "[optional scope — a layer, feature, or path, e.g. 'PO matching', 'the payment webhooks', backend/app/services/po_matching.py; omit to let it choose high-yield targets]"
---

Hunt for genuine correctness bugs and land the fixes. This is the **cross-cutting, multi-round** companion to the targeted audits (`/audit-money-path`, `/audit-webhooks`, `/audit-security`, which deep-audit one invariant set): `/bug-hunt` ranks high-yield targets, finds bugs in each, **proves them with a runnable probe before believing them**, fixes the root cause, ships a regression test that would fail on the old code, and then sweeps the sibling paths that share the same pattern.

`$ARGUMENTS` is an optional scope (a layer, feature, or path). If empty, you pick targets (step 1).

## Operating rules (non-negotiable — root `CLAUDE.md` guard rails)

- **Prove it before you believe it.** A bug isn't real until you've reproduced it — a failing probe (a throwaway pytest test, a Playwright snippet, a direct `psql` / async query, or a real endpoint hit with the stack up via `pnpm dev:all`). Discard plausible-but-wrong findings; a hypothesis you can't reproduce is not a finding. Delete throwaway probes before committing.
- **Fix the root cause — never mask.** No inflated timeouts, sleeps, retries, loosened assertions, or swallowed errors. If you can't fix it now, surface it explicitly (and file a tracked follow-up per guard rail 6). (Rails 4–5.)
- **Be honest when there's no bug.** If a target is sound, say so and make the deliverable the coverage gap you closed — never invent a "fix" to justify the command. (Rail 6 / coverage.)
- **Respect tenancy & secrets.** Never bypass tenant isolation (`get_tenant` / `get_tenant_db` is the chokepoint — never hardcode an `feoh_<slug>` name, never build a tenant engine outside `get_tenant_db`, never run tenant-data queries against the `feohledger` control plane), never log PII/banking data, never leak secrets. Treat auth/tenancy/migrations/approval gates/money/audit-trail/PII as load-bearing (mandatory review pass, step 7). (Rail 11.)
- **Docs-as-code.** A behaviour/command/env/port/convention change updates its docs in the same commit. (Rail 12.)
- **Commit each logical unit, path-scoped.** Fix and tests are separate commits (`git commit -m "…" -- <paths>`; the `.claude/hooks/git-scope-guard.py` hook blocks bare/whole-tree commits, `git add -A/.`, and `git commit -a`). (Rail 1 / Git workflow.)

## Where bugs have actually lived here

Bias the hunt toward the classes that bite an accounts-payable system — they recur:

- **Inconsistent logic across paths that should agree.** Two code paths computing "the same" thing differently — money math that rounds in one place and not its sibling, an FX rate locked per international payment in one path but recomputed in another, rebate math that disagrees with the dashboard aggregate, a duplicate-invoice check (`invoice_warnings.py`) that differs from the dedup the webhook relies on. Find the canonical version, diff the others against it.
- **Idempotency / at-least-once on money & webhook writes.** A re-delivered payment/card webhook or a double-clicked payment-run/void that double-pays, double-counts, or clobbers. Every money-moving write (send / post / confirm-paid) must be idempotent at the API boundary; every webhook must verify HMAC and dedupe by `event.id` (`webhook_security.py`) and return 204 on rejection.
- **Workflow state-machine gaps.** A transition that isn't in `workflow_engine.py::VALID_TRANSITIONS`, or code that reads the **live** `WorkflowDefinition` instead of the per-invoice `steps_config_snapshot` frozen at creation. A status mutated directly instead of through the transition helper — so the state changes but no audit row lands (invariant #3).
- **PO matching edge cases.** 2-way / 3-way matching (`po_matching.py`) on partial receipts, over-billing, multi-line POs, rounding tolerances, currency mismatch.
- **URL/filter-state asymmetry (frontend).** Filter/sort/selection state written to the URL but never restored (or vice-versa); `if (v && v !== def)` truthiness dropping a legitimate empty value; a "Clear"/reset that a downstream handler silently coerces back to a default. State must round-trip through `frontend/src/lib/api.ts` consumers cleanly.
- **Edge cases:** null/empty/zero amounts, unicode in vendor names, overflow, divide-by-zero (e.g. rebate %), pagination boundaries, oversized/unbounded JSON payloads, N+1 in list endpoints, concurrent writers, out-of-order webhook arrival.
- **Gate signals that must fail closed.** Anything feeding an authorization decision — approval thresholds, segregation of duties, CFO sign-off, sanctions/compliance checks before a payment adapter call, status transitions — must fail *closed*. A gate that defaults to "allowed" on missing/ambiguous input is the bug.

## Procedure

### 1. Pick targets

- **If `$ARGUMENTS` is given:** resolve it to concrete files/paths and hunt within.
- **If empty:** rank candidates by **logic density × under-coverage × hot-path/recent-bug-activity** (`git log --oneline -20 <file>`), skipping generated/schema/seed files and anything needing live cloud creds (the `mock` adapters are the local default). Favour money math, idempotency on payment/webhook writes, PO matching, the workflow state machine, and shared helpers/services (a bug there has blast radius). State each pick + why in one line. Prefer targets you haven't hit in a recent session — variety is the point.

### 2. Map before judging

Recon the target's contract first — data model (`backend/app/models/`), schemas (`backend/app/schemas/`), call sites, the invariants it must hold. For anything non-trivial spawn an `Explore` agent to map callers/models/siblings rather than guessing from one file. Note the **canonical** version of any logic that appears in more than one place.

### 3. Hunt + reproduce

For each candidate: trace the code to confirm the mechanism, then **write a probe that fails on the current code**. No probe, no finding. A probe is a throwaway pytest test, a Playwright snippet, a direct `psql` / async query, or a real endpoint hit with the stack up (`pnpm dev:all`; `pnpm seed`; login `demo@acme.com` / `demo` at `http://acme.localhost:7777`). Keep probes throwaway and named so they're easy to delete (e.g. `test_probe_*`).

### 4. Fix at the root

Apply the durable fix, matched to surrounding style/idiom and tightly scoped to the issue. If a quick patch and the durable fix diverge, name the durable fix even if you ship the patch (rail 5).

### 5. Lock it with a regression test

Promote the probe into a real test at the right layer:
- **Backend pure logic / service / API** → pytest in `backend/tests/test_*.py` (a `conftest.py` is present; run from `backend/` with the venv active).
- **Frontend user-visible** → Playwright in `frontend/tests-e2e/<area>/*.spec.ts` (areas: auth, invoices, vendors, payments, purchase-orders, goods-receipts, credit-memos, exceptions, workflows, admin, organization, sso, scim, email, smoke). Run one spec from `frontend/`: `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium`.
- **Mobile** → `flutter test` in `mobile/test/`.
- The test must **fail on the old code and pass on the fix**, and assert the invariant the bug violated. Wait on real signals, never sleeps.

### 6. Sweep the siblings

The bug you found is rarely unique. Grep for the same shape elsewhere (the other money helpers, the other webhook handlers, the other workflow transitions, the other list endpoints) and either fix-and-test them too, or state explicitly that they're already correct (with the one-line reason). This sibling sweep is where `/bug-hunt` earns its keep over a one-off fix.

### 7. Verify + review

- Run the type/lint gate (`pnpm lint` — ruff + svelte-check + flutter analyze; or the scoped piece: `ruff check .` / `pnpm check` / `flutter analyze`) and the new tests.
- Run the **nearby existing** suites on the same path to prove no regression — report pass/fail counts faithfully.
- For load-bearing diffs (money path, auth, tenancy, migrations, webhooks, audit trail), run the `code-reviewer` agent and apply/push back before committing.

### 8. Commit (scoped)

Fix and tests as separate path-scoped commits, conventional-commit style, no AI/co-author trailer. Docs ride with the commit that changed the behaviour. Then go back to step 3 for the next target until you've covered the scope (or the user's round budget).

## Report

```
## /bug-hunt — <scope or "self-selected">

**Targets:** <each pick + one-line why>

**Bugs found & fixed:**
- <file:line> — <what was wrong> → <root-cause fix> | repro: <how> | test: <file (layer)>
- … (or "none — targets were sound; coverage backfilled where thin")

**Sibling sweep:** <same-shape paths checked — fixed too / confirmed correct + why>

**Verification:** <lint/type gate; new tests N/N; nearby suites N/N; review verdict if run>

**Commits:** <hash + subject, one per line>

**Deferred / recommended:** <out-of-scope leads with the long-term fix named + tracked follow-up (rail 6) — or "nothing outstanding">
```

## Tone

Lead with the verdict, not the process. State a real bug plainly with its repro; if a target was sound, say so and point at the coverage you added. Don't dress up a non-finding as a fix.
