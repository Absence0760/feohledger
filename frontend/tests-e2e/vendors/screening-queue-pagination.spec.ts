import { expect, test } from '../fixtures/helpers';

/**
 * `/vendors/screening` — the queue paginates, and the KPI row does NOT come
 * from what it loaded.
 *
 * Regression this pins: "Sanctions matches" / "Needs review" were
 * `items.filter(...).length` over the review queue. That was correct only
 * because the endpoint returned every row AND was selected on exactly those
 * two statuses — a property of the implementation, not of the contract. The
 * moment the queue pages, a client-side derivation is a page-scoped
 * undercount: it reports what is on screen and calls it the whole set.
 *
 * The fixture makes the two disagree on BOTH axes on purpose. 25 flagged
 * vendors (9 `match` + 16 `review`), ordered so the first page of 20 holds 5
 * matches and 15 reviews — so a page-scoped derivation renders 5/15 where the
 * whole-set figures are 9/16.
 */

const PAGE_SIZE = 20;

function row(i: number, status: 'match' | 'review') {
	const n = String(i).padStart(2, '0');
	return {
		vendor_id: `00000000-0000-0000-0000-0000000000${n}`,
		vendor_name: `Flagged Vendor ${n}`,
		screening_status: status,
		last_screened_at: '2026-01-05T10:00:00Z',
		payments_blocked: false,
		risk_level: 'medium',
		risk_score: '55.00',
		latest_matched_list: 'OFAC SDN',
		latest_provider: 'mock',
		latest_categories: ['sanctions'],
		adverse_media: false
	};
}

const ALL = [
	...Array.from({ length: 5 }, (_, i) => row(10 + i, 'match')), // page 1
	...Array.from({ length: 15 }, (_, i) => row(20 + i, 'review')), // page 1
	...Array.from({ length: 4 }, (_, i) => row(40 + i, 'match')), // page 2
	row(50, 'review') // page 2
];
const TOTAL_MATCH = 9;
const TOTAL_REVIEW = 16;

async function mockQueue(page: import('@playwright/test').Page) {
	await page.route('**/api/vendors/screening/review-queue**', async (route) => {
		const url = new URL(route.request().url());
		const p = Number(url.searchParams.get('page') ?? '1');
		const size = Number(url.searchParams.get('page_size') ?? String(PAGE_SIZE));
		const start = (p - 1) * size;
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: ALL.slice(start, start + size),
				total: ALL.length,
				page: p,
				page_size: size
			})
		});
	});
}

async function mockCounts(page: import('@playwright/test').Page) {
	await page.route('**/api/vendors/counts**', async (route) => {
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				total: 200,
				by_status: { active: 199, unverified: 1 },
				payments_blocked: 4,
				by_screening_status: { clear: 175, match: TOTAL_MATCH, review: TOTAL_REVIEW }
			})
		});
	});
}

const kpi = (page: import('@playwright/test').Page, label: string) =>
	page.locator('.kpi', { hasText: label }).locator('.kpi-value');

test.describe('screening review queue — pagination + whole-set KPIs', () => {
	test('KPIs report the whole set while only one page is loaded', async ({ page }) => {
		await mockQueue(page);
		await mockCounts(page);
		await page.goto('/vendors/screening');

		// One page on screen; five more flagged vendors are unfetched.
		await expect(page.locator('.grid-container tbody tr')).toHaveCount(PAGE_SIZE);

		// Whole-set, from GET /api/vendors/counts. A filter over the loaded rows
		// would report 5 and 15 — what this page happens to hold.
		await expect(kpi(page, 'Sanctions matches')).toHaveText(String(TOTAL_MATCH));
		await expect(kpi(page, 'Needs review')).toHaveText(String(TOTAL_REVIEW));
		await expect(kpi(page, 'Payments blocked')).toHaveText('4');
	});

	test('Load more appends the next page and the footer never overstates', async ({ page }) => {
		await mockQueue(page);
		await mockCounts(page);
		await page.goto('/vendors/screening');

		const rows = page.locator('.grid-container tbody tr');
		await expect(rows).toHaveCount(PAGE_SIZE);

		// "Showing all N" must not be on screen while 5 rows are unfetched.
		await expect(page.locator('.load-more-end')).toHaveCount(0);
		const loadMore = page.locator('.btn-load-more');
		await expect(loadMore).toContainText(`${PAGE_SIZE} of ${ALL.length}`);

		await loadMore.click();

		await expect(rows).toHaveCount(ALL.length);
		// No row duplicated across the boundary, none dropped.
		const names = await page.locator('.grid-container tbody tr .vendor-name').allInnerTexts();
		expect(new Set(names).size).toBe(ALL.length);
		// Only now is the end-of-list claim true.
		await expect(page.locator('.btn-load-more')).toHaveCount(0);
		await expect(page.locator('.load-more-end')).toContainText(String(ALL.length));

		// The KPIs did not move — they never described the loaded rows.
		await expect(kpi(page, 'Sanctions matches')).toHaveText(String(TOTAL_MATCH));
		await expect(kpi(page, 'Needs review')).toHaveText(String(TOTAL_REVIEW));
	});

	test('search is sent to the server, not applied to the loaded page', async ({ page }) => {
		await mockCounts(page);
		const searchTerms: (string | null)[] = [];
		await page.route('**/api/vendors/screening/review-queue**', async (route) => {
			const url = new URL(route.request().url());
			searchTerms.push(url.searchParams.get('search'));
			const term = (url.searchParams.get('search') ?? '').toLowerCase();
			const matched = term ? ALL.filter((r) => r.vendor_name.toLowerCase().includes(term)) : ALL;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: matched.slice(0, PAGE_SIZE),
					total: matched.length,
					page: 1,
					page_size: PAGE_SIZE
				})
			});
		});

		await page.goto('/vendors/screening');
		await expect(page.locator('.grid-container tbody tr')).toHaveCount(PAGE_SIZE);

		// `Flagged Vendor 50` is the LAST of 25 — it is not on the loaded first
		// page, so a client-side filter could only report "no vendors match".
		await page.locator('.search-box input').fill('Vendor 50');

		await expect(page.locator('.grid-container tbody tr')).toHaveCount(1);
		await expect(page.locator('.grid-container tbody tr .vendor-name')).toContainText(
			'Flagged Vendor 50'
		);
		expect(searchTerms).toContain('Vendor 50');
		// The issued term is reflected in the URL, so a reload keeps the view.
		await expect(page).toHaveURL(/[?&]search=Vendor(\+|%20)50/);
	});

	test('a bookmarked ?search= loads the filtered queue once', async ({ page }) => {
		await mockCounts(page);
		const searchTerms: (string | null)[] = [];
		await page.route('**/api/vendors/screening/review-queue**', async (route) => {
			const url = new URL(route.request().url());
			searchTerms.push(url.searchParams.get('search'));
			const term = (url.searchParams.get('search') ?? '').toLowerCase();
			const matched = term ? ALL.filter((r) => r.vendor_name.toLowerCase().includes(term)) : ALL;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: matched.slice(0, PAGE_SIZE),
					total: matched.length,
					page: 1,
					page_size: PAGE_SIZE
				})
			});
		});

		await page.goto('/vendors/screening?search=Vendor%2050');
		await expect(page.locator('.search-box input')).toHaveValue('Vendor 50');
		await expect(page.locator('.grid-container tbody tr')).toHaveCount(1);
		// The mount load carried the term, and the debounce did not re-issue it.
		await expect(page.locator('.load-more-end')).toContainText('1');
		expect(searchTerms).toEqual(['Vendor 50']);
	});

	test('a failed load states it in place with a Retry', async ({ page }) => {
		await mockCounts(page);
		let served = 0;
		await page.route('**/api/vendors/screening/review-queue**', async (route) => {
			served += 1;
			if (served === 1) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: ALL.slice(0, PAGE_SIZE), total: ALL.length, page: 1, page_size: PAGE_SIZE })
			});
		});

		await page.goto('/vendors/screening');
		const error = page.getByTestId('screening-queue-load-error');
		await expect(error).toContainText('Could not load the review queue');
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(page.locator('.grid-container tbody tr')).toHaveCount(PAGE_SIZE);
	});
});
