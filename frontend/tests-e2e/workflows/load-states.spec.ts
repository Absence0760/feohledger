import { expect, test } from '../fixtures/helpers';

/**
 * /workflows and /workflows/[id] — a failed load is stated in place, with a
 * Retry, instead of a dead end.
 *
 * Regressions:
 * - The list read "Couldn’t load this list. Refresh to try again." in the
 *   empty row: correct, but the only way to try again was a full reload.
 * - The editor toasted "Workflow not found" — for a 500 as much as a 404 —
 *   over a "Loading…" line that never went away.
 *
 * Stubbed so the failures are deterministic; Retry is released by flipping a
 * flag the route handler reads, not by a timer.
 */

test.describe('/workflows load states (acme admin)', () => {
	test('a FAILED list load offers Retry, which recovers', async ({ page }) => {
		let fail = true;
		await page.route(
			(url) => url.pathname === '/api/workflows',
			(route) =>
				fail
					? route.fulfill({
							status: 500,
							contentType: 'application/json',
							body: JSON.stringify({ detail: 'boom' })
						})
					: route.fallback()
		);

		await page.goto('/workflows');

		const failed = page.getByTestId('workflows-load-error');
		await expect(failed).toBeVisible();
		await expect(failed).toContainText('Couldn’t load workflows.');
		// Never the "nothing configured" claim on a load we never completed.
		await expect(page.getByTestId('table-empty')).toHaveCount(0);

		fail = false;
		await failed.getByRole('button', { name: 'Retry' }).click();
		await expect(page.locator('table tbody tr', { hasText: 'Default Workflow' })).toBeVisible();
		await expect(failed).toHaveCount(0);
	});

	test('the editor states a 404 in place, with a way back to the list', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/workflows/00000000-0000-0000-0000-000000000000',
			(route) =>
				route.fulfill({
					status: 404,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'Workflow not found' })
				})
		);

		await page.goto('/workflows/00000000-0000-0000-0000-000000000000');

		const failed = page.getByTestId('workflow-load-error');
		await expect(failed).toContainText('This workflow doesn’t exist or was deleted.');
		await failed.getByRole('link', { name: 'Back to workflows' }).click();
		await expect(page).toHaveURL(/\/workflows$/);
	});

	test('the editor says a 500 failed to load — not "not found" — and Retry recovers', async ({
		page
	}) => {
		await page.goto('/workflows');
		const link = page
			.locator('table tbody tr', { hasText: 'Default Workflow' })
			.getByRole('link', { name: /^Edit workflow/ });
		const href = await link.getAttribute('href');
		expect(href).toMatch(/^\/workflows\/[^/]+$/);

		let fail = true;
		await page.route(
			(url) => url.pathname === `/api${href}`,
			(route) =>
				fail
					? route.fulfill({
							status: 500,
							contentType: 'application/json',
							body: JSON.stringify({ detail: 'boom' })
						})
					: route.fallback()
		);
		await page.goto(href!);

		const failed = page.getByTestId('workflow-load-error');
		await expect(failed).toContainText('Couldn’t load this workflow.');
		await expect(failed).not.toContainText('doesn’t exist');

		fail = false;
		await failed.getByRole('button', { name: 'Retry' }).click();
		await expect(page.locator('h1.page-title')).toContainText('Default Workflow');
		await expect(failed).toHaveCount(0);
	});
});
