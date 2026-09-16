# Frontend — CLAUDE.md

Frontend-specific guidance. See root `CLAUDE.md` for project-wide context.

## Where to look (frontend docs)

This file holds the rules. The reference material lives in `frontend/docs/` so
it is read on demand rather than loaded into every conversation:

| Topic | File |
|-------|------|
| Design system + every UI pattern (layout, tables, search, bulk, modals, a11y, colour) | `docs/ui-patterns.md` |
| Every shared component and its props | `docs/component-library.md` |
| Every route, the endpoints it calls, its role gating | `docs/routes-api-map.md` |
| i18n — catalogues, negotiation, locale-aware formatting | `docs/i18n.md` |
| Money rendering (`<Money>`, exact decimal strings) | `docs/money-formatting.md` |

## Stack

- **SvelteKit 2** with **Svelte 5** (runes syntax), adapter-static
- **TypeScript** 5.8, **pnpm**
- **Icons**: unplugin-icons with `@iconify-json/material-symbols`
- **Markdown**: mdsvex
- **Styling**: normalize.css + custom CSS in `src/app.css`
- **Sanitization**: there is no sanitizer dependency, deliberately. The XSS
  defence in this tree is that **no component uses `{@html}` at all** — every
  chat / assistant / invoice bubble binds plain text, and says so in a comment.
  `isomorphic-dompurify` was declared for years and never imported once; it was
  removed rather than bumped, since an unused dependency still sets a Node floor
  and drags `jsdom` into the graph. If you ever have a case that genuinely needs
  user-supplied markup, add the sanitizer back **with** its call site in the same
  change — never ahead of one.

## Commands (from `frontend/`)

```bash
pnpm dev              # dev server on :7777
pnpm build            # production build (adapter-static)
pnpm preview          # preview build on :8888
pnpm check            # typecheck (src/ only — see check:e2e below)
pnpm check:e2e        # typecheck tests-e2e/ (tsconfig.e2e.json)
pnpm test:unit        # vitest unit tests (i18n parity, pure helpers, the
                      # stylesheet colour-token/contrast guard)
```

### `pnpm check` does not cover `tests-e2e/` — `pnpm check:e2e` does

`tsconfig.json` extends `.svelte-kit/tsconfig.json`, whose generated `include`
lists `../src/**`, `../test/**` and `../tests/**`. `tests-e2e` is none of those,
and TypeScript does not merge includes from an extended config, so the omission
is **silent**: an e2e spec can carry any type error at all and `pnpm check`
stays green.

What that cost: every e2e stub of an API response was a hand-maintained object
literal that nothing compared against the type the app reads. The dashboard stub
omitted `aging_reporting.unconverted_count`, `undefined > 0` is false, and the
partial-conversion disclosure the spec existed to exercise could only ever
render its no-notice branch — in two files, with no signal.

`tsconfig.e2e.json` puts that tree in a real program (`pnpm check:e2e`, wired
into the **Frontend** CI job beside the typecheck, and into root `pnpm lint` as
`lint:frontend:e2e`). Two things follow:

- **A response fixture declares its contract.** `tests-e2e/dashboard/fixture.ts`
  is the worked example: one shared payload builder ending in
  `satisfies DashboardData`, so a new non-optional field on the app type is a
  compile error in the fixture rather than a spec that quietly stops exercising
  its own branch. Build a new stub this way whenever the shape it fakes has a
  `$lib/types` counterpart. It found two real drifts on the way in — the
  dashboard's `discount_capture` money was typed `MoneyString` while the wire
  sends JSON numbers, and the five `AgingBuckets` bands were typed `number` and
  were being summed and divided as raw currency in the route.
- **`$lib` imports under `tests-e2e/` must be `import type`.** `tsc` resolves the
  alias through `.svelte-kit/tsconfig.json`'s `paths`; Playwright's own esbuild
  transform does not read that file, so a VALUE import from `$lib` typechecks
  and then fails to resolve when Playwright loads the spec. Types are erased
  before the runtime sees them, which is why the contract costs nothing at test
  time.

`@types/node` is a devDependency for this config alone — the Playwright tree
genuinely runs in Node (`process.env`, `Buffer`, `node:crypto`), and
`fixtures/helpers.ts` is imported by every spec, so there was no excluding its
way around. `tsconfig.json` therefore sets `"types": []`, which turns OFF the
automatic `@types/*` sweep and keeps Node globals out of `src/`: without it,
`process.env` in a component would typecheck and only fail in the browser.
`tsconfig.e2e.json` names `"types": ["node"]` explicitly instead.

`tsconfig.e2e.json`'s `exclude` list is **shrink-only** — every entry is a file
whose fix is a one-line annotation, listed rather than fixed because another
session owned it. Never add a file there to turn a red check green.

## The lockfile: pnpm is pinned, and `pnpm.overrides` is fragile

`package.json` declares `"packageManager": "pnpm@10.12.4"` — in **both** the
repo root and here — and no `pnpm/action-setup` step in `.github/workflows/`
passes a `version:` input any more. The action reads the manifest, so one edit
moves CI, `sso-e2e`, `web-bundle-budget`, `aws-deploy`, `audit`, the VM's
`deploy/deploy.sh` (which reads the same field) and every contributor
together. Before this, four different pnpm versions wrote one
lockfile (CI on 9, `audit.yml` on 10, laptops on whatever, Dependabot on its
own default), which shows up as unrelated `libc:` lines churning in and out of
`pnpm-lock.yaml` depending on who ran the install. **Bump pnpm by editing
`packageManager`, never by adding a `version:` back to a workflow.**

`package.json` also declares `pnpm.overrides` for `cookie@<0.7.0` and
`undici@<7.28.0`. These are **conditional** floor guards: the tree currently
resolves `cookie@0.7.2` and `undici@8.10.0`, so neither override matches
anything today. They exist to block a *future* transitive downgrade onto a
vulnerable version — which is exactly why losing one is easy to miss.

Two Dependabot npm PRs (#344, #351) arrived with the lockfile's whole
`overrides:` block deleted, red on every job that installs:

```
ERR_PNPM_LOCKFILE_CONFIG_MISMATCH  The current "overrides" configuration
doesn't match the value found in the lockfile
```

That failure is the guard working — `--frozen-lockfile` is the only thing
standing between a dropped override and a merge that silently stops applying
it. If you see it, **do not** reach for `--no-frozen-lockfile`: regenerate
properly and confirm the block survived.

```bash
pnpm install --lockfile-only          # then check the overrides are still there
sed -n '1,12p' pnpm-lock.yaml
pnpm install --frozen-lockfile        # the check CI runs
```

The `packageManager` pin is expected to stop Dependabot dropping the block,
since it will now resolve the same pnpm as everything else — but that is
unconfirmed until the next npm PR.

### The sync is automated, and the overrides check is the point

`.github/workflows/dependabot-lockfile.yml` runs that recipe for you on a
Dependabot PR touching `frontend/package.json`: `pnpm install --lockfile-only`,
then it commits the regenerated `pnpm-lock.yaml` back onto the Dependabot
branch. An already-correct lockfile commits nothing. It does the backend pip
locks in the same run — see
[backend/CLAUDE.md](../backend/CLAUDE.md) § Dependency lock.

Between regenerating and committing it **verifies every `pnpm.overrides` entry
in `package.json` is present in the lockfile with the same value**, and fails
the job naming the offending key if not. That check is the reason the workflow
exists rather than being a plain "run pnpm install" step: the guards are inert
today, so a lockfile missing one looks and behaves exactly like a correct one
until the day it matters, and a workflow that committed silently would
automate away the very failure (`ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`) that
caught #344 and #351. It then runs `pnpm install --frozen-lockfile` — the
command CI runs — as a second gate. There is no `--no-frozen-lockfile`
anywhere in it, and there must never be.

**When the job goes red on that check, regenerate by hand with the recipe
above and confirm the block survived** — the same fallback as before, and the
same one `backend/CLAUDE.md` § Dependency lock describes for the pip locks.
Never "fix" it by relaxing the check or the install flag.

Two limits worth knowing: a `GITHUB_TOKEN` push cannot start a workflow run
unattended, so the synced commit's CI is *created but parked* in
`action_required` and the PR keeps reading red until someone approves those runs
(checks tab → "Approve and run", or
`gh api -X POST repos/<owner>/<repo>/actions/runs/<run_id>/approve`; do not
`@dependabot rebase`, which drops the commit); and the workflow only fires for
`dependabot[bot]`, so a human-pushed lockfile bump is still yours to
regenerate.

## The Node floor: 24, and the dependency that sets it

CI runs Node **24** — every `setup-node` step across all six workflows that
have one (`ci.yml` ×4, `sso-e2e`, `web-bundle-budget`, `compliance-drift`,
`aws-deploy`, `audit`). 24 is the current Active LTS (Krypton, LTS since
2025-10-28, supported to 2028-04-30); Node 20 (Iron) went end-of-life on
2026-04-30 and 22 (Jod) has been in maintenance since 2025-10-21. Pin the
whole set together — a floor raised in one workflow and not another is worse
than not raising it, because the disagreement is invisible until the odd job
out breaks.

**What used to set the floor** was `isomorphic-dompurify` and its `jsdom`
dependency, both declaring `engines: ^22.22.2 || ^24.15.0 || >=26.0.0`. pnpm
does not enforce `engines` without `engine-strict`, so Node 20 installed them
without complaint — which is precisely why this drifted unnoticed for so long.
It was removed rather than bumped, because nothing had ever imported it: an
unused dependency was dictating the runtime the whole project builds on. What
remains is `jsdom@30` as **vitest's own** test-environment peer, which declares
the same range — so the floor is real, it just now belongs to something the
project actually uses.

Two consequences worth knowing before you touch either half:

- **Bumping the floor is a two-file-set edit**, not one. Raising
  `node-version:` means checking every other place a Node version is written
  down. There is no `.nvmrc`, no `engines` block in either `package.json`, and
  no Node Dockerfile in the app tree — but `deploy/deploy.sh` builds the
  production frontend in a `NODE_IMAGE=node:<major>-alpine` container,
  documented in `docs/minimal-deployment.md`. **Both halves move together**;
  raising CI and leaving the deploy image behind means production builds on a
  runtime CI never tested.
- **The `# vN.N.N` comment beside a `setup-node@<sha>` pin is documentation,
  not the pin.** Eight of the nine sites carried `# v6.0.0` against a SHA that
  is really `v7.0.0`; Scorecard's PinnedDependencies check reads the SHA and
  was perfectly happy, so nothing caught it. When you re-pin, verify the tag
  the SHA resolves to rather than copying the neighbouring comment.

## Routes → API mappings

**The full per-route map — every route, the endpoints it calls, and its role
gating — is `frontend/docs/routes-api-map.md`.** Read it when wiring a page to
the API, or when changing a backend route's shape and you need every caller. The
backend side of the same map is `backend/docs/api-surface.md`.

Two things hold for every route, and a diff that breaks either is a bug:

- **All fetches go through `src/lib/api.ts`**, which adds the JWT and the
  `X-Tenant-Slug` / `X-Entity-ID` headers. A bare `fetch` to the backend from a
  component bypasses tenant scoping.
- **The sidebar gate and the route's own gate must agree.** `nav.ts` deciding a
  row is visible while the page's endpoints 403 is a defect that has shipped
  more than once — gate per item, against the same permission the endpoint
  checks.
## Key modules

### API client — `src/lib/api.ts`

All data fetching goes through this module. Never call `fetch()` directly for API requests.

- Auto-adds `Authorization: Bearer <token>` from localStorage
- Auto-adds `X-Tenant-Slug` header from subdomain
- 401 responses clear token and redirect to `/login`
- Methods: `api.get<T>()`, `api.post<T>()`, `api.patch<T>()`, `api.put<T>()`, `api.delete()`, `api.upload<T>()`
- Token helpers: `setToken()`, `clearToken()`, `hasToken()`
- **Error messages** — every non-OK response is thrown as an `ApiError` whose message comes from `formatApiDetail(body.detail, fallback)` (`src/lib/utils/apiError.ts`, re-exported from `api.ts`). FastAPI's `detail` is **not always a string**: a Pydantic 422 carries a LIST of `{loc, msg, type}`, which the old `body.detail || fallback` stringified as literally `"[object Object]"` in the toast. The helper renders a string as-is, a validation list as `field: msg; field: msg` (dropping the `body`/`query` `loc` prefix), a lone `{msg}`/`{message}`, and falls back otherwise. It is pure (no `$env`, no `fetch`) so it lives in `utils/` and is unit-tested (`apiError.test.ts`). It deliberately FLATTENS to one string — a caller that needs the structure reads the raw body itself, which is why `api/expenses.ts::submitReport` still hand-rolls its own `fetch` to keep the policy-violation list intact.
- **Streaming** — `streamAssistantChat(body, { onTool, onDelta, onDone, onError }, signal?)` streams the AP assistant turn from `POST /api/assistant/chat/stream` (`text/event-stream`). It uses `fetch` + `response.body.getReader()` (NOT `EventSource`, which can't set the Authorization / tenant / entity headers) and a small SSE parser (split on `\n\n`, read `event:`/`data:` lines), reusing the shared `authHeaders()` helper so the header logic can't drift from `request()`. SSE frames: `tool` (one per tool invocation; `result` carries the structured output for charts), `delta` (incremental answer text), `done` (authoritative payload), `error` (mid-stream failure). A pre-stream HTTP 429 throws `AssistantBudgetError` (carries `used`/`budget`/`period`); any other non-OK / network failure throws a plain `Error` — the `/assistant` page catches both and falls back to the non-streaming `POST /api/assistant/chat`.
- Typed feature helpers wrap `api` per domain — e.g. `src/lib/api/audit.ts` (`getInvoiceAuditLog`, `getAuditExport`, `downloadAuditExportCsv`) over the SOX audit endpoints, with `AuditEntry` / `AuditFieldChange` types in `src/lib/types/audit.ts`. The invoice-modal Activity timeline renders `details.changes` (per-field before/after) from these. `src/lib/api/tax.ts` (`get1099Report`) wraps the 1099 endpoint, with `Report1099` / `Vendor1099Row` types in `src/lib/types/tax.ts`.

### Money formatting — `src/lib/utils/money.ts` + `ui/Money.svelte`

**Full reference: `frontend/docs/money-formatting.md`.**

Render every amount through `<Money>`; never hand-roll `toFixed` or an `Intl`
call at a call site. The backend serializes money as an **exact decimal string**
(project invariant: money is `Decimal`, never `float`) — parsing one into a JS
number to format it is how precision gets lost, so pass the string straight
through. An absent figure formats as a dash, never `0.00`, and absence is keyed
on nullish rather than falsiness so a genuine `0` still renders.
### Internationalization (i18n) — `src/lib/i18n/`

**Full reference: `frontend/docs/i18n.md`** (catalogue layout, locale
negotiation, pluralization, and the locale-aware number/date/currency helpers).
Mobile has a parallel setup in `mobile/CLAUDE.md` § Internationalization.

The rule: **no user-facing string is ever a hardcoded literal** — everything goes
through `t()`, and every number, date and currency renders through the
locale-aware helpers rather than a raw `toLocaleString`. A new string ships with
its catalogue entry in the same change.

That applies to a sentence the BACKEND composed too. A server-rendered finding
is not exempt from i18n just because it arrived over the wire: the backend emits
a stable code plus typed params and the client keys on the code, with the
server's prose as the fallback for a code this build predates. The two worked
examples are `api/einvoiceIssues.ts` (e-invoice refusals) and
`api/invoiceWarnings.ts` (invoice warnings); both read a GENERATED map, so the
catalogue cannot drift from the backend. Reach for that shape rather than
rendering server English inside a translated frame.

**The one exception is the published legal text** under `src/routes/legal/` and
`src/lib/legal/`, which is deliberately English-only and carries no catalogue
entries. A translated privacy policy, DPA or set of Terms is not a localized UI
string — it is a second binding text, and a mistranslated clause is a
misrepresentation rather than a cosmetic bug. The documents say so themselves,
and the surrounding chrome a reader navigates by is minimal on purpose. If these
are ever translated it is a legal exercise with counsel sign-off per locale, not
a catalogue backfill; see `docs/decisions.md` §174. **This exception covers only
those two directories**, plus the document titles `pages.ts` exports for the
links that reach them — take those from `legalTitle(path)`, never by typing the
title again. A nav item, footer or banner elsewhere that merely *points at* a
legal page is ordinary UI and goes through `t()` like anything else. A sentence
with a link inside it stays ONE catalogue entry with `{token}` markers, rendered
through `ui/LinkedMessage.svelte`; fragmenting it into `…Pre`/`…Post` entries
fixes the link order and cannot be translated (`frontend/docs/i18n.md` § A
sentence with a link inside it).

### Tenant — `src/lib/tenant.ts` + `src/lib/hostRouting.ts`

`hostRouting.ts` owns the pure rules ("what does this hostname mean") and is
dependency-free so vitest can reach it; `tenant.ts` composes them with the env
read. A hostname classifies as one of four kinds:

| Kind | Example | Slug sent | API base |
|------|---------|-----------|----------|
| `platform-tenant` | `acme.localhost:7777` | `acme` | build-time `PUBLIC_API_URL` |
| `platform-apex` | the marketing host | none | build-time |
| `vanity` | a customer's own `ap.acmecorp.com` | **none** | **same origin** (`/api`) |
| `unknown` | no platform domains configured | legacy rule | build-time |

`PUBLIC_PLATFORM_DOMAINS` (comma-separated registrable domains) is what
separates a platform host from a customer's. **Unset replays the pre-change
rule exactly**, so an existing build is unaffected on upgrade — custom domains
simply stay unreachable until an operator opts in.

- `getTenantSlug()` — the slug, or `null` on a vanity host. A vanity host HAS a
  tenant; the backend resolves it from `Host`. Never gate rendering on this.
- `hasTenantContext()` — "does this host carry a tenant at all". This is the
  render gate. Gating on the slug is what made the app show the **marketing
  landing page** to a customer on their own domain.
- `getApiBase()` — resolved per request, not baked at build time. Suppressing
  `X-Tenant-Slug` alone is not enough: a vanity host calling the build-time API
  origin hands the backend the *platform's* `Host`. Same-origin `/api` is what
  carries the vanity hostname, which makes "proxy `/api` on the vanity origin"
  an operator requirement (see `docs/white-label.md`).
- `getTenantStorageKey()` — per-tenant browser-storage key (`$lib/entity.ts`):
  the slug on a platform host, the hostname on a vanity one.

`tenantSlugUsage.test.ts` ratchets both invariants: nothing re-derives the slug
by hand, and the list of files still reading `PUBLIC_API_URL` directly may only
ever shrink. **Both host kinds are covered end-to-end** by
`tests-e2e/tenant/vanity-host.spec.ts`: `PUBLIC_PLATFORM_DOMAINS` reaches the
dev server through `playwright.config.ts`'s `webServer.env` and CI's preview
bundle through the `pnpm build` step's env (both from
`fixtures/env.ts::PLATFORM_DOMAINS`, so they cannot disagree), and the vanity
origin is the loopback **IP literal** — no `*.localhost` name can be a vanity
host while `localhost` is the declared platform domain. `vite.config.ts` proxies
`/api` on the same origin (`changeOrigin: false`) so the vanity `Host` survives
to the backend, which is the operator requirement a real custom domain carries
and the only way to exercise one on a laptop.

### Stores (`src/lib/stores/`) — Svelte 5 rune stores

| Store | File | State | Key methods |
|-------|------|-------|-------------|
| `auth` | `auth.svelte.ts` | `user` (incl. `mfa_enabled`, `mfa_required_by_org`), `loggedIn`, role checks (`isAdmin`, `isManager`, `isCfo`, `isClerkOnly`) | `login()` (returns `{kind:'ok'} \| {kind:'mfa', challenge}` — MFA branch routes to `/login/mfa`), `completeMfa(token, code, method)`, `requestEmailMfa(token)`, `completePasskey(token)`, `listPasskeys()`, `passkeyStepUp(operation)` (mint + sign a factor-change step-up assertion), `registerPasskey(name, stepUp)`, `deletePasskey(id, stepUp)`, `listSessions()` / `revokeSession(id)` / `revokeOtherSessions()` (the caller's own live sessions — see the `/profile` row), `logout()`, `fetchUser()`, `hasRole()`, `hasAnyRole()` |
| `invoiceStore` | `invoices.svelte.ts` | `all`, `loading`, `errored`, `total`, `statusCounts` | `fetch(params)`, `fetchCounts(params)` (pass the list's `buildParams()` — the chip tallies are population-filtered too; own `countsSequence`), `update(id, changes)` |
| `paymentStore` | `payments.svelte.ts` | `all`, `loading`, `errored`, `total`, `hasMore` | `fetch(params)`, `loadMore()` (history-tab Load-More; remembers filter params) |
| `workflowStore` | `workflows.svelte.ts` | `all`, `loading`, `total`, `hasMore`, `activeSteps` | `fetch()`, `loadMore()`, `fetchActiveSteps()`, `getById()`, `create()`, `update()` |
| `adminStore` | `admin.svelte.ts` | `users`, `roles`, `loading` | `fetchUsers()`, `fetchRoles()`, `createUser()`, `updateUser()`, `deleteUser()` |
| `sidebar` | `sidebar.svelte.ts` | `collapsed` | `toggle()` |
| `orgCurrency` | `orgSettings.svelte.ts` | `currency` | `ensureLoaded()`, `reset()` — tenant REPORTING currency for aggregate (non-per-row) money; lazy-loads from `/api/organization` and resolves via `utils/reportingCurrency.ts` in the backend's order, USD fallback |
| `notificationStore` | `notifications.svelte.ts` | `items`, `unread`, **`inboxTotal`** (the WHOLE inbox, filter-independent — the All chip), **`filteredTotal`** (the count of what the current filter matched — `hasMore`, Load-more, "Showing all N"), `loading`, `hasMore`, `prefs`. The two were one field, so an `unread_only=true` response overwrote the inbox count and both chips showed the same number | `fetchList({unreadOnly})`, `loadMore()`, `fetchUnreadCount()`, `markRead(id)`, `markAllRead()`, `fetchPrefs()`, `updatePrefs()`, `startPolling()`/`stopPolling()` (60s unread-count poll for the sidebar-header bell badge; started from `+layout` when signed in) |

### Components (`src/lib/components/`)

**Full props and usage for every component: `frontend/docs/component-library.md`.**
Grouped into subfolders by role; import with the full path
(`import Modal from '$lib/components/ui/Modal.svelte'`) — there is no barrel file.

**Guard rail 9: build UI from these, never copy-pasted markup. Extract a new
component the second time you would duplicate one.**

- `ui/` — primitives: `PageHeader` `DataTable` `FilterChips` `Modal` `KpiCard` `Badge` `EmptyState` `Money` `SectionTabs` `Tabs` `FieldWarning` `SecretReveal` `BrandMark`
- domain: `InvoiceModal` `VendorModal` `VendorPicker` `RunDetailModal` `ApprovalMatrixEditor` `BulkRecodeGLModal` `AdvancedSearchModal` `ScreeningBadge` `SubscriptionBadge` `SpendBarChart` `UsageMeter` `PortalListFilters`
- chrome: `Sidebar` `NotificationBell` `EntitySwitcher`
- chat/assistant: `SupplierChatThread` `ChatMessage` `ExamplePrompts` `ToolResultView`
- marketing: `Landing` `Pricing` `Backdrop` `HeroPipeline` `AdapterRail` `MotionToggle`
- auth: `AuthShell` (every pre-auth employee page) `AuthCard`

Two invariants the library enforces, worth knowing before you call anything:

- **A figure that does not exist yet is a dash, never a zero.** `KpiCard` keys
  absence on nullish, never falsiness, so a genuine computed `0` still renders
  `0`. Don't hand-write the dash at a call site.
- **`Modal` needs an exact `ariaLabel`** and keeps the page behind it inert; the
  a11y e2e specs assert both.
### Types (`src/lib/types/`)

- `invoice.ts` — `Invoice`, `InvoiceStatus` (12 statuses), `VALID_TRANSITIONS`, `AdvancedSearchFilters`
- `payment.ts` — `Payment`, `PaymentRun`, `PaymentStatus`, `PaymentMethod` (ach, wire, check, virtual_card + the UK domestic rails bacs / faster_payments / chaps — issue #328). `PAYMENT_METHODS` / `PAYMENT_METHOD_LABEL_KEYS` drift-guarded against the backend rails by `paymentMethod.test.ts`, which also asserts the scheme *names* (ACH, BACS, Faster Payments, CHAPS) stay verbatim in all six locales — a locale that translated one would name a rail that does not exist
- `workflow.ts` — `WorkflowDefinition`, `WorkflowStep`, step configs (extraction, approval, erp_export)
- `admin.ts` — `AdminUser`, `Role` (admin, ap_manager, ap_clerk, cfo)
- `tax.ts` — `Report1099`, `Vendor1099Row` (1099 reporting dashboard)
- `supplierChat.ts` — `ChatThread`, `ChatMessage`, `ChatAttachment`, `ChatTemplate` (AP, full) + masked `PortalChatThread` / `PortalChatMessage` (portal — no `author_user_id`, no mentions)
- `assistant.ts` — `ToolInvocation`, `ChatResponse`, `ConversationSummary` / `ConversationDetail` / `ConversationListResponse`, `UsageResponse`, the five structured tool-result shapes (`VendorSpendResult`, `ForecastResult`, `InvoiceListResult`, `PendingApprovalsResult`, `TextSearchResult`), the UI-side `UiMessage`, and `EXAMPLE_PROMPTS` (the three built-in empty-state prompts). Money fields are string-Decimal — pass to `formatMoney`, never `parseFloat` for display.
- `vendor.ts` — `Vendor` (incl. `screening_status` / `last_screened_at` / `payments_blocked(+_reason)` / `risk_score` / `risk_level`), `SanctionsCheck`, `ScreeningReviewItem`, `VendorRisk`, `RiskSummaryBucket`, the `ScreeningStatus` / `RiskLevel` unions + label maps, and the dual-control change-request shapes — `VendorChangeRequest` / `VendorChangeRequestPage` / `VendorChangeRequestStatus` / `VendorChangeType` plus two pure formatters for the two views of `proposed_value`: `maskedProposalSummary` (the queue list's last-4-only line — it renders the backend's mask and never invents a full account number) and `revealedProposalFields` (the detail dialog's field list, returning `null` — so the caller falls back to raw JSON — whenever it can't flatten the payload without dropping a field)

## Multi-tenant routing

- `src/lib/tenant.ts` + `src/lib/hostRouting.ts` classify the hostname (see
  § Tenant above) — a platform subdomain yields a slug, a customer's vanity
  host deliberately yields none
- `src/lib/api.ts` sends `X-Tenant-Slug` on every request **when there is a
  slug**, and resolves the API origin via `getApiBase()`. On a vanity host it
  sends no slug and calls same-origin `/api`, so the backend resolves the
  tenant from the request `Host` against the org's registered custom domains
- `+layout.svelte` / `portal/+layout.svelte` gate on `hasTenantContext()`, not
  on the slug, so a vanity host boots the app rather than the marketing page

Access via: http://acme.localhost:7777 or http://techflow.localhost:7777

## Design system & UI patterns

**The full pattern library is `frontend/docs/ui-patterns.md`.** Read the relevant
section there before building or restyling a page — reuse the patterns instead of
inventing new ones (guard rail 9), and reach for an existing component before
writing markup.

The rules that hold everywhere, so you know when you need to go read the detail:

- **Page layout** — every list page is the same shell: `PageHeader` → filter chips → `SearchBox` → `DataTable` → pagination. Don't hand-roll a page frame.
- **Filter, sort, search and selection state is URL-backed**, so back/forward and a pasted link reproduce the view. State that lives only in a `$state` rune is a bug.
- **List fetches go through `createRequestSequencer`** — a late response from a superseded request must never overwrite a newer one.
- **Money renders through `<Money>`**, never a hand-rolled `toFixed` or `Intl` call. See `### Money formatting`.
- **User-facing strings go through `t()`** — never a hardcoded literal. See `### Internationalization`.
- **Accessibility is WCAG 2.2 AA and it is tested.** Focus management, keyboard reachability, target size, and reflow at 320 px are guarded by `frontend/tests-e2e/a11y/` (axe-core). Never loosen those specs — fix the markup.
- **Colour comes from the tokens in `app.css`**, never a literal hex in a component. Contrast ratios are computed and asserted; a new token pair must pass 1.4.3.
- **Motion ends on its resting frame, hides nothing by stylesheet, and stops on request.** Continuous animation lives under a `data-motion` root with `MotionToggle` (WCAG 2.2.2); reduced motion collapses durations *and* delays. The five rules are `docs/ui-patterns.md` § Motion.
- **Class names follow the documented convention** rather than utility soup; the conventions section is the reference.
## Conventions

- **Svelte 5 runes** — `$state`, `$derived`, `$effect`, `$props`. No legacy options API.
- **TypeScript** — `lang="ts"` on all `<script>` blocks.
- **API access** — always through `src/lib/api.ts`, never raw `fetch()`.
- **BASE_PATH** — URL prefix for serving under a sub-path. Unset everywhere today (the deploy serves from the CDN root); kept as an escape hatch.
- **No SSR** — static adapter only. Dynamic data comes from the backend API.

### `networkidle` is not a readiness signal (tests-e2e/)

`page.waitForLoadState('networkidle')` waits for 500ms of no network traffic.
Playwright discourages it, and this repo measured what it costs: the
`organization/` directory failed 10 runs in 75 (13.3%) on that call alone, every
failure a 30s timeout whose screenshot showed the panel already fully rendered.
It never asserts that anything was *drawn*, and on a page that polls or streams
it may never fire. **Don't add one.** Wait on the thing the test actually
depends on — which, nine times out of ten, is the auto-waiting assertion already
on the next line.

That last point is stronger here than it sounds, and it is why deleting these is
safe rather than merely usually-safe: `routes/+layout.svelte` renders **nothing**
until its `browser`-guarded `$effect` resolves `hasTenant` (the tri-state guard
that stops the marketing page flashing on a tenant host). Effects run during
neither SSR nor prerender, so no route's markup exists in the served document —
`pnpm build` emits a single `index.html` whose body is an empty
`<div style="display: contents">`, with no `<form>` and no `<input>` in it, and
`adapter-static`'s `fallback` hands that same document to every path under
`vite preview` (what CI serves). So waiting for **any** element in the app shell
transitively proves hydration completed. `fixtures/helpers.ts::signIn` carries
the long-form version of this, because its JSDoc used to claim the opposite.

Three shapes need a real substitute rather than a deletion, and one keeps it:

1. A bare `.count()` or `page.evaluate()` on the next line has no auto-wait —
   gate on the element the read depends on.
2. A `toHaveCount(0)` **absence** assertion passes vacuously against the empty
   pre-hydration document. Put a positive assertion before it; the wait was
   hiding a test that could not fail.
3. Asserting that **no further request** was issued is the one honest use — a
   `waitForResponse` proves at least one fired, never that a duplicate did not.
   `credit-memos/load-sequencing.spec.ts` keeps its two on those grounds and
   says so inline; don't sweep them.

Never substitute `waitForTimeout`, and never raise the 30s timeout to absorb it
(both are masking, see the root `CLAUDE.md` § Fix bugs at the source).

**`pnpm check` does NOT typecheck `tests-e2e/`.** A syntax error there silently
zeroes the whole Playwright suite. After any bulk edit run
`pnpm exec playwright test --config=tests-e2e/playwright.config.ts --list` and
compare the total — note the explicit `--config`, without which the command
picks up the wrong project and reports `Total: 0 tests in 0 files`.

## Web vs Mobile feature parity

The mobile app (`mobile/`) is deliberately a **subset**, focused on the
approve-on-the-go path rather than mirroring every web page. **The authoritative
per-surface table — what mobile ships, what is web-only, and what is deliberately
out of scope — is `mobile/docs/feature-status.md`**; do not maintain a second
list here, because this one went stale (it claimed vendors, exceptions, workflows,
org settings, admin, the payment queue, invoice editing, bulk ops and the audit
timeline were web-only long after each had shipped on mobile).

The parity *direction*, which is what belongs here:

- **A mobile surface mirrors the backend gate of the routes it calls**, per entry,
  not per nav group — a row shown to a role the API refuses is a guaranteed 403 on
  first paint, and a row hidden from a role the API admits is a dead end.
- **Money and statistics cross the wire as exact strings and are rendered
  verbatim.** The device never does float arithmetic on currency; every total is
  server-computed.
- **What stays on the web is a judgment about the decision, not the screen
  size**: configuration surfaces (ERP/payment/SSO secrets, the no-code workflow
  builder, QMS sync), and the money-path *control* surfaces whose refusals need
  room to explain themselves (the adaptive routing / auto-approve-threshold
  applies). Read-first on mobile is a valid shipped state for such a feature.

Mobile has features **not on web**: camera OCR, push notifications, offline mode,
biometric login, swipe-to-approve.

## Deployment

- **S3 + CloudFront**: publishing a GitHub release triggers `.github/workflows/aws-deploy.yml`, whose `Deploy Frontend (S3 + CloudFront)` job builds, `aws s3 sync`s to the bucket with `--delete`, and invalidates the distribution. A published **release** is the only trigger — never a push to `main` and never an ad-hoc manual run — so the deployed artifact always matches a named version, and the job runs in the `production` environment behind its required reviewer.
- Served from the bucket root, so the build sets no `BASE_PATH`.
