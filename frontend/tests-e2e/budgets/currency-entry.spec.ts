import { expect, test } from '../fixtures/helpers';

/**
 * The budget modal's currency is `ui/CurrencyInput`. A new budget follows the
 * org's currency; an existing one opens on its OWN code (`bind:touched`) and
 * is never re-seeded when the org currency resolves after the modal mounts.
 *
 * Org, list and summary are stubbed so nothing depends on the shard's data.
 */
const EUR_BUDGET = {
	id: '00000000-0000-4000-8000-0000000c0e01',
	name: 'E2E Ccy Budget',
	dimension: 'department',
	dimension_value: 'Ops',
	period: '2026',
	period_start: null,
	period_end: null,
	amount: 1000,
	currency: 'EUR',
	notes: null,
	created_at: '2026-01-01T00:00:00Z',
	updated_at: '2026-01-01T00:00:00Z'
};

test.beforeEach(async ({ page }) => {
	await page.route('**/api/organization', (route) =>
		route.request().method() === 'GET'
			? route.fulfill({
					json: { settings: { reporting_currency: 'GBP' }, resolved_reporting_currency: 'GBP' }
				})
			: route.continue()
	);
	await page.route('**/api/budgets**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/budgets/summary') {
			return route.fulfill({
				json: { total: 1, by_currency: [{ currency: 'EUR', total: '1000.00', count: 1 }] }
			});
		}
		if (url.pathname === '/api/budgets' && route.request().method() === 'GET') {
			return route.fulfill({ json: { items: [EUR_BUDGET], total: 1, page: 1, page_size: 50 } });
		}
		return route.continue();
	});
	await page.goto('/budgets');
});

test('a new budget seeds its currency from the org', async ({ page }) => {
	await page.getByRole('button', { name: '+ New Budget' }).click();
	const currency = page.getByTestId('budget-currency');
	await expect(currency).toHaveValue('GBP');
	await currency.fill('zar');
	await expect(currency).toHaveValue('ZAR');
});

test('an existing budget keeps its own currency', async ({ page }) => {
	await page.getByRole('button', { name: 'Open budget E2E Ccy Budget' }).click();
	await expect(page.getByTestId('budget-currency')).toHaveValue('EUR');
});
