import { expect, signInAndWait, test } from '../fixtures/helpers';
import type { Route } from '@playwright/test';

/**
 * /admin/api-keys — both failed loads render `ui/LoadError`, in its two
 * homes: the page (the key list) and a dialog (the per-key usage view).
 *
 * The in-dialog case is the one that matters. Retry unmounts the button it was
 * pressed on, so LoadError moves focus somewhere first. In a page that is
 * `<main>`; inside `ui/Modal` it must be the dialog itself — `<main>` sits
 * behind the open modal, outside its focus trap, so focusing it would strand a
 * keyboard user behind the dialog they are working in (WCAG 2.4.3).
 *
 * Only the FIRST request of each kind is failed, by exact pathname (the
 * module-request trap in tests-e2e/README.md § Stubbing an API route), so the
 * retry gets a real answer and the recovery is real.
 */

const KEY_ID = 'cccccccc-3333-4333-8333-cccccccccccc';

const KEY_ROW = {
	id: KEY_ID,
	name: 'Load Error Key',
	key_prefix: 'feoh_live_cccc',
	scopes: ['read'],
	created_at: '2024-01-01T00:00:00Z',
	last_used_at: '2024-01-02T00:00:00Z',
	revoked_at: null
};

const USAGE = {
	api_key_id: KEY_ID,
	key_prefix: 'feoh_live_cccc',
	total_requests: 529,
	window_days: 30,
	window_requests: 529,
	last_used_at: '2024-01-02T00:00:00Z',
	daily: [{ usage_date: '2024-01-02', request_count: 529 }]
};

const FAIL = {
	status: 500,
	contentType: 'application/json',
	body: JSON.stringify({ detail: 'boom' })
};

test.describe('/admin/api-keys load errors', () => {
	// Explicit sign-in, matching `admin/api-keys.spec.ts`: every endpoint behind
	// this page is admin-only.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a failed list load shows LoadError, and Retry focuses <main> and renders the list', async ({
		page
	}) => {
		await signInAndWait(page);

		let listFailed = false;
		await page.route(
			(url) => url.pathname === '/api/api-keys',
			async (route: Route) => {
				if (route.request().method() !== 'GET') return route.fallback();
				if (!listFailed) {
					listFailed = true;
					return route.fulfill(FAIL);
				}
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify([KEY_ROW])
				});
			}
		);

		await page.goto('/admin/api-keys');

		const error = page.getByTestId('api-keys-error');
		await expect(error).toBeVisible();
		await error.getByRole('button', { name: 'Retry' }).click();

		await expect(page.locator('main#main-content')).toBeFocused();
		await expect(page.locator('table tbody tr', { hasText: 'Load Error Key' })).toBeVisible();
		await expect(page.getByTestId('api-keys-error')).toHaveCount(0);
	});

	test('a failed usage load inside the dialog keeps focus in the dialog on Retry', async ({
		page
	}) => {
		await signInAndWait(page);

		let usageFailed = false;
		await page.route(
			(url) => url.pathname === '/api/api-keys',
			(route: Route) =>
				route.request().method() === 'GET'
					? route.fulfill({
							status: 200,
							contentType: 'application/json',
							body: JSON.stringify([KEY_ROW])
						})
					: route.fallback()
		);
		await page.route(
			(url) => url.pathname === `/api/api-keys/${KEY_ID}/usage`,
			async (route: Route) => {
				if (!usageFailed) {
					usageFailed = true;
					return route.fulfill(FAIL);
				}
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(USAGE)
				});
			}
		);

		await page.goto('/admin/api-keys');
		await page.locator('table tbody tr', { hasText: 'Load Error Key' }).locator('.row-link').click();

		const dialog = page.getByRole('dialog', { name: 'API key usage' });
		const error = dialog.getByTestId('usage-error');
		await expect(error).toBeVisible();
		await error.getByRole('button', { name: 'Retry' }).click();

		// The dialog, not the <main> behind it.
		await expect(dialog).toBeFocused();
		await expect(dialog.getByTestId('usage-totals')).toContainText('529');
		await expect(dialog.getByTestId('usage-error')).toHaveCount(0);
	});
});
