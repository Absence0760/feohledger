import { expect, test } from '../fixtures/helpers';

/**
 * The search term + status filter on /invoices, /vendors and /payments
 * (History tab) are URL-backed (`?search=&status=`, plus `?tab=history` on
 * payments) — a reload / back / shared link reproduces the view, matching
 * what /contracts and /expenses already do (issue #328, persona-power-user).
 *
 * Sort was already persisted; search + status were not. Each case here:
 *   1. picks a status chip and types a search term,
 *   2. asserts both reach the URL,
 *   3. reloads and asserts the controls come back AND the list request on
 *      reload carries both params.
 *
 * The debounce + response-sequencing behaviour these `syncUrl()` calls sit
 * next to is guarded separately by `search-debounce-race.spec.ts`.
 */

interface Case {
	name: string;
	route: string;
	searchPlaceholder: string;
	/** A status chip that always renders for the acme seed. */
	statusChip: RegExp;
	expectedStatus: string;
	/** Payments: switch to the History tab first. */
	before?: (page: import('@playwright/test').Page) => Promise<void>;
	/** Payments: the tab is persisted too. */
	expectTabParam?: boolean;
}

const CASES: Case[] = [
	{
		name: 'invoices',
		route: '/invoices',
		searchPlaceholder: 'Search invoices...',
		statusChip: /^New\s/,
		expectedStatus: 'new'
	},
	{
		name: 'vendors',
		route: '/vendors',
		searchPlaceholder: 'Search vendors...',
		statusChip: /^Unverified/,
		expectedStatus: 'unverified'
	},
	{
		name: 'payments (history)',
		route: '/payments',
		searchPlaceholder: 'Search payments...',
		statusChip: /^Completed/,
		expectedStatus: 'completed',
		before: async (page) => {
			await page.getByRole('tab', { name: /History/ }).click();
		},
		expectTabParam: true
	}
];

const TERM = 'ZZ-URLTEST-QUERY';

for (const c of CASES) {
	test(`${c.name}: search + status filter survive a reload`, async ({ page }) => {
		await page.goto(c.route);
		if (c.before) await c.before(page);

		await page.locator('.filter-chip', { hasText: c.statusChip }).first().click();
		await expect(page).toHaveURL(new RegExp(`[?&]status=${c.expectedStatus}`), { timeout: 5_000 });

		await page.getByPlaceholder(c.searchPlaceholder).fill(TERM);
		// Debounced (300ms) into the URL alongside the fetch.
		await expect(page).toHaveURL(new RegExp(`[?&]search=${TERM}`), { timeout: 5_000 });
		if (c.expectTabParam) await expect(page).toHaveURL(/[?&]tab=history/);

		// Reload — the list request must carry BOTH params, restored from the URL.
		const restoredFetch = page.waitForRequest(
			(r) =>
				r.url().includes(`search=${TERM}`) && r.url().includes(`status=${c.expectedStatus}`)
		);
		await page.reload();
		await restoredFetch;

		if (c.before) await c.before(page); // no-op if the tab already restored
		await expect(page.getByPlaceholder(c.searchPlaceholder)).toHaveValue(TERM);
		await expect(
			page.locator('.filter-chip', { hasText: c.statusChip }).first()
		).toHaveClass(/active/);
	});
}
