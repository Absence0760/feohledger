import { expect, test } from '../fixtures/helpers';

/**
 * /organization — a failed load says so, and Retry recovers (#635).
 *
 * `org` is only set on success, and the failure used to be reported by a
 * five-second toast alone, so the template's fallback branch read "Loading…"
 * forever: a dead end with no way forward short of a reload. The page now
 * renders `ui/LoadError` in that branch.
 *
 * Only the FIRST `GET /api/organization` is failed, by exact pathname (the
 * module-request trap in tests-e2e/README.md § Stubbing an API route), so the
 * retry reaches the real backend and the recovery is real.
 */
test.describe('/organization load error', () => {
	test('a failed load shows an error with Retry, and Retry renders the page', async ({ page }) => {
		let failed = false;
		await page.route(
			(url) => url.pathname === '/api/organization',
			async (route) => {
				if (!failed && route.request().method() === 'GET') {
					failed = true;
					return route.fulfill({
						status: 500,
						contentType: 'application/json',
						body: JSON.stringify({ detail: 'boom' })
					});
				}
				return route.fallback();
			}
		);

		await page.goto('/organization');

		const error = page.getByTestId('org-load-error');
		await expect(error).toBeVisible();
		await expect(error).toContainText('Failed to load organization');
		// The loading line is gone — it is the dead end this replaced.
		await expect(page.getByText('Loading...', { exact: true })).toHaveCount(0);

		await error.getByRole('button', { name: 'Retry' }).click();

		// Retry unmounts the button it was pressed on. Focus goes to the page's
		// <main> rather than falling to <body> (WCAG 2.4.3).
		await expect(page.locator('main#main-content')).toBeFocused();

		await expect(page.getByRole('heading', { name: 'Getting started' })).toBeVisible();
		await expect(page.getByTestId('org-load-error')).toHaveCount(0);
	});

	// A panel that loads its own data (residency, custom domains, chat, SSO,
	// email intake) used to print its failure as a bare line with no way out
	// but a full reload. It renders the same LoadError, and Retry re-runs that
	// panel's own load.
	test('a failed panel load offers Retry, and Retry loads the panel', async ({ page }) => {
		let failed = false;
		await page.route(
			(url) => url.pathname === '/api/organization/data-residency',
			async (route) => {
				if (!failed && route.request().method() === 'GET') {
					failed = true;
					return route.fulfill({
						status: 500,
						contentType: 'application/json',
						body: JSON.stringify({ detail: 'boom' })
					});
				}
				return route.fallback();
			}
		);

		await page.goto('/organization?section=residency');

		const error = page.getByTestId('residency-load-error');
		await expect(error).toBeVisible();
		await error.getByRole('button', { name: 'Retry' }).click();

		await expect(page.getByTestId('residency-load-error')).toHaveCount(0);
		const card = page.locator('section.card', {
			has: page.getByRole('heading', { name: 'Data Residency' })
		});
		await expect(card.getByText('Deployment alignment')).toBeVisible();
	});
});
