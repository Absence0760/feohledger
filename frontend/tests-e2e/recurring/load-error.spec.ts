import { expect, test } from '../fixtures/helpers';
import type { Route } from '@playwright/test';

/**
 * `/recurring` — a failed list load is stated in place with `ui/LoadError` and a
 * Retry, never as the table's empty line (clearing the rows made the page assert "no templates match your filters" about a list it never received).
 *
 * Only the FIRST GET of the exact list pathname is failed (the module-request
 * trap in tests-e2e/README.md § Stubbing an API route), so Retry reaches the
 * real backend and the recovery is real.
 */
test('/recurring: a failed list load shows LoadError, and Retry recovers', async ({ page }) => {
	let failed = false;
	await page.route(
		(url) => url.pathname === '/api/recurring',
		async (route: Route) => {
			if (route.request().method() !== 'GET' || failed) return route.fallback();
			failed = true;
			return route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		}
	);

	await page.goto('/recurring');
	const loadError = page.getByTestId('recurring-load-error');
	await expect(loadError).toBeVisible();
	await expect(loadError).toHaveAttribute('role', 'alert');
	await expect(page.getByRole('table')).toHaveCount(0);

	await loadError.getByRole('button').click();
	// Retry unmounts the button it was pressed on; focus goes to <main>, not <body>.
	await expect(page.locator('main')).toBeFocused();
	await expect(loadError).toHaveCount(0);
	await expect(page.getByRole('table')).toBeVisible();
});

/**
 * `/recurring?id=` — only a 404 may say "Template not found". A 500 (or an
 * offline backend) is a failed load, stated in place with a Retry. The id is
 * one no template has: the first GET is failed with a 500, so the page must
 * offer Retry rather than "not found"; Retry then reaches the real backend,
 * whose 404 is the one answer allowed to say "not found".
 */
test('/recurring?id: a server failure offers Retry; only the 404 says not found', async ({
	page
}) => {
	const missing = '00000000-0000-0000-0000-000000000000';
	let failed = false;
	await page.route(
		(url) => url.pathname === `/api/recurring/${missing}`,
		async (route: Route) => {
			if (route.request().method() !== 'GET' || failed) return route.fallback();
			failed = true;
			return route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		}
	);

	await page.goto(`/recurring?id=${missing}`);
	const loadError = page.getByTestId('recurring-deeplink-error');
	await expect(loadError).toBeVisible();
	await expect(loadError).toHaveAttribute('role', 'alert');
	await expect(page.locator('.toast-text', { hasText: 'Template not found' })).toHaveCount(0);

	await loadError.getByRole('button').click();
	await expect(loadError).toHaveCount(0);
	await expect(page.locator('.toast-text', { hasText: 'Template not found' })).toBeVisible();
	// A 404 is final: the dead id leaves the URL so Back / reload does not
	// re-run the lookup and toast again.
	await expect(page).toHaveURL(/\/recurring$/);
});
