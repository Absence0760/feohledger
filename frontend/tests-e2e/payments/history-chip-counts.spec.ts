import { expect, test } from '../fixtures/helpers';

/**
 * Payments History filter-chip counts reflect the whole set, not the loaded page.
 *
 * Regression: the History chips (incl. "All") counted only the loaded
 * (page-1, size-20) payment array, so they undercounted once history
 * paginated. The page now reads tallies from GET /api/payments/counts. We
 * mock that at 30 completed while the list page returns 2 rows, and assert
 * the All chip shows 30.
 *
 * Two later regressions are covered below, because the first test passes while
 * both are broken:
 *   2. the chips must send the list's own filters. `/api/payments/counts`
 *      declared no query parameters at all and grouped over the whole
 *      entity-scoped set, so a one-vendor search left the chips reading the
 *      tenant's total over a one-row table.
 *   3. the counts fetch must be sequenced. It fires from the debounced-search
 *      path alongside the list fetch, so a slow response for an older term can
 *      land after a faster newer one and leave the chips describing a search
 *      the table is no longer showing.
 */

const SEARCH = 'Search payments...';

function payment(suffix: string) {
	return {
		id: `00000000-0000-0000-0000-0000000000${suffix}`,
		invoice_id: null,
		amount: '10.00',
		currency: 'USD',
		method: 'ach',
		status: 'completed',
		reference: null,
		created_at: '2026-06-01T00:00:00Z'
	};
}

function listBody(total: number, rows = 2) {
	return JSON.stringify({
		items: Array.from({ length: rows }, (_, i) => payment(`c${i + 1}`)),
		total,
		page: 1,
		page_size: 20
	});
}

function countsBody(total: number) {
	return JSON.stringify({ total, by_status: { completed: total } });
}

/** Switch to the History tab, where the chips render. */
async function openHistory(page: import('@playwright/test').Page) {
	await page.goto('/payments');
	await page.getByRole('tab', { name: 'History', exact: true }).click();
}

const allChip = (page: import('@playwright/test').Page) =>
	page.locator('.filter-chip', { hasText: 'All' });

test.describe('payments history chip counts', () => {
	test('All chip reflects the counts endpoint, not the loaded page', async ({ page }) => {
		await page.route('**/api/payments/counts**', (r) =>
			r.fulfill({ status: 200, contentType: 'application/json', body: countsBody(30) })
		);
		// History list returns only two rows — far fewer than 30.
		await page.route(/\/api\/payments\?/, (r) =>
			r.fulfill({ status: 200, contentType: 'application/json', body: listBody(30) })
		);

		await openHistory(page);

		// The "All" chip carries the whole-set count (30), not the 2 loaded.
		await expect(allChip(page)).toBeVisible();
		await expect(allChip(page)).toContainText('30');
	});

	test('the counts request carries the active search term', async ({ page }) => {
		// Every counts URL the page requests, in order. The assertion is on the
		// REQUEST, not the response: the endpoint honouring `search` is useless
		// if the page never sends it, which is exactly how this broke.
		const countsUrls: string[] = [];
		await page.route('**/api/payments/counts**', (r) => {
			const url = new URL(r.request().url());
			countsUrls.push(url.search);
			// Answer the searched request with the narrowed tally, so the chip
			// value doubles as proof the searched response is what landed.
			const term = url.searchParams.get('search') ?? '';
			return r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: countsBody(term ? 1 : 30)
			});
		});
		await page.route(/\/api\/payments\?/, (r) =>
			r.fulfill({ status: 200, contentType: 'application/json', body: listBody(1, 1) })
		);

		await openHistory(page);
		await expect(allChip(page)).toContainText('30');

		await page.getByPlaceholder(SEARCH).fill('globex');

		// Wait on the searched counts request itself, not on a timer.
		await page.waitForResponse(
			(res) => res.url().includes('/api/payments/counts') && res.url().includes('search=globex')
		);
		await expect(allChip(page)).toContainText('1');

		expect(countsUrls.some((s) => s.includes('search=globex'))).toBe(true);
	});

	test('a stale counts response cannot overwrite a newer one', async ({ page }) => {
		// Hold the FIRST search's counts response open, let the second answer,
		// then release the first. The chips must keep the second's value —
		// without the request sequencer the late response clobbers it.
		let releaseFirst: (() => void) | undefined;
		const firstHeld = new Promise<void>((resolve) => {
			releaseFirst = resolve;
		});

		await page.route('**/api/payments/counts**', async (r) => {
			const term = new URL(r.request().url()).searchParams.get('search') ?? '';
			if (term === 'aaa') {
				await firstHeld;
				return r.fulfill({ status: 200, contentType: 'application/json', body: countsBody(111) });
			}
			if (term === 'bbb') {
				return r.fulfill({ status: 200, contentType: 'application/json', body: countsBody(222) });
			}
			return r.fulfill({ status: 200, contentType: 'application/json', body: countsBody(30) });
		});
		await page.route(/\/api\/payments\?/, (r) =>
			r.fulfill({ status: 200, contentType: 'application/json', body: listBody(1, 1) })
		);

		await openHistory(page);
		await expect(allChip(page)).toContainText('30');

		const search = page.getByPlaceholder(SEARCH);

		// First term — its counts request is issued and then held.
		const firstRequested = page.waitForRequest(
			(req) => req.url().includes('/api/payments/counts') && req.url().includes('search=aaa')
		);
		await search.fill('aaa');
		await firstRequested;

		// Second term — answers immediately, so the chip lands on 222.
		await search.fill('bbb');
		await page.waitForResponse(
			(res) => res.url().includes('/api/payments/counts') && res.url().includes('search=bbb')
		);
		await expect(allChip(page)).toContainText('222');

		// Now let the stale first response through.
		releaseFirst?.();
		await page.waitForResponse(
			(res) => res.url().includes('/api/payments/counts') && res.url().includes('search=aaa')
		);

		// The chip must still describe 'bbb'. Asserting a NEGATIVE (111 never
		// appears) needs a settle window, so this waits on the stale response
		// above first and then re-asserts — the wait IS the assertion.
		await expect(allChip(page)).toContainText('222');
		await expect(allChip(page)).not.toContainText('111');
	});
});
