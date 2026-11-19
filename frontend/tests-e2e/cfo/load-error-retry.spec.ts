import type { Route } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * /cfo — a failed forecast load offers a Retry that really retries.
 *
 * The page fetches the forecast, what-if and cash-position panels together;
 * any one failing renders `ui/LoadError` (`cfo-load-error`). Its Retry must
 * take the block down while the request is out (the loading line shows) and
 * give way to the forecast once it answers. The forecast request is failed,
 * then HELD on a promise the test releases — no sleeps, no count-based stubs
 * a second mount-time fetch could shift.
 *
 * The KPI row is never removed: a failed load leaves its cards
 * `unavailable` (the banner under the row says why), a load in flight marks
 * them `pending`, and an answer fills them (`KpiCard`'s `data-kpi-state`).
 * So the spec reads the cards' state, not whether the row exists.
 */
test.describe('/cfo load error', () => {
	test('Retry clears the error while out and renders the forecast', async ({ page }) => {
		let mode: 'fail' | 'hold' = 'fail';
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		await page.route('**/api/analytics/cashflow_forecast*', async (route: Route) => {
			if (new URL(route.request().url()).pathname !== '/api/analytics/cashflow_forecast') {
				await route.continue();
				return;
			}
			if (mode === 'fail') {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await held;
			await route.continue();
		});

		await page.goto('/cfo');
		const error = page.getByTestId('cfo-load-error');
		await expect(error).toBeVisible();
		const kpis = page.getByTestId('forecast-kpi-row').locator('[data-kpi-state]');
		// The three forecast cards: no figure, and not claiming to load one.
		for (const i of [0, 1, 2]) {
			await expect(kpis.nth(i)).toHaveAttribute('data-kpi-state', 'unavailable');
		}

		mode = 'hold';
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(error).toHaveCount(0);
		await expect(kpis.first()).toHaveAttribute('data-kpi-state', 'pending');

		release();
		for (const i of [0, 1, 2]) {
			await expect(kpis.nth(i)).toHaveAttribute('data-kpi-state', 'value');
		}
		await expect(error).toHaveCount(0);
	});
});
