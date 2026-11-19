import { expect, test } from '../fixtures/helpers';

/**
 * /experiments — Conclude is terminal (`POST /api/experiments/{id}/conclude`:
 * no new assignments, no restart), so it arms on the first click and fires on
 * the second, like Delete. It used to fire on one click, beside the reversible
 * Stop.
 *
 * The list and the conclude POST are stubbed; the POST count is the assertion.
 * The exact-pathname list guard is the one `load-states.spec.ts` documents.
 */

const EXP = 'dddddddd-4444-4444-8444-dddddddddddd';

const row = {
	id: EXP,
	name: 'Conclude confirm stub',
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

test.describe('/experiments conclude', () => {
	test('Conclude arms first, fires on the confirm click, and a click elsewhere disarms it', async ({
		page
	}) => {
		let concludes = 0;
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
		await page.route(
			(url) => url.pathname === `/api/experiments/${EXP}/conclude`,
			(route) => {
				concludes += 1;
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ ...row, status: 'concluded' })
				});
			}
		);

		await page.goto('/experiments');
		const tr = page.locator('table tbody tr', { hasText: 'Conclude confirm stub' });
		await expect(tr).toBeVisible();

		// First click arms — nothing sent.
		await tr.getByRole('button', { name: 'Conclude' }).click();
		const confirm = tr.getByRole('button', { name: 'Confirm' });
		await expect(confirm).toBeVisible();
		expect(concludes).toBe(0);

		// A click elsewhere disarms.
		await page.getByRole('heading', { name: 'Workflow Experiments' }).click();
		await expect(tr.getByRole('button', { name: 'Conclude' })).toBeVisible();
		expect(concludes).toBe(0);

		// Arm again, confirm: exactly one POST.
		const posted = page.waitForRequest((r) => new URL(r.url()).pathname.endsWith('/conclude'));
		await tr.getByRole('button', { name: 'Conclude' }).click();
		await tr.getByRole('button', { name: 'Confirm' }).click();
		await posted;
		expect(concludes).toBe(1);
	});
});
