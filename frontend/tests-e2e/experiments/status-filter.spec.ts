import { expect, test } from '../fixtures/helpers';

/**
 * /experiments — the status chip round-trips through `?status=` (Back, reload
 * and a shared link restore it, as on every other list), and a chip that
 * filters every row away says so instead of claiming "No experiments yet."
 * over a page that has some.
 *
 * The list is stubbed; the exact-pathname guard is the one
 * `load-states.spec.ts` documents.
 */

const row = {
	id: 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee',
	name: 'Status filter stub',
	description: '',
	status: 'running',
	workflow_definition_name: 'Default workflow',
	assigned_count: 4,
	workflow_definition_id: 'cccccccc-3333-4333-8333-cccccccccccc',
	split_a_pct: 50,
	primary_metric: 'time_to_approval_days',
	min_sample_per_variant: 10,
	config_a: { steps: [] },
	config_b: { steps: [] },
	started_at: '2024-01-01T00:00:00Z',
	ended_at: null,
	created_at: '2024-01-01T00:00:00Z',
	updated_at: '2024-01-01T00:00:00Z'
};

test.describe('/experiments status filter', () => {
	test('the chip writes ?status=, a reload restores it, and filtered-to-zero is not "empty"', async ({
		page
	}) => {
		await page.route(
			(url) => url.pathname === '/api/experiments',
			(route) =>
				route.request().method() === 'GET'
					? route.fulfill({
							status: 200,
							contentType: 'application/json',
							body: JSON.stringify({ experiments: [row] })
						})
					: route.fallback()
		);

		await page.goto('/experiments');
		await expect(page.locator('table tbody tr', { hasText: 'Status filter stub' })).toBeVisible();

		await page.getByRole('button', { name: /^Concluded/ }).click();
		await expect(page).toHaveURL(/[?&]status=concluded\b/);
		await expect(page.getByTestId('table-empty')).toHaveText('No experiments with this status.');

		await page.reload();
		await expect(page.getByRole('button', { name: /^Concluded/ })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
		await expect(page.getByTestId('table-empty')).toHaveText('No experiments with this status.');

		await page.getByRole('button', { name: /^All/ }).click();
		await expect(page).not.toHaveURL(/status=/);
		await expect(page.locator('table tbody tr', { hasText: 'Status filter stub' })).toBeVisible();
	});
});
