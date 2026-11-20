import {
	API_BASE,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from './fixtures/helpers';

/**
 * /discounts — Dynamic Discounting & Early-Payment Optimization dashboard.
 *
 * Reads GET /api/discounts/dashboard + /offers, POSTs /optimize and the
 * accept/decline actions. The default per-worker storage state signs the
 * worker's admin in, and admin is one of the allowed roles (admin /
 * ap_manager / cfo), so the page loads directly without a redirect.
 *
 * NOTE: the Phase-C `/api/discounts` router isn't wired yet, so these calls
 * may currently 404. The page is built to degrade gracefully (zeroed KPIs +
 * empty offers table), and this spec asserts the *structure* + the
 * empty-state fallback rather than seeded tallies. When the backend + a seed
 * land, the offer-row + accept-flow assertions below activate automatically
 * (they branch on whether any row rendered).
 */

test.describe('/discounts (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/discounts');
	});

	test('renders the discounts surface — header, KPIs, optimizer, filters, table', async ({
		page
	}) => {
		await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();

		// KPI row: Captured / Missed / Capture rate / Projected savings / Open offers.
		await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.kpi')).toHaveCount(5);
		await expect(page.locator('.kpi-label', { hasText: 'Capture rate' })).toBeVisible();

		// Optimizer panel with its cash-budget input + Optimize button.
		await expect(page.getByRole('heading', { name: 'Early-payment optimizer' })).toBeVisible();
		await expect(page.getByLabel('Cash budget')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Optimize' })).toBeVisible();

		// Status filter chips.
		await expect(page.locator('.filter-chip', { hasText: 'All' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Captured' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Missed' })).toBeVisible();

		// The offers data table renders (rows or the centred empty state).
		await expect(page.locator('.grid-container table')).toBeVisible();
	});

	test('switching the status filter re-requests the offers list', async ({ page }) => {
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/discounts/offers') && r.url().includes('status=captured')
		);
		await page.locator('.filter-chip', { hasText: 'Captured' }).click();
		const resp = await respPromise;
		// The backend may not be wired yet (404 acceptable); we only assert the
		// request was issued with the right status param.
		expect(resp.request().url()).toContain('status=captured');
	});

	test('the status filter round-trips through the URL', async ({ page }) => {
		// The chip writes `?status=`, and a reload restores both the chip and
		// the filtered request — the same contract as every sibling list.
		await page.locator('.filter-chip', { hasText: 'Captured' }).click();
		await expect(page).toHaveURL(/[?&]status=captured\b/);

		const reloaded = page.waitForResponse(
			(r) => r.url().includes('/api/discounts/offers') && r.url().includes('status=captured')
		);
		await page.reload();
		await reloaded;
		await expect(page.locator('.filter-chip', { hasText: 'Captured' })).toHaveAttribute(
			'aria-pressed',
			'true'
		);

		await page.locator('.filter-chip', { hasText: 'All' }).click();
		await expect(page).not.toHaveURL(/status=/);
	});

	test('clicking Optimize posts to /optimize', async ({ page }) => {
		const respPromise = page.waitForResponse((r) =>
			r.url().includes('/api/discounts/optimize')
		);
		await page.getByLabel('Cash budget').fill('100000');
		await page.getByRole('button', { name: 'Optimize' }).click();
		const resp = await respPromise;
		expect(resp.request().method()).toBe('POST');
	});

	test('offers table: either rows render with an Accept action, or the empty state shows', async ({
		page
	}) => {
		await expect(page.locator('.grid-container table')).toBeVisible({ timeout: 10_000 });

		const rows = page.locator('.grid-container tbody tr:not(:has(td.empty))');
		const rowCount = await rows.count();

		if (rowCount === 0) {
			// No seeded offers (or router not wired) → friendly empty state.
			await expect(page.locator('td.empty')).toBeVisible();
			await expect(page.locator('td.empty')).toContainText(/No discount offers|Loading…/);
			return;
		}

		// Base-amount cell carries a currency symbol, not a bare number.
		await expect(rows.first().locator('td.right .money').first()).toContainText(/[^\d.,]/);

		// If an `offered` row exists, its Accept action opens the tier modal.
		const accept = page.getByRole('button', { name: /^Accept discount for / }).first();
		if (await accept.count()) {
			await accept.click();
			await expect(page.getByRole('dialog', { name: 'Accept discount offer' })).toBeVisible();
		}
	});
});

test.describe('/discounts (clerk — read-only)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	/**
	 * `backend/app/api/discounts.py::_READ_ROLES` includes `ROLE_AP_CLERK`, so a
	 * clerk may read the dashboard, the offer list, per-invoice ROI and the
	 * optimizer (`POST /optimize` is read-gated — it computes and mutates
	 * nothing). This page used to redirect them anyway, on a comment claiming
	 * "the backend 403s everyone else": a dead end on a surface they are
	 * entitled to. What a clerk must NOT get is the accept / decline controls —
	 * those are `_ACCEPT_ROLES` (admin / ap_manager / cfo).
	 */
	test('ap_clerk reads the dashboard; no accept/decline controls, no 403s', async ({
		page,
		tenantAdmin,
		tenantClerk,
		tenantSlug
	}) => {
		// Seed one OFFERED offer as the admin so the "no Accept button" assertion
		// is not vacuous — for a manager every offered row renders one.
		const login = await page.request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': tenantSlug },
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		expect(login.status()).toBe(200);
		const adminHeaders = {
			Authorization: `Bearer ${((await login.json()) as { access_token: string }).access_token}`,
			'X-Tenant-Slug': tenantSlug
		};
		const vendors = await page.request.get(`${API_BASE}/api/vendors`, { headers: adminHeaders });
		const vendor = ((await vendors.json()) as { items: { id: string; name: string }[] }).items[0];
		const invResp = await page.request.post(`${API_BASE}/api/invoices`, {
			headers: adminHeaders,
			data: {
				vendor: vendor.name,
				invoice_number: `DISC-RBAC-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
				amount: 1000,
				currency: 'USD'
			}
		});
		const invoiceId = ((await invResp.json()) as { id: string }).id;
		tenantPsql(
			`UPDATE invoices SET status='approved', vendor_id='${vendor.id}' WHERE id='${invoiceId}'`
		);
		const offerResp = await page.request.post(`${API_BASE}/api/discounts/offers`, {
			headers: adminHeaders,
			data: {
				scope: 'invoice',
				invoice_id: invoiceId,
				tiers: [{ days: 10, percent: '2.00' }]
			}
		});
		expect(offerResp.status(), await offerResp.text()).toBe(201);
		const offerId = ((await offerResp.json()) as { id: string }).id;

		// Any 403 on a read the page issues is the bug in the other direction.
		const forbidden: string[] = [];
		page.on('response', (r) => {
			if (r.url().includes('/api/discounts/') && r.status() === 403) {
				forbidden.push(`${r.request().method()} ${r.url()}`);
			}
		});

		try {
			// The shared helper, not a hand-rolled copy of it: this block was a
			// verbatim duplicate of signInAndWait, down to the same
			// `waitForLoadState('networkidle')` after goto('/login'). It also
			// misses what the helper adds — the cookie-consent init script, whose
			// banner is fixed to the bottom of the viewport and intercepts clicks
			// on exactly the filter chips and row controls this test then drives.
			await signInAndWait(page, tenantClerk);

			await page.goto('/discounts');
			// The page stays put — the clerk is not bounced to the tenant root.
			await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();
			await expect(page).toHaveURL(/\/discounts$/);
			await expect(page.locator('.kpi')).toHaveCount(5);
			await expect(page.locator('.grid-container table')).toBeVisible();

			// The offered offer we seeded renders (newest first, `created_at desc`).
			await page.locator('.filter-chip', { hasText: 'Offered' }).click();
			const rows = page.locator('.grid-container tbody tr:not(:has(td.empty))');
			await expect(rows.first()).toBeVisible();

			// …but with no decide controls on any row.
			await expect(page.getByRole('button', { name: /^Accept discount for / })).toHaveCount(0);
			await expect(page.getByRole('button', { name: /^Decline for / })).toHaveCount(0);

			// The optimizer is a read for RBAC purposes, so a clerk may run it.
			const optimize = page.waitForResponse((r) => r.url().includes('/api/discounts/optimize'));
			await page.getByRole('button', { name: 'Optimize' }).click();
			expect((await optimize).status()).toBe(200);

			expect(forbidden, 'a read the clerk page issues was refused').toEqual([]);
		} finally {
			tenantPsql(`DELETE FROM discount_offers WHERE id='${offerId}'`);
			tenantPsql(
				`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${invoiceId}')`
			);
			tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${invoiceId}'`);
			tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${invoiceId}'`);
			deleteInvoicesWhere(`id='${invoiceId}'`);
		}
	});
});
