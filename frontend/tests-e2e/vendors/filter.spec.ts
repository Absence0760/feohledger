import { expect, test , isVendorListResponse} from '../fixtures/helpers';

/**
 * /vendors status-chip filtering. Seed has at least one vendor in
 * each of (active, unverified, rejected). Clicking a chip narrows the
 * table to the matching status; clicking All restores everything.
 */

test.describe('/vendors status filter (acme admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendors');
	});

	test('Unverified chip narrows to vendors in unverified status', async ({ page }) => {
		// `count()` does not auto-wait, and this baseline is what the narrowed
		// count is compared against — so anchor on the unfiltered list being on
		// screen first rather than on a quiet network.
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		const beforeRows = await page.locator('table tbody tr').count();

		const filtered = page.waitForResponse(
			(r) => isVendorListResponse(r.url(), 'status=unverified')
		);
		await page.locator('.filter-chip', { hasText: /^Unverified/ }).click();
		const body = (await (await filtered).json()) as { items: unknown[] };

		await expect(page.locator('.filter-chip', { hasText: /^Unverified/ })).toHaveClass(
			/active/
		);
		// The table re-renders AFTER the response lands. Counting rows straight
		// away read the old, longer list, and the loop then walked indices the
		// narrowed list no longer had. Wait for exactly the rows it carried.
		const rows = page.locator('table tbody tr');
		await expect(rows).toHaveCount(body.items.length);
		expect(body.items.length).toBeGreaterThan(0);
		expect(body.items.length).toBeLessThanOrEqual(beforeRows);
		// Every visible row carries the .unverified class on the <tr>.
		for (let i = 0; i < body.items.length; i++) {
			await expect(rows.nth(i)).toHaveClass(/unverified/);
		}
	});

	test('Active chip narrows to vendors in active status', async ({ page }) => {
		const filtered = page.waitForResponse(
			(r) => isVendorListResponse(r.url(), 'status=active')
		);
		await page.locator('.filter-chip', { hasText: /^Active$/ }).click();
		await filtered;

		await expect(page.locator('.filter-chip', { hasText: /^Active$/ })).toHaveClass(
			/active/
		);
		// Active rows have neither .unverified nor .rejected.
		const rows = page.locator('table tbody tr');
		expect(await rows.count()).toBeGreaterThan(0);
		const first = rows.first();
		await expect(first).not.toHaveClass(/unverified/);
		await expect(first).not.toHaveClass(/rejected/);
	});

	test('Rejected chip narrows to rejected vendors', async ({ page }) => {
		const filtered = page.waitForResponse(
			(r) => isVendorListResponse(r.url(), 'status=rejected')
		);
		await page.locator('.filter-chip', { hasText: /^Rejected$/ }).click();
		await filtered;

		const rows = page.locator('table tbody tr');
		expect(await rows.count()).toBeGreaterThan(0);
		await expect(rows.first()).toHaveClass(/rejected/);
	});

	test('All chip restores the unfiltered list', async ({ page }) => {
		// Narrow first — wait for the filtered fetch.
		const narrowed = page.waitForResponse(
			(r) => isVendorListResponse(r.url(), 'status=unverified')
		);
		await page.locator('.filter-chip', { hasText: /^Unverified/ }).click();
		await narrowed;

		const restored = page.waitForResponse(
			(r) => isVendorListResponse(r.url()) && !r.url().includes('status=')
		);
		await page.locator('.filter-chip', { hasText: /^All\s/ }).click();
		await restored;

		await expect(page.locator('.filter-chip', { hasText: /^All\s/ })).toHaveClass(
			/active/
		);
	});
});
