/**
 * Single source of truth for the app's primary navigation.
 *
 * Two kinds of entries:
 *  - `link`  — a high-traffic destination that gets its own sidebar row.
 *  - `group` — a cluster of lower-traffic routes folded behind ONE sidebar row.
 *              Clicking it lands on its first accessible child; the page then
 *              shows the group's children as a sub-tab bar (see
 *              `components/layout/SectionTabs.svelte`).
 *
 * Both the sidebar (`components/layout/Sidebar.svelte`) and the section sub-tab
 * bar read from `NAV` so the structure + RBAC live in exactly one place.
 *
 * `roles` is the at-least-one-of gate (omitted = visible to every role). A
 * group is visible when at least one child is; its sidebar link points at the
 * first child the current role can see. Child order = sub-tab order.
 *
 * `permissions` is an OR alternative to `roles` — an entry is visible if the
 * caller matches `roles` (when given) OR holds ANY of `permissions` (when
 * given). It exists for the granular permission layer (`auth.can`, mirroring
 * backend `require_permission`): a custom role holding only a splittable
 * permission (e.g. `user.manage`) should see the nav entry its permission
 * unlocks even though it doesn't hold the system role the entry also lists.
 * Omit `permissions` for anything not behind `require_permission` on the
 * backend.
 *
 * i18n: each entry carries a `labelKey` (an i18n message key) — the single
 * source of truth for the *translated* display string. The bare `label` is
 * retained as a stable, locale-independent identifier (used as an `{#each}`
 * key and as the English fallback); UI surfaces render `m(entry.labelKey)`,
 * not `entry.label`.
 *
 * **Keep this module pure** — no `$env/*`, `$app/*`, store or `.svelte`
 * import. `tests-e2e/auth/rbac.spec.ts` value-imports it to compute each
 * role's expected sidebar, and Playwright's loader resolves `$lib` but not
 * SvelteKit's virtual modules (`frontend/CLAUDE.md` § `pnpm check` does not
 * cover `tests-e2e/`). The per-row gates themselves are pinned as a literal
 * table in `nav.test.ts`; change a row's `roles` here and that table in the
 * same commit.
 */

import type { MessageKey } from '$lib/i18n/messages';
import { PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID, PERM_USER_MANAGE } from '$lib/types/admin';

/**
 * Every icon key `Sidebar.svelte` can draw. A union rather than `string` so a
 * typo or a key with no matching branch fails typecheck instead of rendering a
 * blank 20x20 box — the if/else chain there has no `else`, so an unknown key
 * was silently invisible.
 */
export type NavIcon =
	| 'dashboard'
	| 'invoices'
	| 'payments'
	| 'vendors'
	| 'screening'
	| 'bankChange'
	| 'exceptions'
	| 'cart'
	| 'receipt'
	| 'assistant'
	| 'automation'
	| 'governance'
	| 'settings';

export interface NavLink {
	kind: 'link';
	label: string;
	labelKey: MessageKey;
	href: string;
	icon: NavIcon;
	roles?: string[];
	/**
	 * OR'd with `roles` — visible if the user holds ANY of these granular
	 * permissions too, even without a listed role. Exists for a custom-role
	 * grantee: `roles` alone means a role gate hides the nav row entirely for
	 * someone whose ONLY access is a granular permission (e.g. `payment.execute`
	 * with no `admin`/`ap_manager`/`cfo` role), stranding them on a page they
	 * can't navigate to despite the backend letting every call through. A
	 * single-permission entry is just a one-element array (e.g. `[PERM_USER_MANAGE]`).
	 */
	permissions?: string[];
}

export interface NavChild {
	label: string;
	labelKey: MessageKey;
	href: string;
	roles?: string[];
	/** See `NavLink.permissions`. */
	permissions?: string[];
}

export interface NavGroup {
	kind: 'group';
	label: string;
	labelKey: MessageKey;
	icon: NavIcon;
	children: NavChild[];
}

export type NavEntry = NavLink | NavGroup;

/** Matches the signature of `auth.hasAnyRole`. */
export type RoleCheck = (...roles: string[]) => boolean;

/** Matches the signature of `auth.can`. */
export type PermissionCheck = (perm: string) => boolean;

export const NAV: NavEntry[] = [
	{ kind: 'link', label: 'Dashboard', labelKey: 'nav.dashboard', href: '/', icon: 'dashboard' },
	{ kind: 'link', label: 'Invoices', labelKey: 'nav.invoices', href: '/invoices', icon: 'invoices' },
	// `permissions` OR's in a custom role holding ONLY `payment.execute` /
	// `payment.void` (no `admin`/`ap_manager`/`cfo` role) — those are the two
	// granular permissions whose supporting read endpoints
	// (GET /api/payments, GET /api/payments/{id}, GET /api/payments/runs/,
	// GET /api/payments/runs/{id}) are gated with `require_permission` for
	// exactly this reason. Without this the sidebar row — the only way to
	// reach the page — stayed role-gated even though every call the page
	// makes would succeed.
	{
		kind: 'link',
		label: 'Payments',
		labelKey: 'nav.payments',
		href: '/payments',
		icon: 'payments',
		roles: ['admin', 'ap_manager', 'cfo'],
		permissions: [PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID]
	},
	{ kind: 'link', label: 'Vendors', labelKey: 'nav.vendors', href: '/vendors', icon: 'vendors', roles: ['admin', 'ap_manager', 'cfo'] },
	// Sanctions-screening review queue (a sub-route of /vendors). Reuses the
	// existing `vendors.col.screening` message ("Screening") as its label — a
	// dedicated `nav.screening` key is a later i18n slice (Lane A owns locales).
	// ap_clerk included deliberately: both endpoints this page calls —
	// `GET /api/vendors/screening/review-queue` and `.../screening-history` —
	// are require_roles(ADMIN, AP_MANAGER, AP_CLERK, CFO). Every mutating
	// control on it is gated further (re-screen on `auth.isManager`,
	// block/unblock on the `vendor.block` permission), so it renders read-only
	// for a clerk and hiding the row was a dead end, not a gate.
	{ kind: 'link', label: 'Screening', labelKey: 'vendors.col.screening', href: '/vendors/screening', icon: 'screening', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
	// Vendor bank / tax change-approval queue (a second sub-route of /vendors).
	// The dual-control BEC gate's only UI: a staged change never applies until
	// a SECOND user signs it off, so without a nav row the queue — and with it
	// the ability to change vendor banking at all — is unreachable.
	// admin | ap_manager mirrors the backend list gate
	// (`require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)`); note CFO is deliberately
	// absent, matching the API. The *approve* action inside the page is gated
	// further, on the granular `vendor.bank_change.approve` permission.
	{ kind: 'link', label: 'Bank Changes', labelKey: 'vendors.changeRequests.navLabel', href: '/vendors/change-requests', icon: 'bankChange', roles: ['admin', 'ap_manager'] },
	{ kind: 'link', label: 'Exceptions', labelKey: 'nav.exceptions', href: '/exceptions', icon: 'exceptions', roles: ['admin', 'ap_manager'] },
	{
		kind: 'group',
		label: 'Procurement',
		labelKey: 'nav.group.procurement',
		icon: 'cart',
		// Gated PER ITEM, against each route's OWN backend gate — never one
		// blanket group role list. The two reads below are `get_current_user`
		// (auth-gated, role-open) while `/budgets` is genuinely narrower, and a
		// group-level gate cannot express both.
		children: [
			// ap_clerk included deliberately: `GET /api/purchase-orders`, its
			// `/counts` sibling and the detail route are ALL `get_current_user` —
			// the counts route says so in its own docstring ("RBAC matches the
			// list itself — auth-gated, role-open"). The only mutation,
			// `POST /sync-erp`, is admin | ap_manager and is already gated in-page
			// on `auth.isManager`, so the page renders read-only for a clerk.
			// Hiding the row was a dead end, not a gate — and PO data is exactly
			// what 3-way matching sends a clerk to chase.
			{ label: 'Purchase Orders', labelKey: 'nav.purchaseOrders', href: '/purchase-orders', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// ap_clerk included deliberately, and this one contradicted the page's
			// own header comment: the receipts list + detail (`api/goods_receipts.py`)
			// and the Inspections tab (`GET /api/inspections`) are all
			// `get_current_user`, and `/goods-receipts/+page.svelte` documents
			// "a clerk sees every inspection and no button". The clerk the page was
			// written for could not reach it from the sidebar.
			{ label: 'Goods Receipts', labelKey: 'nav.goodsReceipts', href: '/goods-receipts', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Requisitions', labelKey: 'nav.requisitions', href: '/requisitions', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Intake', labelKey: 'nav.intake', href: '/intake', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Catalogs', labelKey: 'nav.catalogs', href: '/catalogs', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// Chart of accounts — the third ERP-synced master data set behind
			// 3-way matching, beside the two that already had a page
			// (`/purchase-orders` here, `/vendors` as a top-level row). It sits in
			// Procurement rather than Settings because the chart is the CODING
			// TARGET of every document in this group (requisition → PO → invoice
			// line) and of `/budgets` below it, whose `gl_account` dimension keys
			// on it; and because a clerk-visible row is the only child a clerk can
			// see in Settings, which would make "Settings" a group containing one
			// reference table they cannot change.
			//
			// ap_clerk included deliberately, for the reason the four rows above
			// state: `GET /api/gl-accounts` is `get_current_user` — auth-gated,
			// role-open — because a clerk coding an invoice has to be able to look
			// a code up, and the invoice/expense/requisition GL pickers already
			// serve them that same list. Both writes (`POST ""` create and
			// `POST /sync-erp`) are admin | ap_manager and are gated in-page on
			// `auth.isManager`, matching the backend, so the page renders read-only
			// for a clerk. Gating the ROW on the write instead would have hidden a
			// page whose every read succeeds — the dead end nav.ts has now fixed
			// five times — while leaving `sync-erp` unreachable for the one role
			// the backend deliberately widened it to (docs/decisions.md §161).
			{ label: 'Chart of Accounts', labelKey: 'nav.glAccounts', href: '/gl-accounts', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// ap_clerk EXCLUDED deliberately, unlike its four siblings: every
			// `/api/budgets` read is `require_roles(ADMIN, AP_MANAGER, CFO)` and
			// mutation is admin | cfo (the CFO owns budgets), so a clerk's first
			// page load would 403. Per-item gating is what lets this stay narrow
			// while the rows above widen.
			{ label: 'Budgets', labelKey: 'nav.budgets', href: '/budgets', roles: ['admin', 'ap_manager', 'cfo'] },
		],
	},
	{
		kind: 'group',
		label: 'Billing',
		labelKey: 'nav.group.billing',
		icon: 'receipt',
		children: [
			{ label: 'Contracts', labelKey: 'nav.contracts', href: '/contracts', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Expenses', labelKey: 'nav.expenses', href: '/expenses', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// ap_clerk included deliberately: `GET /api/credit-memos` is
			// require_roles(ADMIN, AP_MANAGER, AP_CLERK, CFO). Create / apply /
			// void are admin | ap_manager and are gated on the page, so it
			// renders read-only for a clerk.
			{ label: 'Credit Memos', labelKey: 'nav.creditMemos', href: '/credit-memos', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// ap_clerk included deliberately: `api/discounts.py::_READ_ROLES`
			// grants a clerk the dashboard, the offer list and the per-invoice ROI.
			// The page renders read-only for them (accept/decline stay gated on
			// admin/ap_manager/cfo), so hiding the link was a dead end, not a gate.
			{ label: 'Discounts', labelKey: 'nav.discounts', href: '/discounts', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// ap_clerk included deliberately: `api/recurring.py::_READ_ROLES`
			// includes ROLE_AP_CLERK. Create and every row action are behind
			// `auth.isManager` on the page, matching `_WRITE_ROLES`.
			{ label: 'Recurring', labelKey: 'nav.recurring', href: '/recurring', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Statements', labelKey: 'nav.statements', href: '/vendor-statements', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Positive Pay', labelKey: 'nav.positivePay', href: '/positive-pay', roles: ['admin', 'ap_manager', 'cfo'] },
			// Bank reconciliation — import a bank statement and confirm every
			// payment we think we made actually cleared. Sits beside Positive Pay
			// (the other treasury/bank-file surface) deliberately.
			//
			// ap_clerk IS included here, unlike Positive Pay: `api/bank_reconciliation
			// .py::_READ_ROLES` is (ADMIN, AP_MANAGER, AP_CLERK, CFO) — a clerk works
			// the reconciliation queue. Every mutating control on the page is gated
			// further on `auth.isManager`, matching that router's `_WRITE_ROLES`
			// (admin | ap_manager — treasury-adjacent, clerks excluded), so the page
			// renders read-only for a clerk and hiding the row would be a dead end
			// rather than a gate.
			{ label: 'Bank Reconciliation', labelKey: 'bankRecon.navLabel', href: '/bank-reconciliation', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			// Platform billing — the AP platform's OWN subscription / plan / usage
			// (control-plane), distinct from the customer AP money path above.
			// Admin/CFO only (the backend GET /api/billing/subscription 403s the rest).
			{ label: 'Subscription', labelKey: 'nav.platformBilling', href: '/billing', roles: ['admin', 'cfo'] },
		],
	},
	{
		kind: 'group',
		label: 'Insights',
		labelKey: 'nav.group.insights',
		icon: 'assistant',
		children: [
			{ label: 'AI Assistant', labelKey: 'nav.aiAssistant', href: '/assistant', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
			{ label: 'Cash Flow', labelKey: 'nav.cashFlow', href: '/cfo', roles: ['admin', 'cfo'] },
			// Conversational cash-flow copilot (finance leaders — mirrors the
			// /api/cash-flow façade RBAC; distinct from the /cfo analytics dashboard).
			{ label: 'Cash-Flow Copilot', labelKey: 'nav.cashFlowCopilot', href: '/cash-flow', roles: ['admin', 'ap_manager', 'cfo'] },
			{ label: '1099 Reporting', labelKey: 'nav.taxReporting', href: '/tax', roles: ['admin', 'ap_manager', 'cfo'] },
			// Ad-hoc / custom Report Builder — read is all four roles (the backend
			// gates saving/patching/deleting a definition to admin/ap_manager/cfo).
			{ label: 'Report Builder', labelKey: 'nav.reports', href: '/reports', roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'] },
		],
	},
	// Workflow authoring + the AI that tunes it. Split out of Settings: these
	// are operational surfaces an AP manager works in, not org configuration,
	// and burying them under a 15-tab Settings bar hid them.
	{
		kind: 'group',
		label: 'Automation',
		labelKey: 'nav.group.automation',
		icon: 'automation',
		children: [
			{ label: 'Workflows', labelKey: 'nav.workflows', href: '/workflows', roles: ['admin'] },
			// A/B testing of workflow rules — compare two configs on objective
			// metrics. Read for managers/CFO; mutate is admin (backend RBAC).
			{ label: 'Experiments', labelKey: 'nav.experiments', href: '/experiments', roles: ['admin', 'ap_manager', 'cfo'] },
			// Adaptive AI workflows — approval-pattern learning, baseline
			// anomalies, advisory suggestions, smart routing, the auto-approve
			// threshold recommendation and the feedback loop. Read is
			// admin/ap_manager/cfo (the backend's `_READ_ROLES`); the two acts are
			// gated further inside the page (dismiss/route-apply ap_manager+,
			// threshold apply admin-only — it edits a workflow definition).
			{ label: 'Adaptive Workflows', labelKey: 'nav.adaptive', href: '/adaptive', roles: ['admin', 'ap_manager', 'cfo'] },
		],
	},
	// The SOX / GDPR evidence surfaces. Split out of Settings because an
	// auditor or CFO comes here to READ a record, not to change a setting —
	// and `Audit Trail` is the single most-looked-for page in the section.
	{
		kind: 'group',
		label: 'Governance',
		labelKey: 'nav.group.governance',
		icon: 'governance',
		children: [
			{ label: 'Audit Trail', labelKey: 'nav.auditTrail', href: '/audit', roles: ['admin', 'cfo'] },
			// Periodic SOX access review — flags dormant elevated-role users.
			// Admin | CFO (the reviewer privilege), matching the backend's
			// require_roles(ADMIN, CFO) on both /api/access-reviews routes.
			{ label: 'Access Review', labelKey: 'nav.accessReview', href: '/admin/access-review', roles: ['admin', 'cfo'] },
			// SOX records-management config — per-record-class retention windows.
			// Admin only (the backend GET/PUT /api/retention-policy 403s the rest).
			{ label: 'Retention Policy', labelKey: 'nav.retention', href: '/admin/retention', roles: ['admin'] },
			// GDPR/CCPA data-subject rights — DSAR export + right-to-erasure.
			// Admin only (the backend /api/privacy surface 403s the rest).
			{ label: 'Privacy & DSAR', labelKey: 'nav.privacy', href: '/admin/privacy', roles: ['admin'] },
		],
	},
	// Org configuration proper: who the tenant is, who works in it, and the
	// integration surfaces it exposes.
	{
		kind: 'group',
		label: 'Settings',
		labelKey: 'nav.group.settings',
		icon: 'settings',
		children: [
			{ label: 'Organization', labelKey: 'nav.organization', href: '/organization', roles: ['admin'] },
			// Users + Roles share the /admin route via ?tab=; they're surfaced as
			// peer section tabs (not a second tab row inside the page).
			// Users: `GET /api/admin/users` is `require_permission(user.manage)`
			// (defaults to admin-only, same as `roles` below — see
			// docs/authentication.md § Granular permissions), so a custom role
			// holding only `user.manage` sees this tab too via `permissions`.
			{
				label: 'Users',
				labelKey: 'nav.users',
				href: '/admin?tab=users',
				roles: ['admin'],
				permissions: [PERM_USER_MANAGE]
			},
			// Roles: role CRUD (defining what a role can grant) stays
			// admin-only on the backend — no `permissions` here on purpose.
			{ label: 'Roles', labelKey: 'nav.roles', href: '/admin?tab=roles', roles: ['admin'] },
			// Legal entities / subsidiaries — the `entity_id` scope target the
			// sidebar switcher selects. `GET /api/entities` is open to any authed
			// user, but this page is a mutation surface (POST / PATCH /
			// set-default are all `require_roles(ROLE_ADMIN)`), so admin only.
			{ label: 'Entities', labelKey: 'nav.entities', href: '/admin/entities', roles: ['admin'] },
			// Partner / reseller multi-tenant admin — manage branded child tenants.
			// Admin only (the backend /api/partner surface 403s the rest). A
			// standalone org sees an empty "not a partner" state.
			{ label: 'Partner Admin', labelKey: 'nav.partner', href: '/admin/partner', roles: ['admin'] },
			// Developer-API key management — admin only (the backend 403s the rest).
			{ label: 'API Keys', labelKey: 'nav.apiKeys', href: '/admin/api-keys', roles: ['admin'] },
			// Outbound-webhook subscriptions + delivery log / redelivery — admin
			// only (the backend /api/webhooks surface 403s the rest).
			{ label: 'Webhooks', labelKey: 'nav.webhooks', href: '/admin/webhooks', roles: ['admin'] },
			// Background-sweep health — per-sweep last run / outcome / failure
			// streak. Admin only, matching `require_roles(ROLE_ADMIN)` on
			// GET /api/health/sweeps. The public /api/health probe deliberately
			// reports none of this, so this row is the only way to see a dead or
			// stalled sweep (backend/docs/background-sweeps.md).
			{ label: 'Sweep Health', labelKey: 'nav.sweepHealth', href: '/admin/health', roles: ['admin'] },
		],
	},
];

/**
 * `roles` and `permissions` are OR'd: no gate at all (both omitted) is
 * visible to everyone; otherwise visible if EITHER the role check OR the
 * permission check passes. A custom role holding only the permission (no
 * listed role) still sees the entry.
 */
export function canSee(
	roles: string[] | undefined,
	has: RoleCheck,
	permissions?: string[],
	can?: PermissionCheck
): boolean {
	if (!roles && !permissions) return true;
	if (roles && has(...roles)) return true;
	if (permissions && can && permissions.some((p) => can(p))) return true;
	return false;
}

/** The children of a group the current role/permissions can see, in order. */
export function visibleChildren(
	group: NavGroup,
	has: RoleCheck,
	can?: PermissionCheck
): NavChild[] {
	return group.children.filter((c) => canSee(c.roles, has, c.permissions, can));
}

/** A link is visible by its own gate; a group is visible if any child is. */
export function isEntryVisible(entry: NavEntry, has: RoleCheck, can?: PermissionCheck): boolean {
	return entry.kind === 'link'
		? canSee(entry.roles, has, entry.permissions, can)
		: visibleChildren(entry, has, can).length > 0;
}

/** Where a group's sidebar row navigates to — its first accessible child. */
export function groupHref(group: NavGroup, has: RoleCheck, can?: PermissionCheck): string | null {
	return visibleChildren(group, has, can)[0]?.href ?? null;
}

/** Where one sidebar row points: a link's own href, a group's landing child. */
export function entryHref(entry: NavEntry, has: RoleCheck, can?: PermissionCheck): string | null {
	return entry.kind === 'link' ? entry.href : groupHref(entry, has, can);
}

/**
 * The sidebar a caller is offered, in nav order — one href per visible row.
 * `Sidebar.svelte` renders exactly this set (`isEntryVisible` + `entryHref`),
 * and `tests-e2e/auth/rbac.spec.ts` asserts the rendered DOM against it rather
 * than against a hand-typed copy of the answer. What each role is ALLOWED to
 * see is pinned once, as a literal table, in `nav.test.ts`.
 */
export function sidebarHrefs(has: RoleCheck, can?: PermissionCheck): string[] {
	return NAV.filter((e) => isEntryVisible(e, has, can)).flatMap((e) => {
		const href = entryHref(e, has, can);
		return href ? [href] : [];
	});
}

/**
 * `/foo` is active on `/foo` and `/foo/bar`; `/` only on exactly `/`. Any query
 * string / hash on `href` is ignored here — path comparison only. Query-aware
 * active state (e.g. `/admin?tab=roles`) is handled by {@link sectionTabActive}.
 */
export function pathMatches(href: string, pathname: string): boolean {
	const path = href.split(/[?#]/)[0];
	if (path === '/') return pathname === '/';
	return pathname === path || pathname.startsWith(path + '/');
}

/**
 * Whether a section tab is the active one for the current URL. Handles tabs that
 * differ only by query (Users/Roles both live at `/admin`):
 *  - exact match on every query param the tab specifies → active;
 *  - if the URL carries none of those params, the FIRST sibling sharing that
 *    path is the default and wins (so bare `/admin` lights up Users).
 */
export function sectionTabActive(child: NavChild, siblings: NavChild[], url: URL): boolean {
	const [path, qs] = child.href.split('?');
	if (!qs) {
		// A plain-path tab is active on its path or a sub-path — unless a
		// more-specific sibling tab claims that same URL (so a parent tab
		// doesn't stay lit on a deeper sibling's page).
		if (!pathMatches(path, url.pathname)) return false;
		return !siblings.some(
			(s) => s !== child && s.href.split('?')[0].startsWith(path + '/') && pathMatches(s.href, url.pathname)
		);
	}

	// A query-bearing tab (e.g. `/admin?tab=users`) shares its path with a
	// sibling, so it matches only the EXACT path + query — never a sub-path.
	// Prefix-matching here lit up `/admin?tab=users` on `/admin/api-keys` (which
	// is its own, deeper tab).
	if (url.pathname !== path) return false;

	const want = new URLSearchParams(qs);
	const keys = [...want.keys()];
	if (keys.every((k) => url.searchParams.get(k) === want.get(k))) return true;

	// URL specifies none of this tab's params → the first sibling on this path
	// is the default selection.
	if (keys.every((k) => url.searchParams.get(k) === null)) {
		const firstOnPath = siblings.find((s) => s.href.split('?')[0] === path);
		return firstOnPath?.href === child.href;
	}
	return false;
}

/** True when the current path belongs to this entry (link or any group child). */
export function isEntryActive(entry: NavEntry, pathname: string): boolean {
	if (entry.kind === 'group') {
		return entry.children.some((c) => pathMatches(c.href, pathname));
	}
	// A link matches its path or any sub-path — but when two top-level links
	// nest (`/vendors` and `/vendors/screening`), only the most specific (longest
	// matching href) should light up, so `/vendors` doesn't stay active while on
	// `/vendors/screening`.
	if (!pathMatches(entry.href, pathname)) return false;
	const here = entry.href.split(/[?#]/)[0];
	return !NAV.some(
		(other) =>
			other !== entry &&
			other.kind === 'link' &&
			other.href.split(/[?#]/)[0].length > here.length &&
			pathMatches(other.href, pathname)
	);
}

/**
 * The group that owns `pathname`, or null if the path is a top-level link (or
 * unknown). Structural only — does NOT apply RBAC, so the caller still filters
 * the returned group's children by role before rendering the sub-tab bar.
 */
export function groupForPath(pathname: string): NavGroup | null {
	for (const e of NAV) {
		if (e.kind === 'group' && e.children.some((c) => pathMatches(c.href, pathname))) return e;
	}
	return null;
}
