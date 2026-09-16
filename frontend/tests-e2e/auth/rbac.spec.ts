import { expect, sectionTabHrefs, signInAndWait, test } from '../fixtures/helpers';
import type { Page } from '@playwright/test';

/**
 * RBAC — sidebar + section-tab visibility per role.
 *
 * The primary nav (`frontend/src/lib/nav.ts`) keeps high-traffic destinations as
 * top-level links and folds the rest into groups (Procurement / Billing /
 * Insights / Automation / Governance / Settings). The sidebar shows ONE row per
 * group, pointing at the
 * first child the role can see; each grouped page then renders the group's
 * children as a section sub-tab bar — again RBAC-filtered. Both layers read the
 * same per-route `roles` gate, so each test asserts both:
 *   1. the sidebar rows a role sees (direct links + group landings), and
 *   2. the section tabs a role sees inside a group.
 *
 * Login budget: the backend caps logins at 10/60s per IP and every e2e login
 * shares localhost, so we sign in ONCE per non-admin role (asserting everything
 * for that role in the single session) and cover admin on the cached default
 * storage-state — no extra fresh login. Admin route reachability is also in
 * `smoke/nav.spec.ts`.
 *
 * Per-route gates (mirroring the backend read-RBAC):
 *   Direct: Dashboard(all) · Invoices(all) · Payments(adm/mgr/cfo) ·
 *           Vendors(adm/mgr/cfo) · Screening(adm/mgr/cfo) · Exceptions(adm/mgr)
 *   Procurement: Budgets(adm/mgr/cfo);
 *                PurchaseOrders·GoodsReceipts·Requisitions·Intake·Catalogs(all)
 *   Billing: Contracts·Expenses·VendorStatements(all); CreditMemos·Discounts(adm/mgr/cfo)
 *   Insights: AIAssistant(all); CashFlow(adm/cfo); 1099(adm/mgr/cfo)
 *   Settings: Organization·Users·Roles·Entities·Partner·APIKeys·Webhooks·
 *             SweepHealth(admin) — every child is admin-only, so a CFO sees NO
 *             Settings row at all since the Governance/Automation split
 *   Governance: Retention·Privacy(admin); AuditTrail·AccessReview(adm/cfo)
 *   Automation: Workflows(admin); Experiments·Adaptive(adm/mgr/cfo)
 */

async function sidebarHrefs(page: Page): Promise<string[]> {
	const links = page.locator('aside.sidebar a.nav-item');
	return (
		await links.evaluateAll((els) =>
			els.map((e) => (e as HTMLAnchorElement).getAttribute('href') ?? '')
		)
	).sort();
}

/**
 * Every section tab a role is offered at `route` — the row plus the More menu,
 * via the shared helper. Reading the row alone would answer "which tabs fit at
 * 1280px" rather than "which tabs does this role have" (`decisions §174`).
 */
async function sectionTabHrefsAt(page: Page, route: string): Promise<string[]> {
	await page.goto(route);
	await expect(page.locator('aside.sidebar')).toBeVisible();
	return (await sectionTabHrefs(page)).sort();
}

test.describe('RBAC — non-admin roles (one fresh sign-in each)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('clerk: Dashboard/Invoices + folded group landings; group tabs RBAC-filtered', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		// A group row links to the FIRST child the role can see.
		expect(await sidebarHrefs(page)).toEqual(
			['/', '/invoices', '/vendors/screening', '/purchase-orders', '/contracts', '/assistant'].sort()
		);
		// Procurement tabs: every child whose reads admit a clerk — which is all
		// of them but Budgets. Purchase Orders and Goods Receipts joined this set
		// when the group stopped carrying one blanket gate: both routers read via
		// `get_current_user`, as does the Inspections tab's `GET /api/inspections`,
		// and each page gates its own mutations on `auth.isManager`. Budgets stays
		// out — every `/api/budgets` read is `require_roles(ADMIN, AP_MANAGER, CFO)`,
		// so a clerk's first paint would 403. Chart of Accounts is in for the same
		// reason the other five are: `GET /api/gl-accounts` is `get_current_user`,
		// and a clerk coding an invoice already reads that exact list through the
		// line-item GL picker — the page's two writes gate themselves on
		// `auth.isManager` (`docs/decisions.md` §161). Purchase Orders being first
		// in nav order is also why the group's sidebar row now lands there, not on
		// Requisitions. `frontend/src/lib/nav.test.ts` pins the same set.
		expect(await sectionTabHrefsAt(page, '/purchase-orders')).toEqual(
			[
				'/purchase-orders',
				'/goods-receipts',
				'/requisitions',
				'/intake',
				'/catalogs',
				'/gl-accounts'
			].sort()
		);
		// Billing tabs: every child whose LIST endpoint admits a clerk. Credit
		// Memos and Recurring joined this set when the nav stopped hiding pages
		// the API grants — `GET /api/credit-memos` and `api/recurring.py`'s
		// `_READ_ROLES` both include `ap_clerk`, and each page gates its own
		// mutations. Positive Pay and Billing stay out (admin/manager/cfo and
		// admin/cfo respectively). Bank Reconciliation is in for the same reason
		// as Credit Memos and Recurring — `api/bank_reconciliation.py::_READ_ROLES`
		// admits a clerk, and the page gates its own writes on `auth.isManager`.
		expect(await sectionTabHrefsAt(page, '/contracts')).toEqual(
			[
				'/contracts',
				'/expenses',
				'/credit-memos',
				'/discounts',
				'/recurring',
				'/vendor-statements',
				'/bank-reconciliation'
			].sort()
		);
	});

	test('manager: + Payments/Vendors/Exceptions; full Procurement tabs; Settings = Adaptive + Experiments', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		// Experiments and Adaptive Workflows are both manager-readable and both
		// live in Settings, so a manager gets the Settings group landing.
		// `/adaptive` matches its backend gate: `api/adaptive._READ_ROLES` is
		// admin/ap_manager/cfo, so a manager can read every panel on it (the
		// write surfaces gate themselves separately, and threshold-apply is
		// admin-only).
		expect(await sidebarHrefs(page)).toEqual(
			['/', '/invoices', '/payments', '/vendors', '/vendors/screening', '/vendors/change-requests', '/exceptions', '/purchase-orders', '/contracts', '/assistant', '/experiments'].sort()
		);
		expect(await sectionTabHrefsAt(page, '/purchase-orders')).toEqual(
			[
				'/purchase-orders',
				'/goods-receipts',
				'/requisitions',
				'/intake',
				'/catalogs',
				'/gl-accounts',
				'/budgets'
			].sort()
		);
		// Two Settings tabs now, so the section bar renders instead of being
		// suppressed as it was when Experiments stood alone. The group landing
		// is still /experiments — the first child a manager can see in nav order.
		expect(await sectionTabHrefsAt(page, '/experiments')).toEqual(
			['/adaptive', '/experiments'].sort()
		);
	});

	test('cfo: no Settings row at all; Governance + Automation landings', async ({
		page,
		tenantCfo
	}) => {
		await signInAndWait(page, tenantCfo);
		// The Governance/Automation split changed what a CFO sees, and correctly:
		// every remaining Settings child is admin-only, so the group is hidden
		// rather than rendering a "Settings" row whose only destination was the
		// audit trail. The two surfaces a CFO actually holds are now named.
		expect(await sidebarHrefs(page)).toEqual(
			['/', '/invoices', '/payments', '/vendors', '/vendors/screening', '/purchase-orders', '/contracts', '/assistant', '/audit', '/experiments'].sort()
		);
		// Governance: Audit Trail + Access Review are (admin|cfo); Retention and
		// Privacy are admin-only and stay out.
		expect(await sectionTabHrefsAt(page, '/audit')).toEqual(
			['/audit', '/admin/access-review'].sort()
		);
		// Automation: Experiments + Adaptive are (admin|mgr|cfo); Workflows is
		// admin-only.
		expect(await sectionTabHrefsAt(page, '/experiments')).toEqual(
			['/experiments', '/adaptive'].sort()
		);
	});
});

test.describe('RBAC — admin (cached session, no extra login)', () => {
	test('full sidebar set + the three admin group bars; no bar on a top-level route', async ({
		page
	}) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		expect(await sidebarHrefs(page)).toEqual(
			['/', '/invoices', '/payments', '/vendors', '/vendors/screening', '/vendors/change-requests', '/exceptions', '/purchase-orders', '/contracts', '/assistant', '/workflows', '/audit', '/organization'].sort()
		);
		// The 15 tabs that made this bar overflow the page are now three groups.
		expect(await sectionTabHrefsAt(page, '/organization')).toEqual(
			[
				'/organization',
				'/admin?tab=users',
				'/admin?tab=roles',
				'/admin/entities',
				'/admin/partner',
				'/admin/api-keys',
				'/admin/webhooks',
				'/admin/health'
			].sort()
		);
		expect(await sectionTabHrefsAt(page, '/audit')).toEqual(
			['/audit', '/admin/access-review', '/admin/retention', '/admin/privacy'].sort()
		);
		expect(await sectionTabHrefsAt(page, '/workflows')).toEqual(
			['/workflows', '/experiments', '/adaptive'].sort()
		);
		// Direct (non-grouped) routes have no section bar.
		await page.goto('/invoices');
		await expect(page.locator('.section-tabs')).toHaveCount(0);
	});
});
