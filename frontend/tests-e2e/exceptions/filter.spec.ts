import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /exceptions status-chip filtering. Seed has 3 open + 1 resolved
 * exception per tenant. Read-only — no mutations, no cleanup.
 *
 * The row badges assert the LABEL exactly (`Open` / `Resolved`), not a
 * case-insensitive regex: the badge and the chip above it now read one key set
 * (`types/exception.ts::EXCEPTION_STATUS_LABEL_KEYS`), and a `/open/i` would
 * have gone on passing against the raw wire value this replaced — which is the
 * regression the pairing exists to catch.
 */

test.describe('/exceptions status filter', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/exceptions');
	});

	test('Open chip is the default and shows only open rows', async ({ page }) => {
		await expect(page.locator('.filter-chip', { hasText: /^Open/ })).toHaveClass(/active/);

		const rows = page.locator('table tbody tr');
		expect(await rows.count()).toBeGreaterThan(0);
		const total = await rows.count();
		for (let i = 0; i < total; i++) {
			await expect(rows.nth(i).locator('.status-badge')).toHaveText('Open');
		}
	});

	test('Resolved chip narrows to resolved rows', async ({ page }) => {
		const filtered = page.waitForResponse(
			(r) => r.url().includes('/api/exceptions') && r.url().includes('status=resolved')
		);
		await page.locator('.filter-chip', { hasText: /^Resolved/ }).click();
		await filtered;

		await expect(page.locator('.filter-chip', { hasText: /^Resolved/ })).toHaveClass(/active/);
		const rows = page.locator('table tbody tr');
		const total = await rows.count();
		expect(total).toBeGreaterThan(0);
		for (let i = 0; i < total; i++) {
			await expect(rows.nth(i).locator('.status-badge')).toHaveText('Resolved');
		}
	});

	test('All chip clears the status filter and shows every row', async ({ page }) => {
		const all = await page.request.get(`${API_BASE}/api/exceptions`, {
			headers: await authedTenantHeaders(page)
		});
		const allBody = (await all.json()) as { items: unknown[] };
		const totalApi = allBody.items.length;

		// The frontend's "All" chip omits the status param entirely.
		const filtered = page.waitForResponse(
			(r) => r.url().includes('/api/exceptions?') && !r.url().includes('status=')
		);
		// `/^All\s+\d+/`, the convention `frontend/docs/ui-patterns.md` names for
		// the status row's All chip: the severity row below it is FilterChips too,
		// and its "All severities" chip also starts with "All".
		await page.locator('.filter-chip', { hasText: /^All\s+\d+/ }).click();
		await filtered;

		await expect(page.locator('.filter-chip', { hasText: /^All\s+\d+/ })).toHaveClass(/active/);
		await expect(page.locator('table tbody tr')).toHaveCount(totalApi);
	});

	test('Open count chip displays the seeded open total', async ({ page }) => {
		const summary = await page.request.get(`${API_BASE}/api/exceptions/summary`, {
			headers: await authedTenantHeaders(page)
		});
		const body = (await summary.json()) as { open: number };

		await expect(
			page.locator('.filter-chip', { hasText: /^Open/ }).locator('.count')
		).toHaveText(String(body.open));
	});
});
