---
description: Hunt for interaction/UX defects in the SvelteKit app — dead-ends, broken back/forward + URL filter/sort/selection state, missing empty/loading/error states, stale selection, filter/sort/count inconsistency, keyboard traps, invalid status transitions surfaced as controls. Fixes the objective bugs (with e2e), reports the judgment calls. Commits scoped.
argument-hint: "[optional scope — a route or feature, e.g. /invoices, the payment-runs flow, 'the exceptions queue master/detail'; omit to sweep the main app routes]"
---

Drive the app like a real user and hunt for **interaction defects** — flows that technically render but dead-end, lie, lose state, or strand the user. This is the behaviour-correctness cousin of `/polish-ui` (which redesigns layout to the visual quality bar — including the Flutter mobile surfaces) and `/persona` (which audits read-only from a role's POV: accountant, approver, CFO, supplier, etc.). `/ux-hunt` **lands fixes** for the objective defects and writes up the subjective calls. It is web-only — the SvelteKit frontend; a pure mobile visual redesign is `/polish-ui`'s job.

`$ARGUMENTS` is an optional scope (a route or feature). If empty, sweep the main AP app routes: dashboard, invoices (list + detail + upload/extraction review), vendors, payments + payment runs, purchase-orders, goods-receipts, credit-memos, exceptions queue, workflows, analytics / CFO dashboard, tax / 1099, cards, admin (users/roles), organization settings (ERP / extraction / SSO / SCIM), and the supplier portal.

## What counts as a UX *bug* here (objective — fix these)

These are the classes that strand a real AP user:

- **Dead-ends.** A control leaves the user somewhere with no way forward — e.g. an invoice extraction-review pane whose vendor/GL dropdowns never populated because the entry path didn't fetch the data, a payment-run modal with no working close, or an approval action that lands on a blank screen.
- **Broken navigation state.** Back/forward, reload, and new-tab must preserve the view. The page's URL filter/sort/selection state must round-trip: every filter, sort, and selected row written to the URL is **restored on load** (and vice-versa), deep-links open the right invoice/vendor/payment, and a `replaceState` doesn't leave the visible state and the URL disagreeing.
- **State-coercion no-ops.** A "Clear" / "All" / reset that a downstream handler silently coerces back to a default (the control appears to do nothing). The default-vs-truthiness trap: `if (v && v !== def)` drops a legitimate empty selection (an "all statuses" filter, a cleared vendor filter).
- **Missing or wrong empty / loading / error states.** A list with no rows shows a blank pane instead of an empty state; a filtered-to-zero result (e.g. "no exceptions in this queue") shows the global empty instead of a "no matches" state; a failed fetch shows a stuck "Loading…"; an invoices or exceptions master/detail split leaves a **stale** or **blank-while-results-exist** detail pane when the selected row is filtered out.
- **Filter / sort / count inconsistency.** A summary count (open invoices, pending approvals, aging buckets) that disagrees with the rendered rows; a "showing N of M" where M is the paginated count not the full set; an active status-chip class that doesn't match the actual filter.
- **Invalid status transitions offered as controls.** A button that offers a state change the workflow can't make — the authoritative graph is `backend/app/services/workflow_engine.py::VALID_TRANSITIONS`. An "Approve" on an already-paid invoice, a "Send to ERP" on a voided one, or any control whose action the backend will reject is a UX bug: the affordance must match what the state machine allows.
- **Keyboard / focus traps.** A modal that doesn't trap focus or close on Escape; a row-as-button with no keyboard activation; focus lost after an action (approve, reject, save).
- **Destructive-action surprises.** A delete / void / reject with no confirm, or a click target that does two things at once (a row navigates *and* the void button inside it fires navigation).
- **Invariant leaks visible in the UI.** A view that renders without auth (every route is auth-walled by design); a view that shows another tenant's data (tenant isolation); money displayed float-rounded instead of exact (`Decimal`); bank-account / tax-ID / payment-method data exposed in a URL or query string. If you can see one of these from the front end, it's an objective defect — fix it and flag it to `code-reviewer` / the operator.

## What is NOT in scope (hand off, don't fix here)

- Pure visual layout / spacing / archetype redesign (web or mobile) → `/polish-ui`.
- Subjective wording / IA / "should this flow exist" → write it up; don't unilaterally redesign.
- Accessibility conformance depth (contrast ratios, ARIA semantics, WCAG/EAA) → `/audit/accessibility`.
- Role-POV correctness sweeps (does the CFO sign-off gate hold, can a supplier see another supplier's data) → `/persona`.
- Security / money-path / webhook deep audits → `/audit-security`, `/audit-money-path`, `/audit-webhooks`, `/audit/auth`.

## Operating rules (non-negotiable — root `CLAUDE.md` guard rails)

- **Reproduce in the running app first.** Confirm the defect against the live stack (`pnpm dev:all` + `pnpm seed`) or a Playwright snippet before fixing — a UX bug you can't demonstrate is a hypothesis. Log in at http://acme.localhost:7777 (`demo@acme.com` / `demo`). The fix is proven by an **e2e test that fails on the old behaviour**.
- **Fix the root cause — never mask** (no arbitrary waits/retries to paper over a race; fix the readiness signal). (Rail 4.)
- **Reusable components & runes.** Build UI from `frontend/src/lib/components/*`; Svelte 5 runes only (`$state` / `$derived` / `$effect` / `$props`). All dynamic data goes through `frontend/src/lib/api.ts` (it adds the JWT + `X-Tenant-Slug` header) — there is no SSR data load. Don't copy-paste markup. (Rail 9; `frontend/CLAUDE.md`.)
- **Honour the project invariants.** Auth before everything, tenant isolation at the data layer, money exact, no PII/banking in URLs — see `## What counts as a UX bug` and the root invariants list. (Rail 11.)
- **Be honest about non-findings.** A sound flow + a new e2e test that locks the good behaviour is a success. (Rail 6 — no dangling findings; close the loop or hand it off explicitly.)
- **Docs-as-code; commit scoped.** (Rail 12; git workflow — fix and test as separate path-scoped commits via `git commit -m "…" -- <paths>`; the scope-guard hook blocks bare/whole-tree commits; no AI/co-author trailer.)

## Procedure

1. **Resolve scope** → concrete routes/components under `frontend/src/routes/`. Bring the stack up if it isn't (`pnpm dev:all`, then `pnpm seed` once).
2. **Walk the flows like a user.** For each route in scope, exercise: first load (empty + populated), every filter/sort/search (incl. filtered-to-zero), selection + deep-link, back/forward/reload/new-tab, every button/affordance, the keyboard path, and every status-changing control (does the offered action match `VALID_TRANSITIONS`?). Read the `+page.svelte` to confirm the mechanism behind anything that smells wrong (URL filter/sort/selection round-trip parity, `$derived` count sources, change-handler coercion).
3. **Probe each candidate** with a Playwright snippet that asserts the broken behaviour — that's your repro and your soon-to-be regression test.
4. **Fix the objective defects** at the root, in shared components where the markup repeats.
5. **Lock with e2e** in `frontend/tests-e2e/<area>/` (areas: auth, invoices, vendors, payments, purchase-orders, goods-receipts, credit-memos, exceptions, workflows, admin, organization, sso, scim, email, smoke). Prefer read-only assertions where possible so they're parallel-safe across worker tenants; wait on real signals, never sleeps. The test must fail on the old behaviour. Run one spec from `frontend/`:
   `pnpm exec playwright test --config=tests-e2e/playwright.config.ts <spec> --project=chromium`
6. **Verify:** `pnpm check` (svelte-check) + the new specs + the nearby existing specs for that area (report counts). For load-bearing flows (auth walls, tenant scoping, status-transition gating surfaced in UI, money display) run `code-reviewer`.
7. **Commit** fix + tests as separate path-scoped commits. Write up the subjective/out-of-scope findings for the operator.

## Report

```
## /ux-hunt — <scope>

**Flows walked:** <routes/features exercised>

**Defects fixed:**
- <route> — <what stranded/confused the user> → <fix> | repro+test: <spec (e2e)>
- … (or "none objective; behaviour was sound")

**Reported (judgment calls / out of scope):** <subjective findings + where they belong (/polish-ui, /audit/accessibility, /persona) — or "none">

**Verification:** <pnpm check; new specs N/N; nearby specs N/N; review verdict if run>

**Commits:** <hash + subject>
```

## Tone

Lead with what was broken for the user and how it's fixed. Don't redesign on a whim — fix the defects, route the opinions to the operator.
