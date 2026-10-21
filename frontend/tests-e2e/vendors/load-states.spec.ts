import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * /vendors chooses its empty message by state (#635 finding 20). It used to
 * read "No vendors yet" while loading and after a failed load, with an English
 * five-second toast as the only sign of the failure.
 *
 * Stubs match the EXACT pathname — `/api/vendors` is also the module
 * `#lib/api/vendors.ts` under vite dev, and `/api/vendors/counts` carries the
 * same query (tests-e2e/README.md § Stubbing an API route).
 */

const LIST = (url: URL) => url.pathname === '/api/vendors';
const COUNTS = (url: URL) => url.pathname === '/api/vendors/counts';

async function stubEmptyTenant(page: Page) {
	await page.route(LIST, (route) => route.fulfill({ json: { items: [], total: 0 } }));
	await page.route(COUNTS, (route) => route.fulfill({ json: { total: 0, by_status: {} } }));
}

test.describe('/vendors load states', () => {
	test('a failed load shows the error with Retry, never "No vendors yet"', async ({ page }) => {
		let fail = true;
		await page.route(LIST, (route) =>
			fail ? route.fulfill({ status: 500, json: { detail: 'boom' } }) : route.fallback()
		);
		await page.goto('/vendors');

		const error = page.getByTestId('vendors-load-error');
		await expect(error).toContainText('Could not load vendors. Try again.');
		await expect(page.getByTestId('vendors-empty-state')).toHaveCount(0);
		await expect(page.getByText('No vendors yet')).toHaveCount(0);
		await expect(page.locator('table')).toHaveCount(0);

		// Retry fetches again and the real list replaces the error.
		fail = false;
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(page.locator('table tbody td.vendor-name').first()).toBeVisible();
		await expect(page.getByTestId('vendors-load-error')).toHaveCount(0);
	});

	test('while loading the table says so, not "No vendors yet"', async ({ page }) => {
		let release!: () => void;
		const held = new Promise<void>((r) => (release = r));
		await page.route(LIST, async (route) => {
			await held;
			await route.fallback();
		});
		await page.goto('/vendors');

		await expect(page.locator('table tbody')).toContainText('Loading…');
		await expect(page.getByTestId('vendors-empty-state')).toHaveCount(0);
		await expect(page.getByText('No vendors yet')).toHaveCount(0);

		release();
		await expect(page.locator('table tbody td.vendor-name').first()).toBeVisible();
	});

	test('a fresh tenant gets the onboarding state', async ({ page }) => {
		await stubEmptyTenant(page);
		await page.goto('/vendors');
		await expect(page.getByTestId('vendors-empty-state')).toBeVisible();
		await expect(page.getByTestId('vendors-load-error')).toHaveCount(0);
	});

	test('a search that matches nothing says so in the table', async ({ page }) => {
		await page.goto('/vendors?search=zz-no-vendor-matches-this');
		await expect(page.locator('table tbody')).toContainText('No vendors found.');
		await expect(page.getByTestId('vendors-empty-state')).toHaveCount(0);
	});
});
