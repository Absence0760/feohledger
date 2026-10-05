import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * /invoices — "My Approvals" + the "Assigned to" filter (issue #328,
 * approver/High).
 *
 * Regression: an approver had no way to narrow the queue to what was
 * assigned to them — no assignee column, no filter, no dedicated view — so
 * a team of any size had to scan the whole queue by eye. The fix adds
 * `assigned_to_id` as a real server-side filter (`GET /api/invoices` +
 * `GET /api/invoices/ids`, `backend/app/api/invoices.py`), an "Assigned to"
 * dropdown, a "My Approvals" quick toggle that sets the filter to the
 * caller's own id, and an Assigned-To table column. `assigned_to_id`
 * round-trips through the URL like the page's other URL-backed filters
 * (`?id=` deep-link) — see `syncUrl()` in `routes/invoices/+page.svelte`.
 *
 * Seeds two invoices assigned to the signed-in manager and two assigned to
 * an unrelated id, all `ready_for_review` (so a real assignee is
 * meaningful). No cross-database FK exists on `assigned_to_id` (it's a
 * tenant-DB column; `users` lives in the control-plane DB), so the "other"
 * assignment is a synthetic id/name — nothing needs to resolve it.
 */

const MARKER = `E2E-ASSIGNEE-${Date.now()}`;
const MINE_1 = `${MARKER}-MINE-1`;
const MINE_2 = `${MARKER}-MINE-2`;
const OTHER_1 = `${MARKER}-OTHER-1`;
const OTHER_2 = `${MARKER}-OTHER-2`;
const OTHER_NAME = 'E2E Other Reviewer';

function seedInvoice(number: string, assignedToId: string, assignedToName: string): void {
	tenantPsql(
		`INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, vendor_name, amount, currency, status, assigned_to_id, assigned_to, created_at, updated_at)
		 VALUES (gen_random_uuid(), gen_random_uuid(), (SELECT organization_id FROM invoices LIMIT 1),
		         '${number}', 'E2E Assignee Vendor', 250.00, 'USD', 'ready_for_review',
		         '${assignedToId}', '${assignedToName}', now(), now())`
	);
}

test.describe('/invoices "My Approvals" + assigned-to filter', () => {
	let myId = '';
	let myName = '';

	test.beforeAll(async ({ browser, tenantManager }) => {
		// Resolve the manager's real id/name once — every test signs in as the
		// same worker-scoped `tenantManager` account.
		const page = await browser.newPage();
		await signInAndWait(page, tenantManager);
		const res = await page.request.get(`${API_BASE}/api/auth/me`, {
			headers: await authedTenantHeaders(page)
		});
		expect(res.ok()).toBeTruthy();
		const me = (await res.json()) as { id: string; full_name: string };
		myId = me.id;
		myName = me.full_name;
		await page.close();
	});

	test.beforeEach(() => {
		seedInvoice(MINE_1, myId, myName);
		seedInvoice(MINE_2, myId, myName);
		seedInvoice(OTHER_1, '00000000-0000-0000-0000-0000000000aa', OTHER_NAME);
		seedInvoice(OTHER_2, '00000000-0000-0000-0000-0000000000bb', OTHER_NAME);
	});

	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${MARKER}%'`));

	test('My Approvals narrows the table to the current user\'s assigned invoices', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		// Narrow the visible set to just our four seeded rows first, so the
		// assertions below aren't defeated by unrelated pre-existing rows also
		// assigned to this manager from other specs.
		const searched = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes(`search=${encodeURIComponent(MARKER)}`)
		);
		await page.getByPlaceholder('Search invoices...').fill(MARKER);
		await searched;
		await expect(page.locator('table tbody tr')).toHaveCount(4);

		const toggle = page.locator('[data-testid="my-approvals-toggle"]');
		await expect(toggle).toBeVisible();
		await expect(toggle).not.toHaveClass(/active/);

		const filtered = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`assigned_to_id=${myId}`)
		);
		await toggle.click();
		await filtered;

		await expect(toggle).toHaveClass(/active/);
		await expect(page.locator('table tbody tr')).toHaveCount(2);
		await expect(page.locator('table tbody tr', { hasText: MINE_1 })).toBeVisible();
		await expect(page.locator('table tbody tr', { hasText: MINE_2 })).toBeVisible();
		await expect(page.locator('table tbody tr', { hasText: OTHER_1 })).toHaveCount(0);
		await expect(page.locator('table tbody tr', { hasText: OTHER_2 })).toHaveCount(0);

		// The Assigned To column renders the display name the backend already
		// carries on the row — no separate lookup.
		const mineRow = page.locator('table tbody tr', { hasText: MINE_1 });
		await expect(mineRow.locator('td.assignee')).toHaveText(myName);

		// Toggling off restores the wider (still search-narrowed) set.
		const cleared = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && !r.url().includes('assigned_to_id=')
		);
		await toggle.click();
		await cleared;
		await expect(toggle).not.toHaveClass(/active/);
		await expect(page.locator('table tbody tr')).toHaveCount(4);
	});

	test('the assigned-to filter round-trips through the URL on reload', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const searched = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes(`search=${encodeURIComponent(MARKER)}`)
		);
		await page.getByPlaceholder('Search invoices...').fill(MARKER);
		await searched;

		const filtered = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`assigned_to_id=${myId}`)
		);
		await page.locator('[data-testid="my-approvals-toggle"]').click();
		await filtered;

		await expect(page).toHaveURL(new RegExp(`assigned_to_id=${myId}`));

		// Reload cold — the filter must re-apply from the URL, not just persist
		// in memory (`assignedToId` is seeded from `$page.url.searchParams` on
		// mount).
		const reloaded = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`assigned_to_id=${myId}`)
		);
		await page.reload();
		await reloaded;

		await expect(page.locator('[data-testid="my-approvals-toggle"]')).toHaveClass(/active/);
		await expect(page.locator('[data-testid="assigned-to-filter"]')).toHaveValue(myId);
	});
});

/**
 * An assignee filter that matches nothing is a filtered-to-zero view, not an
 * empty tenant. `noActiveFilters` on the page used to ignore `assigned_to_id`,
 * so "My Approvals" with nothing assigned (or a reviewer with an empty queue)
 * replaced the whole table with the fresh-tenant onboarding — "upload your
 * first invoice" over a tenant full of them.
 */
test.describe('/invoices assigned-to filter matching nothing', () => {
	test('shows the no-matches table, not the first-run onboarding', async ({ page }) => {
		// No invoice is ever assigned to this id: there is no `users` row for it
		// (assigned_to_id is a tenant column with no cross-database FK).
		const nobody = '00000000-0000-0000-0000-0000000000cc';
		const filtered = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`assigned_to_id=${nobody}`) &&
				r.request().method() === 'GET'
		);
		await page.goto(`/invoices?assigned_to_id=${nobody}`);
		const res = await filtered;
		expect(((await res.json()) as { total: number }).total).toBe(0);

		await expect(page.getByText('No invoices match your filters.')).toBeVisible();
		await expect(page.locator('[data-testid="invoices-empty-state"]')).toHaveCount(0);
	});
});
