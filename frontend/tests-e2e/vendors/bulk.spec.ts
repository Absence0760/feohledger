import { deleteVendorsWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /vendors bulk operations (issue #328, power-user/Medium): /vendors and
 * /contracts shipped zero bulk actions despite being primary volume list
 * pages. This covers the UI wiring end-to-end — checkbox selection, the
 * shared BulkBar appearing, and each bulk action actually landing —
 * complementing the exhaustive skip-and-report / RBAC coverage in
 * backend/tests/test_vendor_bulk_ops.py.
 */

const MARKER = 'BULKVND-';

function seedVendors(n: number, status = 'unverified'): void {
	tenantPsql(
		`INSERT INTO vendors (id, organization_id, name, status, source, accepts_virtual_cards, is_1099_eligible, kyc_status, screening_status, payments_blocked, risk_level, created_at, updated_at)
		 SELECT gen_random_uuid(), (SELECT organization_id FROM vendors LIMIT 1),
		        '${MARKER}' || lpad(g::text, 3, '0'), '${status}', 'manual', false, false, 'not_required', 'unscreened', false, 'unknown', now(), now()
		 FROM generate_series(1, ${n}) g`
	);
}

function purge(): void {
	deleteVendorsWhere(`name LIKE '${MARKER}%'`);
}

test.describe('/vendors bulk operations (acme admin)', () => {
	test.afterEach(() => purge());

	test('bulk verify moves selected unverified vendors to active', async ({ page }) => {
		seedVendors(3, 'unverified');

		await page.goto('/vendors');
		await page.getByPlaceholder('Search vendors...').fill(MARKER);
		await page.waitForResponse(
			(r) => r.url().includes('/api/vendors?') && r.url().includes('search=')
		);

		const rows = page.locator('table tbody tr');
		await expect(rows).toHaveCount(3);

		// Select all three via the header checkbox.
		await page.locator('th.checkbox-col input[type="checkbox"]').check();
		await expect(page.locator('.bulk-count')).toContainText('3 selected');

		const verifyResponse = page.waitForResponse(
			(r) => r.url().includes('/api/vendors/bulk/status') && r.request().method() === 'POST'
		);
		await page.getByRole('toolbar', { name: 'Bulk actions' }).getByRole('button', { name: 'Verify', exact: true }).click();
		const resp = await verifyResponse;
		expect(resp.status()).toBe(200);
		const body = (await resp.json()) as { updated: number; skipped: unknown[] };
		expect(body.updated).toBe(3);
		expect(body.skipped).toHaveLength(0);

		// The bulk bar clears and the list (re-fetched) shows the new status.
		await expect(page.locator('.bulk-count')).toHaveCount(0);
		await expect(page.locator('table tbody tr').first().locator('.status-badge')).toContainText(
			'Active'
		);
	});

	test('a stale id is skipped without failing the rest of the batch', async ({ page }) => {
		seedVendors(2, 'unverified');

		await page.goto('/vendors');
		await page.getByPlaceholder('Search vendors...').fill(MARKER);
		await page.waitForResponse(
			(r) => r.url().includes('/api/vendors?') && r.url().includes('search=')
		);
		await expect(page.locator('table tbody tr')).toHaveCount(2);

		await page.locator('th.checkbox-col input[type="checkbox"]').check();

		// One of the two is deleted server-side between selection and the bulk
		// call — the batch must still land the other, not abort entirely.
		const firstId = tenantPsql(
			`SELECT id FROM vendors WHERE name LIKE '${MARKER}%' ORDER BY name LIMIT 1`
		).trim();
		deleteVendorsWhere(`id = '${firstId}'`);

		const verifyResponse = page.waitForResponse(
			(r) => r.url().includes('/api/vendors/bulk/status') && r.request().method() === 'POST'
		);
		await page.getByRole('toolbar', { name: 'Bulk actions' }).getByRole('button', { name: 'Verify', exact: true }).click();
		const body = (await (await verifyResponse).json()) as {
			updated: number;
			skipped: { id: string; reason: string }[];
		};
		expect(body.updated).toBe(1);
		expect(body.skipped).toHaveLength(1);
		expect(body.skipped[0].id).toBe(firstId);
	});

	test('bulk export downloads a CSV of the selected vendors', async ({ page }) => {
		seedVendors(2, 'active');

		await page.goto('/vendors');
		await page.getByPlaceholder('Search vendors...').fill(MARKER);
		await page.waitForResponse(
			(r) => r.url().includes('/api/vendors?') && r.url().includes('search=')
		);
		await expect(page.locator('table tbody tr')).toHaveCount(2);

		await page.locator('th.checkbox-col input[type="checkbox"]').check();
		await expect(page.locator('.bulk-count')).toContainText('2 selected');

		const [download] = await Promise.all([
			page.waitForEvent('download'),
			page.getByRole('button', { name: 'CSV', exact: true }).click()
		]);
		expect(download.suggestedFilename()).toBe('vendors-export.csv');
	});

	test('select all N matching resolves past the loaded page', async ({ page }) => {
		seedVendors(25, 'active');

		await page.goto('/vendors');
		await page.getByPlaceholder('Search vendors...').fill(MARKER);
		await page.waitForResponse(
			(r) => r.url().includes('/api/vendors?') && r.url().includes('search=')
		);

		// Only one page's worth loads, even though 25 rows match the filter.
		await expect(page.locator('table tbody tr')).toHaveCount(20);

		await page.locator('th.checkbox-col input[type="checkbox"]').check();
		await expect(page.locator('.bulk-count')).toContainText('20 selected');

		const selectAllBtn = page.getByRole('button', { name: /Select all \d+ matching/ });
		await expect(selectAllBtn).toBeVisible();
		await expect(selectAllBtn).toContainText('25');

		const idsResponse = page.waitForResponse(
			(r) => r.url().includes('/api/vendors/ids') && r.request().method() === 'GET'
		);
		await selectAllBtn.click();
		await idsResponse;

		await expect(page.locator('.bulk-count')).toContainText('25 selected');
	});

	// Bulk Reject stops payments and revokes live cards for every selected
	// vendor, so it is the armed two-click the row Reject already is: the first
	// click only arms, and nothing is posted until the confirm.
	test('bulk reject arms on the first click and commits on the confirm', async ({ page }) => {
		seedVendors(2, 'unverified');

		await page.goto('/vendors');
		await page.getByPlaceholder('Search vendors...').fill(MARKER);
		await page.waitForResponse(
			(r) => r.url().includes('/api/vendors?') && r.url().includes('search=')
		);
		await expect(page.locator('table tbody tr')).toHaveCount(2);
		await page.locator('th.checkbox-col input[type="checkbox"]').check();

		let posted = 0;
		page.on('request', (r) => {
			if (r.url().includes('/api/vendors/bulk/status') && r.method() === 'POST') posted += 1;
		});

		const toolbar = page.getByRole('toolbar', { name: 'Bulk actions' });
		await toolbar.getByRole('button', { name: 'Reject', exact: true }).click();
		await expect(toolbar.getByRole('button', { name: 'Confirm reject' })).toBeVisible();
		expect(posted).toBe(0);

		const rejectResponse = page.waitForResponse(
			(r) => r.url().includes('/api/vendors/bulk/status') && r.request().method() === 'POST'
		);
		await toolbar.getByRole('button', { name: 'Confirm reject' }).click();
		const resp = await rejectResponse;
		expect(resp.status()).toBe(200);
		expect(((await resp.json()) as { updated: number }).updated).toBe(2);
		expect(posted).toBe(1);
	});
});
