---
description: Polish the UI/UX of a single web page or mobile screen to FeohLedger' quality bar. Dispatches to `ui-polisher` (frontend / SvelteKit) or `mobile-ui-polisher` (mobile / Flutter) based on the resolved target.
argument-hint: <route, screen, or component/widget path>
---

Polish the UI/UX of `$ARGUMENTS` using the appropriate polisher agent.

## When to use this command

**Right fit (web or mobile):**

- An index page / list screen that doesn't use the available real estate well, where alignment drifts row-to-row, or where the archetype doesn't match the data (flat list when it should be filter-chip-bucketed; card grid when it should be a dense table; static list when it should be pull-to-refresh + swipe-actionable).
- A page / screen leaking raw ISO dates or `toString()` output, missing status filter chips, missing search when the list can grow, or missing pagination / refresh affordances.
- A modal / widget used in multiple places where consistency matters.

**Wrong fit — tell the user and stop:**

- A purely-functional Settings / Login / change-password / profile / MFA page or screen with no real-estate or scanability problem.
- A detail page / screen that already has rich UI (`/workflows/[id]`, `invoice_detail_screen.dart`, etc.) — polish on detail surfaces has a worse cost/value ratio than on index surfaces.
- A request that's really a feature, not a polish — "add a chart of invoice volume over time", "add a push-notification settings panel". Surface as a feature plan, not a polish. (Bookmarkable filter/sort URLs are *not* in this category any more — URL-backed list state is the established convention on ~24 routes, so closing that gap on a route that still keeps filters in local `$state` is ordinary polish.)
- An asks-for-everything sweep ("polish all pages / all screens"). Pick one and tell the user to invoke this command again for the next.

## Resolving the target — and choosing the polisher

`$ARGUMENTS` can be:

### Web (→ `ui-polisher` agent)

- A **route slug** (`/invoices`, `/vendors`, `/payments`, `/exceptions`, `/workflows`, `/admin`, `/purchase-orders`, `/goods-receipts`, `/credit-memos`, `/organization`, `/` for the dashboard) — resolves to `frontend/src/routes/<slug>/+page.svelte` (or `frontend/src/routes/+page.svelte` for `/`).
- A **`frontend/...` file path** — used as-is.
- A **Svelte component name** (`InvoiceModal`, `RunDetailModal`, `BulkBar`) — resolve via `find frontend/src/lib/components -name "<name>.svelte"`.

### Mobile (→ `mobile-ui-polisher` agent)

- A **`mobile/...` file path** — used as-is.
- A **`mobile:<screen>` shorthand** (`mobile:invoices`, `mobile:dashboard`, `mobile:approvals`, `mobile:payments`, `mobile:capture`, `mobile:settings`, `mobile:invoice-detail`, `mobile:home`) — resolves to `mobile/lib/screens/<screen>_screen.dart` (note the `_screen` suffix; `invoice-detail` → `invoice_detail_screen.dart`).
- A **Flutter widget name** (`InvoiceListTile`, `KpiCard`, `StatusBadge`) — resolve via `find mobile/lib/widgets -iname "<snake_case>.dart"`.

### Dispatch rules

- Resolved path under `frontend/` → spawn `ui-polisher`.
- Resolved path under `mobile/` → spawn `mobile-ui-polisher`.
- Argument is a bare name that exists in both (`invoices` matches `/invoices` AND `mobile:invoices`) → **ask the user which platform** with `AskUserQuestion`. Don't guess.
- Argument is empty or "audit" → list candidate index pages **and** candidate mobile screens (two short bulleted lists) with a one-line "why this one matters most right now" each, and ask the user to pick. Don't blanket-sweep.

## The flow

1. **Pre-flight (web targets):**
   - Confirm the frontend dev server is up at `:7777` on the tenant subdomain (`curl -s -o /dev/null -w '%{http_code}' http://acme.localhost:7777/`).
   - Confirm the backend is up at `:8000` (`curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/health`). If not, tell the user (`cd backend && docker compose up -d && python main.py`) and stop.
   - Confirm the seed has been run at least once (the screenshot uses `demo@acme.com` / `demo`). The agent will surface a login failure if needed.

   **Pre-flight (mobile targets):**
   - Check for a connected device / simulator with `cd mobile && flutter devices`. If none, tell the user the agent will skip the screenshot step and rely on source-level diff + their next `flutter run` to verify visually. Ask whether to proceed; don't launch a simulator yourself.
   - Backend at `:8000` is *not* required for source-only polish (the agent doesn't drive the app's networking during the edit), but if the user wants to verify the redesign live, remind them the backend has to be running for any data-fetching screen.

2. **Resolve target → invoke the right agent:**

   Spawn the chosen agent with a prompt like:

   > "Polish the UI/UX of `<resolved file path>`. The user's stated intent was: `<the original argument string>`. Follow your agent spec: audit, plan, edit, verify, report. Do not commit."

3. **Relay the agent's report.** When it returns, surface:

   - The before/after screenshot paths so the user can open them (web always; mobile only when a device was connected).
   - The list of files changed (run `git diff --stat` to confirm).
   - Any test selector updates the agent applied so the user can sanity-check those edits.
   - The agent's "Notes for the human" section verbatim.

4. **Wait for the user's call on the commit.** Do not pre-stage or pre-commit. When the user says yes:

   - Stage the changed files explicitly (don't `git add -A` — risk of pulling in unrelated test results / screenshots).
   - Commit message follows a `ui(<scope>):` convention. Use the platform in the scope when useful — `ui(invoices)` for web, `ui(mobile/invoices)` for mobile. **No `Co-Authored-By` / "Generated with Claude Code" / robot-emoji footers** — the user-level rule wins.
   - Example: `git commit -m "ui(mobile/invoices): <one-liner>" -m "<3-5 line body explaining what archetype + which patterns applied>"`.

## Cost reality

This command costs more than a normal edit (a screenshot pass on web, full lint/type-check, possible test re-run, an agent context). Don't burn it on a 5-pixel padding tweak — for that, the user edits directly. The command earns its cost on archetype-level or hierarchy-level changes (a card grid that should be a table, a flat list that should be filter-chip-bucketed, a list missing pull-to-refresh + swipe actions, etc.).

## What this command does NOT replace

- `/check` for a pre-commit gate (code-review + test-gap + doc-hygiene).
- `/safe-edit` for security-sensitive changes.
- `/audit-security`, `/audit-money-path`, `/audit-webhooks` for periodic broad sweeps.

## Tone

Don't narrate the agent's internal steps. The user sees:

- A one-sentence "Resolving target → `<path>` (web | mobile). Spawning the polisher."
- The agent's structured report (audit findings + changes + verification + notes), relayed.
- A "Want me to commit?" question with the suggested commit message.
