# Component library

Every shared component under `frontend/src/lib/components/`, its props, and when
to use it. Extracted from `frontend/CLAUDE.md` to keep that file cheap to load.

**Guard rail 9: build UI from these instead of copy-pasting markup across
routes; extract a new one the second time you would duplicate.** Svelte 5 runes
only (`$state` / `$derived` / `$effect` / `$props`).


Grouped into subfolders by role. Import with the full path, e.g.
`import Modal from '$lib/components/ui/Modal.svelte'`. No barrel/index file.

**`ui/` — reusable primitives** (use these; don't hand-roll the markup):
- `PageHeader.svelte` — `.workspace` + `.toolbar` shell. `<PageHeader title="X">` with an optional `{#snippet actions()}` (right-aligned toolbar buttons); page body is `children`. Renders the `<h1>` title.
- `DataTable.svelte` — `.grid-container > table`. Pass `columns={[{label,class?}]}` (or a `{#snippet header()}<tr>…</tr>{/snippet}` for select-all/sortable headers) + a `{#snippet body()}` that renders the `<tr>`/`<td>` rows. `isEmpty` + `empty` render the centred empty row (`colspan` auto from columns). Opt-in `fixed` (table-layout:fixed) and `stickyHeader` props. The container is a named, keyboard-focusable scroll region (`role="region"`, `tabindex="0"`, `aria-label` = the `ariaLabel` prop or `common.tableRegion`) so a table wider than its card can be panned without a mouse — see `ui-patterns.md` § Accessibility patterns.
- `FilterChips.svelte` — `nav.filters` of `.filter-chip`. `<FilterChips chips={[{key,label,count?,alert?}]} bind:active={var} />`. Single-select; for multi-select status filters keep an inline chip nav (it still uses the global `.filter-chip` CSS).
- `Modal.svelte` — `.backdrop` + `div.modal[role="dialog"]`. `<Modal open ariaLabel="EXACT" title? width="sm|md|lg" onclose>`; keep the page's own `<form>` + `.modal-footer` inside `children` (preserves submit). Custom heading → `{#snippet header()}`. Handles backdrop-click + Esc, and locks background page scroll while open (restores on close) so a wheel event over the backdrop can't bleed through to the list behind it.
- `KpiCard.svelte` — `.kpi` card. `<KpiCard value label highlight={'green'|'red'|null} sub? />`; wrap a row in `<div class="kpi-row">`. `sub` is an optional muted `.kpi-sub` line under the label for a **qualifier on the headline figure** — money the value deliberately excludes, a caveat, a denominator — so the primary value keeps visual priority instead of competing with a second KPI card. Pass `null` (or omit) when there is nothing to qualify; don't use it for a second metric. First use: `/tax`'s Total-reportable card carrying the card-excluded (1099-K) amount.
  **A figure that does not exist yet is a dash, never a zero.** `value` accepts
  `string | number | null | undefined`; nullish renders `KPI_NO_FIGURE`
  (`$lib/utils/kpiValue.ts`, pinned equal to `formatMoney(null)` so the two owners
  cannot drift). Absence is keyed on NULLISH, never falsiness — a genuine computed
  `0` still renders `0`, and that distinction is what the guard protects. Pass
  `pending` while the fetch is in flight: it changes **nothing that is drawn**,
  and instead sets `aria-busy`, hides the dash from the accessibility tree and
  substitutes screen-reader-only loading text. Two glyphs would tell a sighted
  reader nothing; the difference is only actionable to someone who cannot watch
  the card settle. A pending or unavailable card also drops its `highlight` tint —
  the tint is a verdict, and there is no figure to have a verdict about. Don't
  hand-write the dash at a call site (that renders `data-kpi-state="value"`, a
  claim that a figure exists); pass `null`. Guards:
  `utils/kpiValue.test.ts` + `tests-e2e/a11y/kpi-pending.spec.ts`. See
  `docs/decisions.md` §125.
  **A KPI row is never gated on its own response.** `{:else if data}` around a
  `.kpi-row` collapses it while the fetch is in flight, which satisfies §34 by a
  *different mechanism* — so the one convention grows a second shape and a reader
  cannot learn it once. Render the row on every state and pass `pending`; the
  page's existing loading line stays below it and says a newer answer is coming.
  Adopted by every KPI row in the tree: the dashboard (`routes/+page.svelte`),
  `/discounts`, `/bank-reconciliation`, `/cfo`, `/tax`, and the panel-scoped rows on
  `/adaptive` (threshold + feedback), `/admin/access-review`, `/admin/health`,
  `/billing`, `/expenses` (Reports tab), `/audit`, `cfo/ForecastVariancePanel`,
  `CfoMetrics`, `AgentDashboard` and `BudgetModal`'s spend rollup.
  **Two of those keep a gate on purpose** — `/audit` and `ForecastVariancePanel`
  report a sweep the *user* runs, so they gate on `answer || in-flight` rather
  than rendering unconditionally (`docs/decisions.md` §143); dashes before the
  click would claim a figure was coming for a question nobody asked. **The
  dashboard is the one row with another branch to answer to** — a zero-invoice
  `EmptyState` — and it is keyed on `isEmptyTenant` (`!!data &&
  total_invoices === 0`), never on the count alone: gating the row on
  `total_invoices > 0` gates it on the answer, which is the collapse this whole
  rule removes. So an empty tenant sees a pending row hand over to the empty
  state, deliberately (`docs/decisions.md` §154). **A card that may not exist at
  all stays gated on the response** rather than taking `pending` — the
  dashboard's exceptions / stale-approvals / rebates / captured-discount cards
  render only when there is something to report, and a dash promising a figure
  for a card that then vanishes is the same false promise §143 names. **A failed
  load** renders the row `unavailable` — dashes, no `aria-busy`, no tint — with
  the error banner and its retry directly below, which is why /cfo's banner sits
  under its row rather than replacing it.
- `VendorPicker.svelte` — the vendor field on every surface that has one (`/catalogs` ×2, `/contracts`, `/recurring`, `/vendor-statements`, `/discounts`, `/credit-memos`). A WAI-ARIA 1.2 combobox over `searchVendorOptions()`: filtering is server-side so a vendor on page 40 is reachable, and a count line states how much of the matching set is on screen. **Never re-add a `<select>` over a client-side vendor list** — that is the bug this replaced, and it made every vendor past the first 100 unselectable in all six places at once. `bind:value` is the vendor uuid; pass `selectedLabel` from the row's `vendor_name` in edit mode, `label` (or `ariaLabel` for an inline control), and `onselect` if you need the chosen option. Escape is bound with `onkeydowncapture` so it closes the popup, not the enclosing `Modal` — do not "tidy" that to `onkeydown` (`docs/decisions.md` §146).
- `Badge.svelte` — **the** tinted-badge primitive, and the single owner of the `background: var(--<tone>-tint); color: var(--<tone>-on-tint)` recipe. `<Badge tone="accent|success|warning|danger|muted|neutral|erp" variant? title?>{label}</Badge>`. A caller names a *tone*, so it can't spell one wrong, and a tone that is later recalibrated moves in one place. `variant` is passed through as an extra class for **selector hooks only** (`.badge.approved`, `.badge.violation` — the e2e suite reads them); never give a variant a colour rule in the calling component, pick the tone instead. `neutral` is a flat `--bg` chip for the absence of a signal (cancelled / n-a), deliberately not a tint; `erp` is the one measured literal (purple shares no semantic with the five tones, so it stays here rather than becoming a palette token with one caller). **Sizing is fixed on purpose** — call sites varied padding by a pixel or two with no intent behind it. A pill that genuinely needs different metrics is a different component, not a prop: `ScreeningBadge` is the worked example (its own smaller sentence-case metrics, but the palette tokens for colour). Where a status is badged in more than one place, put a `STATUS_TONES: Record<Status, BadgeTone>` map beside the existing `*_STATUS_LABEL_KEYS` in the shared types module so the list page and its modal can't disagree — several did.
- `BrandMark.svelte` — the FeohLedger mark (feoh ᚠ as a split tally stick, `docs/decisions.md` §173) as an `<img>` of `static/logo-mark.svg`. `<BrandMark size={24} label? />`. Decorative by default (`alt=""`): beside the product name the name carries it, and on the auth cards the heading and `<title>` do; pass `label` only where it is the one thing naming the product in view (the collapsed sidebar rail). **Platform chrome only**: anything a white-label tenant sees picks its mark through `brand.mark` (`brandTheme.ts::brandMark`), which resolves to this only while the product still carries the platform's name. Used by `Sidebar`, `Landing`, `AuthShell`'s brand panel, `AuthCard`, the login, MFA, signup, verify and change-password pages, and the supplier portal's header and login card (through `portalBrand.mark`). The SVG is generated — change `assets/logo-render/gen_svg.py` and run `pnpm gen:icons`, never edit the file.
- `SearchBox`, `StatusBadge`, `RowAction`, `BulkBar`, `BulkDeleteButton`, `Toast` — see the pattern sections below.
- `Tabs.svelte` — underline tab bar for **in-page** panel switching. `<Tabs tabs={[{key,label,count?}]} bind:active ariaLabel? onchange? />`. Owns the `.tab-row` / `.tab` markup + `role="tablist"`/`role="tab"` a11y. The per-route tab copies in `/expenses`, `/payments`, `/audit` predate it and can migrate onto it opportunistically. (Distinct from `layout/SectionTabs.svelte`, which renders the sidebar group's *cross-route* sub-tabs as anchors — that's not this component.)
- `ScreeningBadge.svelte` — sanctions-screening + vendor-risk pill. `<ScreeningBadge screening={v.screening_status} risk={v.risk_level} blocked={v.payments_blocked} adverseMedia={v.adverse_media} />`. Tone map: clear=green, review/medium=amber, match/high/critical/blocked=red, unscreened/low=grey. `adverseMedia` adds an amber **Negative news** pill for an adverse-media (negative-news) screening hit — it reads *alongside* the verdict, not instead of it, because "review the relationship" is a different instruction than a watchlist match. Reuses the existing calibrated tone classes; don't hand-roll a new pill colour. Shared by the vendor list cell, `/vendors/screening`, and `VendorModal`.
- `SubscriptionBadge.svelte` — platform-billing subscription-status pill. `<SubscriptionBadge status={sub.status} />` for the four states (`trialing`/`active`/`past_due`/`canceled`); WCAG-1.4.3-calibrated tones matching `StatusBadge`. Used by `/billing`.
- `LinkedMessage.svelte` — a translated sentence with links inside it. `<LinkedMessage text={m('key')} links={{ token: { href, label } }} />`. The catalogue entry carries `{token}` markers; this splits the message around them (`i18n/segments.ts::splitTokens`) and renders one anchor per token, so the **translation** decides where each link falls. Use it instead of cutting the sentence into `…Pre`/`…Post` catalogue entries with the anchor written between them — fragments fix the link order, which German and Japanese both need to change, and a translator handed three of them can only reproduce English word order or mistranslate. An unknown `{token}` is left visible rather than swallowed (a stray marker gets reported; a silently dropped link does not), and `messages_parity.test.ts` already fails any locale whose placeholder set differs from English, so a translation cannot lose a link. Renders anchors it builds itself and binds the text as text — no `{@html}`. Used by the signup consent line (three links, `auth.signup.legalConsent`). For a link to a published legal document, take the label from `legal/pages.ts::legalTitle(path)` rather than typing the title.
- `SecretReveal.svelte` — **the** one-time credential reveal dialog. `<SecretReveal open ariaLabel heading warningStrong warning secret testId copyLabel copiedLabel copiedToast copyFailedToast doneLabel meta? onclose />` (+ an optional `{#snippet note()}` under the meta rows). Wraps `Modal`; renders the plaintext in a `user-select:all` `<code>` carrying `testId`, a clipboard Copy button with a "Copied" acknowledgement, and the shown-once warning banner. **The secret is a prop, never state** — the component neither stores, caches nor logs it, and the caller drops its own copy in `onclose`, so the value leaves the DOM with the dialog. Every string is passed in already-localized (the component is i18n-agnostic; each caller keeps its own key namespace). Used by the API-key mint (`/admin/api-keys`) and both webhook secret reveals — create and rotate (`/admin/webhooks`). Use this for any new "shown once, never retrievable" value; don't hand-roll a third copy.
- `FieldWarning.svelte` — inline advisory attached to a form field: "this is
  legal, and here is what it will cost you". `<FieldWarning show message />`
  (the message arrives already-localized; the component is i18n-agnostic).
  `role="status"` / polite, because it updates as the user types — an
  assertive region would interrupt on every keystroke. Distinct from a toast
  (transient, on submit) and from the `role="alert"` refusal panels (a request
  the server rejected). First use: the brand strong-accent contrast advisory on
  `/organization` + `/admin/partner`.
- `Money.svelte` — locale-aware currency display. `<Money amount={row.amount} currency={row.currency} />`. Opt-in `whole` (no decimals), `accounting` (parenthesised negatives), `mono` (tabular-nums). Over `utils/money.ts::formatMoney`; see *Money formatting* above. Use this (or `formatMoney` in script) for every currency value — don't write `Intl.NumberFormat` inline.
- `EmptyState.svelte` — the first-run / zero-data affordance: an optional
  generated illustration `art` (`invoices` · `vendors` · `payments` · `inbox` ·
  `chart` · `shield` · `workflow` · `documents` — preferred) or the older emoji
  `icon`, a `heading`, a `description`, and an optional primary action rendered
  as a `<button>` (`onaction`) or `<a>` (`actionHref`). i18n-agnostic (strings
  passed in already-localized, like `FieldWarning`). **Render it ONLY for the
  genuinely-empty-and-unfiltered case** — `loading`, `errored`, and "a filter
  matched nothing" keep their own copy (§ Data tables, "empty must distinguish
  loading / errored / genuinely-empty"). Adopted on the dashboard (zero
  invoices → link to `/invoices`), `/invoices` (zero rows, no filter → the
  upload action, role-gated), and `/portal/invoices` (vendor submitted nothing
  → the submit action). The page keeps its `DataTable` for every other state.
  The illustrations are data, not files: `assets/illustrations/gen_empty_states.py`
  writes `ui/emptyStateArt.generated.ts` (`pnpm gen:illustrations`, guarded by
  `pnpm check:illustrations` in CI), and the component renders each shape as
  inline SVG whose tone classes take the palette's muted stroke and the tenant's
  `--accent`. Add a new illustration in the generator, never in the module.

The visual styling for all of the above lives **globally in `src/app.css`** (class-scoped: `.workspace`, `.grid-container td`, `.filter-chip`, `.modal`, `.kpi`, …) so route pages carry no duplicated `<style>`. Feature components below keep their own scoped CSS (Svelte's `.svelte-<hash>` outranks the bare-class globals).

**`modals/` — feature dialogs:**
- `InvoiceModal.svelte` — invoice detail/edit modal. **Line-total reconciliation:** `saveLineItems` reads `PUT /api/invoices/{id}/line-items`'s `{saved, line_items_total, header_amount, reconciles_with_header}` and, on a divergence, renders a persistent `role="alert"` panel (`[data-testid="line-total-mismatch"]`) naming both figures via `<Money>` plus the money consequence ("cannot enter a payment run" — `line_total_mismatch` is payment-blocking). Response-driven **by necessity**: the `invoice` prop is a snapshot the store's refetch doesn't refresh, so the warning the save just raised isn't on it. Never computes a delta client-side (that would be float money math). See `backend/docs/line-total-reconciliation.md` § What the editor sees. **Approver picker:** `GET /api/invoices/assignable-reviewers` is its ONLY source — the admin-only `GET /api/admin/users` fallback is gone. That endpoint gates on exactly what `POST /invoices/{id}/assign` gates on, so a CFO gets a 403 from it too (deliberately — a CFO cannot assign either), which makes the submit-UNASSIGNED path load-bearing for a whole role rather than a failure cushion: `approverRequired` is false whenever the list is empty, and the note explains that the invoice goes to the queue unassigned. Don't "fix" the CFO 403 by widening the endpoint. `tests-e2e/invoices/approver-picker.spec.ts` asserts the admin directory is never called, from either role. **Chat @mention picker:** the same rule, a different endpoint — the `members` prop it hands `SupplierChatThread` comes from `GET /api/invoices/chat/mentionable-users` (`getChatMentionableUsers`), never `adminStore.users` and never the admin directory. See that component's entry under `chat/` for why neither existing endpoint fits.
- `AdvancedSearchModal.svelte` — invoice search filters
- `BulkRecodeGLModal.svelte` — admin bulk GL re-code preview/apply
- `GlAccountModal.svelte` — create one chart-of-accounts row (`POST /api/gl-accounts`, admin | ap_manager). **It states which chart the account will land in before creating it** — the backend decides that from the live `X-Entity-ID`, not from the body (consolidated ⇒ SHARED across every entity, an entity selected ⇒ that entity's own), the difference is otherwise invisible in the form, and there is no PATCH on that router to correct it afterwards. The hint renders only when `entityStore.multiEntity`, the switcher's own gate. Surfaces the backend's own 409 message verbatim — it names the code AND the chart it already exists in, which is exactly what the user has to change.
- `ApprovalMatrixEditor.svelte` — approval-chain matrix builder
- `RunDetailModal.svelte` — payment run detail; status, total, payments table; Execute button when run is `draft`
- `VendorModal.svelte` — vendor detail modal; the "Screening & Risk" panel (status, last-screened, payment-block + reason, risk level/score), re-screen / recompute-risk / block-unblock actions (gated to admin + ap_manager), and the screening-history timeline. Over `$lib/api/vendors.ts`

**`chat/` — supplier collaboration:**
- `SupplierChatThread.svelte` — surface-agnostic per-invoice chat thread shared
  by the AP modal (`surface="ap"`) and the supplier portal (`surface="vendor"`).
  Never imports `api`/`portalApi`; the caller injects `onsend`/`onresolve`/
  `onreopen`/`ondownload`. Renders message bubbles (own-role right-aligned),
  plain-text body (never `{@html}`), attachment chips, relative time, and on the
  AP side @mention autocomplete + a template picker + resolve/reopen. AP calls
  go through `$lib/api/supplierChat.ts` (over `api`); portal calls through
  `$lib/portalChat.ts` (over `portalApi`). Types in `$lib/types/supplierChat.ts`
  (full `Chat*` for AP, masked `PortalChat*` for the portal — no internal id).
  **The `members` prop — the @mention picker's candidates — comes from
  `GET /api/invoices/chat/mentionable-users`
  (`$lib/api/supplierChat.ts::getChatMentionableUsers`), NOT from
  `adminStore.users` and NOT from the admin directory.** It read the store for
  a while, and that store is only populated by `/admin` and `/workflows/[id]`,
  so on `/invoices` — the one route this component's AP host is reachable from
  — the dropdown was permanently empty, while arriving via `/admin` first
  silently made it work. The endpoint is gated on `get_current_user`, exactly
  what posting a mention requires; `GET /api/admin/users` is not the fix
  (admin-only, so every non-admin 403s — the same mistake the approver picker
  made) and neither is `assignable-reviewers` (admin/manager-only and scoped to
  `invoice.approve` holders, while a clerk or CFO can post to this thread and
  is an ordinary person to mention). The prop type is `ChatMentionCandidate`,
  deliberately narrower than `AdminUser`: **there is no email on it**, which is
  what keeps one out of the dropdown — the picker used to render every
  candidate's address under their name. The vendor surface passes
  `members={[]}` and the picker is `{#if isAp}`-gated besides. See
  `backend/docs/supplier-chat.md` § Who can be mentioned; guards
  `backend/tests/test_chat_mentionable_users.py` +
  `tests-e2e/invoices/chat-mentions.spec.ts`.

**`portal/` — supplier-portal-only components:**
- `PortalListFilters.svelte` — the filter bar for the portal invoice + payment
  lists: a debounced number `<input type="search">`, a From/To `<input
  type="date">` pair, and a single-select row of vendor-facing "phase" chips.
  Owns the phase selection, the search text, the dates AND the 300ms debounce,
  and hands the parent a resolved `{ phase, search, dateFrom, dateTo }` via
  `onchange` (phase + date changes fire immediately, search after typing stops).
  Because the debounce lives here, the parent's `load()` is never reached from a
  reactive `$effect` and needs no `untrack` (issue #168). Strings are passed in
  already-localized; `bind:this` exposes `reset()` for a "Clear filters" empty
  state. The phase→raw-status maps are `PORTAL_INVOICE_PHASES` /
  `PORTAL_PAYMENT_PHASES` in `$lib/types/portalStatus.ts`, both **derived from**
  the existing label maps so they can't drift.

**`assistant/` — Conversational AP Assistant (`/assistant`):**
- `ChatMessage.svelte` — one chat bubble (own-role right-aligned). Renders the
  message's tool results (via `ToolResultView`) before the prose, the prose as
  plain text (never `{@html}`), an inline error, or a typing indicator while a
  streamed reply is still arriving.
- `ToolResultView.svelte` — dispatches a `ToolInvocation` to the right view per
  tool name: `get_vendor_spend` / `get_payment_forecast` → `SpendBarChart`;
  `list_invoices` / `list_pending_approvals` → a compact table (reuses
  `StatusBadge` + `Money`); `find_invoices_by_text` → snippet cards. Any
  unrecognised tool falls back to a formatted-JSON view. Each card carries
  `data-tool="<name>"` (e2e selector).
- `SpendBarChart.svelte` — horizontal CSS bar chart (mirrors the CFO dashboard's
  `.cf-bar*` recipe; no charting dependency). Takes `bars=[{label, value,
  amountLabel, sub?}]`; the parent formats money via `formatMoney` and passes
  the numeric `value` only to drive bar width.
- `ExamplePrompts.svelte` — empty-state with the three built-in roadmap prompts;
  `onpick(prompt)` fills + sends.
- `UsageMeter.svelte` — AI token usage bar over `GET /api/assistant/usage`
  (`data-testid="usage-meter"`). Budget `0` = unlimited (running total, no bar);
  amber ≥80%, red at/over budget.

**`workflow-builder/`** — drag-and-drop no-code builder canvas for the
`/workflows/[id]` editor (step palette, canvas nodes, SVG connectors;
native HTML5 drag-and-drop, no svelte-flow).

**`workflow-mgmt/`** — no-code builder management dialogs mounted on the
`/workflows` list page: `TemplateLibraryModal` (start from a template),
`VersionHistoryModal` (diff + restore versions), `SimulationModal` (dry-run a
sample invoice through the pipeline), and `ImportExportControls` (import a
pasted/uploaded definition; the `exportWorkflowToFile` module helper downloads
a definition as JSON). All wrap the shared `ui/Modal.svelte` and call the
`workflowStore` builder methods.

**`marketing/`** — the public no-tenant page. Motion rules for everything here are in `ui-patterns.md` § Motion.
- `Landing.svelte` — the page. Its first `<span class="eyebrow">` and `<p class="lede">` are read verbatim by `socialPreviewCard.test.ts` against `app.html`'s Open Graph tags, so change them together. Its `stats` are substantiated counts (see the comment above them); `use:countUp` animates them but the markup holds the final figure.
- `Pricing.svelte` — plan cards + billing toggle.
- `Atmosphere.svelte` — `aria-hidden` backdrop layer (named to avoid app.css's global modal `.backdrop` class, which it would otherwise inherit): aurora blobs, ruled grid, generated grain, vignette. `<Atmosphere fixed? dense? still? />`. `fixed` pins it to the viewport (the long landing page); `dense` tightens it for a narrow panel; `still` holds the aurora at rest (the auth pages and the legal frame, which carry no pause control — see Motion). Consumers: `Landing`, `AuthShell`, and `routes/legal/+layout.svelte` — the last brand-NEUTRAL by requirement, since `/legal` publishes the operator's own documents and must not wear a tenant's accent (#433). It reads no `--accent`, which is what makes that true.
- `HeroPipeline.svelte` — the `aria-hidden` animated hero: one invoice assembling field by field, then matched and paid, over a four-stage rail. A drawing of the workflow, not a screenshot. Two clocks: `--build` (one-shot assembly) and `--loop` (ambient). Every keyframe ends on the finished card.
- `AdapterRail.svelte` — scrolling rail of **shipped adapters**, as text word marks. Names only providers with a registered adapter; remove an entry in the same change that removes the adapter.
- `MotionToggle.svelte` — the WCAG 2.2.2 pause control. `<MotionToggle bind:paused pauseLabel? playLabel? />`; the caller puts `data-motion={paused ? 'paused' : 'running'}` on its root and `app.css` pauses every animation beneath it.

**`auth/`** — pre-auth employee pages.
- `AuthShell.svelte` — split-screen frame: the page's form, plus a platform-branded panel (tagline, page-specific `panel` snippet, the tally render). `<AuthShell panelOnMobile?>{form}{#snippet panel()}…{/snippet}</AuthShell>`. Owns the shared field, submit-button and `.error` styling via `:global(:where(…))` under `.form-wrap` — zero added specificity, so a page's own scoped rule wins without hacks (signup's composite slug field, MFA's code input). DOM order is form then panel. Used by signup, login, MFA, verify, change-password and `AuthCard`. **Not** the supplier portal's sign-in, which carries tenant branding.
- `AuthCard.svelte` — heading + subtitle + error for the secondary pages (forgot / reset password), rendered inside `AuthShell`. `<AuthCard heading subtitle? error?>`. Keeps the `.auth-card` class as the e2e hook.
**`layout/`** — the shared left side panel:
- `Sidebar.svelte` — collapsed/expanded nav + profile popover. The nav is
  driven by **`$lib/nav.ts`** (the single source of truth, also read by
  `SectionTabs`): high-traffic destinations are top-level `link`s; the rest are
  folded into `group`s (Procurement / Billing / Insights / Automation /
  Governance / Settings) that show
  ONE sidebar row and open a sub-tabbed page. A group's row links to the first
  child the current role can see; a group hides when the role can see none.
  Add/move a route by editing `$lib/nav.ts` (with its `roles` gate) — don't
  hand-roll nav rows in the component.
  **A `roles` gate is per ENTRY, derived from that route's own backend gate —
  never a blanket list applied across a group.** A group's children routinely
  disagree: in Procurement, `/purchase-orders` and `/goods-receipts` are
  `get_current_user` (auth-gated, role-open) while `/budgets` is
  `require_roles(ADMIN, AP_MANAGER, CFO)`, and a shared list cannot express
  both. Copying a sibling's gate is how `ap_clerk` lost the two pages whose own
  code says a clerk reads them.
  Both directions are bugs, and the second is worse. **Too narrow** is a dead
  end — a page the backend serves with no link to it. **Too wide** is a link
  whose first paint 403s, and `permissions` makes it easy: `/payments` OR's in
  `payment.execute` / `payment.void` for an SoD custom role, but the page's
  mount effect fired three reads still on `require_roles`, so that row led
  straight to three 403s. When you widen a nav entry, check what the PAGE loads
  on mount, not just the endpoint the row is named after. Guard:
  `src/lib/nav.test.ts` pins the exact Procurement link set each system role
  sees, and asserts each entry carries its own gate.
- `SectionTabs.svelte` — the per-page section sub-tab bar, rendered once in
  `routes/+layout.svelte` above the page slot. For a grouped route it renders
  the group's RBAC-visible children as tabs (suppressed when ≤1 is visible);
  top-level routes get no bar.
  **The row can never widen the page.** It was a flex row of `nowrap` children
  with no `flex-wrap` and no `overflow-x`, so a group with more tabs than fit
  pushed the DOCUMENT wider and the whole page scrolled sideways — clipping the
  sidebar, and failing WCAG 1.4.10 (Reflow). Settings carried 15 tabs; Billing
  still carries 9, which overflows a 1280px laptop. Now: `.section-tabs-row` is
  `overflow: hidden` as an unconditional backstop, and whatever does not fit
  moves into a **More** menu measured off an off-screen copy of the row.
  Two traps if you touch it:
  - The measurer is wider than the viewport by design, and `position: absolute`
    + `visibility: hidden` does **not** remove an element from the document's
    scrollable area. It lives inside a zero-height `overflow: hidden` wrapper
    for exactly that reason — dropping the wrapper reintroduces the scrollbar
    (372px of it at 1000px), which is a bug the component's own e2e caught.
  - The measurer renders `<span>`s, not `<a>`s: focusable content inside
    `aria-hidden` is reachable by keyboard while hidden from assistive tech.
  The **active** tab is always pulled into the visible row, so a section never
  renders with nothing selected. Guards: `tests-e2e/a11y/section-tabs.spec.ts`
  (overflow, the menu, Escape/focus return) and `tests-e2e/a11y/reflow.spec.ts`,
  which walks every route under `src/routes` — so every grouped route renders
  this bar under the 320px measurement, rather than the two a hand-kept list
  happened to name.
- `NotificationBell.svelte` — bell + unread badge in the sidebar header with a
  recent-notifications popover (replaced the old Notifications nav row; the full
  `/notifications` page is the "View all" target). Closes on Esc / backdrop.
- `EntitySwitcher.svelte` — multi-entity (subsidiary) selector; hidden for
  single-entity tenants.

