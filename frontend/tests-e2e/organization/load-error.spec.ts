import { expect, signInAndWait, test } from '../fixtures/helpers';

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

	// A 500's message is only its status reason, so the panel says its own
	// translated failure instead of "Internal Server Error" (or the stub's
	// body). A 4xx's detail is a refusal written for the reader and is kept.
	test('a 5xx panel failure reads the panel fallback; a 4xx keeps the server detail', async ({
		page
	}) => {
		let status = 500;
		await page.route(
			(url) => url.pathname === '/api/organization/branding/custom-domains',
			async (route) => {
				if (route.request().method() !== 'GET') return route.fallback();
				return route.fulfill({
					status,
					contentType: 'application/json',
					body: JSON.stringify({ detail: status === 500 ? 'Internal Server Error' : 'Plan does not include custom domains' })
				});
			}
		);

		await page.goto('/organization?section=custom-domains');
		const error = page.getByTestId('custom-domains-load-error');
		await expect(error).toContainText('Failed to load custom domains');
		await expect(error).not.toContainText('Internal Server Error');

		status = 403;
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('custom-domains-load-error')).toContainText(
			'Plan does not include custom domains'
		);
	});

	// The read-only lock is a disabled <fieldset>, which disables every control
	// inside it. Retry only re-reads, so a failed panel load renders outside the
	// lock: a clerk (no admin-only note on this panel) can press it.
	test('a clerk can press Retry on a failed panel load', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
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
		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

		const error = page.getByTestId('residency-load-error');
		const retry = error.getByRole('button', { name: 'Retry' });
		await expect(retry).toBeEnabled();
		await retry.click();

		await expect(page.getByTestId('residency-load-error')).toHaveCount(0);
		// The recovered panel is back inside the lock: its Save stays disabled.
		const card = page.locator('section.card', {
			has: page.getByRole('heading', { name: 'Data Residency' })
		});
		await expect(card.getByText('Deployment alignment')).toBeVisible();
		await expect(card.getByRole('button', { name: /save/i })).toBeDisabled();
	});
});
