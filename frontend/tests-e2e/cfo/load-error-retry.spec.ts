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
		await expect(page.getByTestId('forecast-kpi-row')).toHaveCount(0);

		mode = 'hold';
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(error).toHaveCount(0);

		release();
		await expect(page.getByTestId('forecast-kpi-row')).toBeVisible();
		await expect(error).toHaveCount(0);
	});
});
