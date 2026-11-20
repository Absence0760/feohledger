import { expect, test } from '../fixtures/helpers';

/**
 * The `/invoices` filter chips describe the SAME rows the table shows.
 *
 * `GET /api/invoices/counts` used to take no params and never re-fire on a
 * filter change, so a search matching 3 rows left the chips reading
 * `All 1284 · New 402`. It now takes the list's population filters (through the
 * same `_invoice_list_filters` builder) and the page re-tallies on every
 * search / advanced-filter change.
 *
 * Both responses are stubbed and keyed off whether `search=` is present, so the
 * assertion doesn't depend on the shard tenant's contents.
 */

test('the status chips re-tally to the searched set', async ({ page }) => {
	let countsWithSearch = 0;

	await page.route('**/api/invoices/counts**', async (route) => {
		const url = new URL(route.request().url());
		const searching = (url.searchParams.get('search') ?? '').toLowerCase().includes('globex');
		if (searching) countsWithSearch++;
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				searching
					? { counts: { new: 2, approved: 1 }, total: 3 }
					: { counts: { new: 402, approved: 120, paid: 762 }, total: 1284 }
			)
		});
	});

	await page.route('**/api/invoices?*', async (route) => {
		const url = new URL(route.request().url());
		const searching = (url.searchParams.get('search') ?? '').toLowerCase().includes('globex');
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: [],
				total: searching ? 3 : 1284,
				page: 1,
				page_size: 20
			})
		});
	});

	await page.goto('/invoices');

	// Unsearched: the All chip reads the whole tenant — a locale-grouped count
	// (`formatCount`; the e2e browser runs in en-US).
	const allChip = page.locator('.filter-chip', { hasText: /^All\s/ });
	await expect(allChip).toHaveText(/^All\s+1,284$/);

	// Type a search → the counts endpoint is re-called WITH the term, and the
	// chip re-tallies to the filtered set.
	await page.getByLabel('Search invoices', { exact: true }).fill('globex');

	await expect(allChip).toHaveText(/^All\s+3$/);
	expect(countsWithSearch).toBeGreaterThan(0);
});
