import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * `/invoices` query-string ownership — a filter and a sort must survive each
 * other, a reload, and a deep-link modal close.
 *
 * The page used to have TWO URL writers (`syncUrl()` for the search / status /
 * assignee filters, `syncSortUrl()` for the column sort), and each rebuilt the
 * query string from `$page.url`. SvelteKit's shallow-routing `replaceState`
 * writes `history` and `page.state` but never updates `page.url`, so both
 * writers read a URL frozen at the last real navigation: whichever wrote second
 * dropped the other's params outright. Sorting after a search wiped `search=`;
 * the next filter change wiped `sort=`. `?id=` had the mirror-image problem —
 * closing a deep-linked modal scrubbed it, and the next filter write read it
 * back off the frozen URL and re-armed the deep link.
 *
 * These assertions are the guard on the single-writer fix. They only assert the
 * URL/request contract, never a specific ordering of seed data.
 */

const invoiceRows = (page: import('@playwright/test').Page) =>
	page.locator('table tbody tr');

test.describe('/invoices URL state', () => {
	test('a status filter and a column sort both survive, and survive a reload', async ({
		page
	}) => {
		await page.goto('/invoices');
		await expect(invoiceRows(page).first()).toBeVisible();

		// 1. Filter first.
		const filtered = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('status=approved')
		);
		await page.locator('.filter-chip', { hasText: /^Approved\s/ }).click();
		await filtered;
		await expect(page).toHaveURL(/status=approved/);

		// 2. Then sort. The filter must still be in the URL AND in the request.
		const sorted = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=amount') &&
				r.url().includes('order=asc') &&
				r.url().includes('status=approved')
		);
		await page.getByRole('button', { name: /^Amount/ }).click();
		await sorted;

		await expect(page).toHaveURL(/status=approved/);
		await expect(page).toHaveURL(/sort=amount/);
		await expect(page).toHaveURL(/order=asc/);

		// 3. And both survive a reload of that URL.
		const url = page.url();
		const reloaded = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=amount') &&
				r.url().includes('status=approved')
		);
		await page.goto(url);
		await reloaded;
		await expect(invoiceRows(page).first()).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: /^Approved\s/ })).toHaveClass(
			/active/
		);
	});

	test('sorting first, then filtering, keeps the sort', async ({ page }) => {
		await page.goto('/invoices');
		await expect(invoiceRows(page).first()).toBeVisible();

		const sorted = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('sort=vendor_name')
		);
		await page.getByRole('button', { name: 'Vendor' }).click();
		await sorted;
		await expect(page).toHaveURL(/sort=vendor_name/);

		// The filter effect is the OTHER writer that used to clobber the sort.
		const filtered = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('status=approved') &&
				r.url().includes('sort=vendor_name')
		);
		await page.locator('.filter-chip', { hasText: /^Approved\s/ }).click();
		await filtered;

		await expect(page).toHaveURL(/sort=vendor_name/);
		await expect(page).toHaveURL(/status=approved/);
	});

	test('a search term and a sort coexist in the URL', async ({ page }) => {
		await page.goto('/invoices');
		await expect(invoiceRows(page).first()).toBeVisible();

		const searched = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('search=INV')
		);
		await page.getByPlaceholder('Search invoices...').fill('INV');
		await searched;
		await expect(page).toHaveURL(/search=INV/);

		const sorted = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=amount') &&
				r.url().includes('search=INV')
		);
		await page.getByRole('button', { name: /^Amount/ }).click();
		await sorted;

		// The sort write used to rebuild from a frozen URL with no `search=`.
		await expect(page).toHaveURL(/search=INV/);
		await expect(page).toHaveURL(/sort=amount/);
	});

	test('closing a deep-linked modal scrubs id but keeps the active sort', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as { items: Array<{ id: string }> };
		const target = listed.items[0];
		expect(target).toBeTruthy();

		await page.goto(`/invoices?id=${target.id}&sort=amount&order=asc`);

		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();

		// `id` gone, sort intact — one writer owns both.
		await expect(page).toHaveURL(/sort=amount/);
		await expect(page).toHaveURL(/order=asc/);
		await expect(page).not.toHaveURL(/[?&]id=/);
		await expect(modal).toBeHidden();

		// And a later filter change must not resurrect `id` off the frozen URL.
		const filtered = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('status=approved')
		);
		await page.locator('.filter-chip', { hasText: /^Approved\s/ }).click();
		await filtered;
		await expect(page).not.toHaveURL(/[?&]id=/);
		await expect(page).toHaveURL(/sort=amount/);
	});

	/**
	 * A navigation that REUSES the mounted page must re-read the URL.
	 *
	 * The filter state was read from `$page.url` once, at mount. Clicking the
	 * sidebar's Invoices row while on a filtered view is a real navigation to
	 * the bare `/invoices`, but the component is not remounted — so the URL went
	 * bare while the Approved chip stayed pressed and the table stayed filtered.
	 * Back then returned to `?status=approved`, and Forward to a bare URL over a
	 * filtered table: the view and the address bar disagreed in both directions,
	 * and a reload of either "fixed" it into something else.
	 */
	test('a same-route navigation and back/forward re-apply the URL', async ({ page }) => {
		const approvedChip = page.locator('.filter-chip', { hasText: /^Approved\s/ });
		const allChip = page.locator('nav.filters .filter-chip').first();

		const filtered = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('status=approved')
		);
		await page.goto('/invoices?status=approved');
		await filtered;
		await expect(approvedChip).toHaveClass(/active/);

		// The sidebar row: a real navigation to the same route, unfiltered.
		const unfiltered = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && !r.url().includes('status=')
		);
		await page.getByRole('link', { name: 'Invoices', exact: true }).click();
		await unfiltered;
		await expect(page).toHaveURL(/\/invoices$/);
		await expect(allChip).toHaveClass(/active/);
		await expect(approvedChip).not.toHaveClass(/active/);

		// Back to the filtered entry: the view follows.
		const refiltered = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('status=approved')
		);
		await page.goBack();
		await refiltered;
		await expect(page).toHaveURL(/status=approved/);
		await expect(approvedChip).toHaveClass(/active/);

		// And Forward to the bare one.
		const again = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && !r.url().includes('status=')
		);
		await page.goForward();
		await again;
		await expect(allChip).toHaveClass(/active/);
	});
});
