import { expect, test } from '../fixtures/helpers';

/**
 * /cfo — org-wide budget-vs-actual rollup (`GET /api/budgets/rollup`).
 *
 * Before this, a CFO's only consolidated budget view was opening budgets one
 * at a time on `/budgets`. The rollup is grouped BY CURRENCY and never summed
 * across one, and it discloses what the spend legs had to refuse — a linked
 * requisition / PO / invoice denominated in another currency than its budget
 * is excluded (the legs never convert), which makes `committed` / `actual` a
 * FLOOR rather than the whole picture.
 *
 * The response is stubbed so the assertions don't depend on what the shard's
 * tenant happens to hold, and so the partial-figure disclosure — the whole
 * point of the surface — is actually on screen to assert.
 */

function currencyRow(over: Record<string, unknown> = {}) {
	return {
		currency: 'USD',
		budget_count: 2,
		allocated: '3000.10',
		committed: '100.00',
		actual: '260.05',
		remaining: '2640.05',
		utilization_pct: '12.00',
		over_budget_count: 0,
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		...over
	};
}

async function stubRollup(page: import('@playwright/test').Page, body: unknown) {
	await page.route('**/api/budgets/rollup*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(body)
		})
	);
}

test('renders allocated vs committed vs actual, grouped per currency', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 3,
		by_currency: [
			currencyRow({ currency: 'EUR', budget_count: 1, allocated: '500.00', committed: '50.00', actual: '25.00', remaining: '425.00', utilization_pct: '15.00' }),
			currencyRow()
		],
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		insufficient_data: false
	});

	await page.goto('/cfo');

	const card = page.getByTestId('budget-rollup');
	await expect(card).toBeVisible({ timeout: 15_000 });

	// Each currency is its OWN row, formatted in its OWN currency — never one
	// blended figure. 3000.10 + 500.00 must appear nowhere.
	const rows = card.locator('tbody tr');
	await expect(rows).toHaveCount(2);
	await expect(rows.first()).toContainText('EUR');
	await expect(rows.nth(1)).toContainText('USD');
	await expect(card).toContainText('3,000');
	await expect(card).toContainText('500');
	await expect(card).not.toContainText('3,500');

	// No disclosure when nothing was excluded.
	await expect(page.getByTestId('budget-rollup-excluded')).toHaveCount(0);
});

test('discloses currency-excluded rows beside the figures they qualify', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 1,
		by_currency: [currencyRow({ excluded_row_count: 2 })],
		excluded_row_count: 2,
		insufficient_data: false
	});

	await page.goto('/cfo');

	// A partial figure has to SAY it is partial, at the point of reading — the
	// same `role="alert"` treatment the cash-position card gives its
	// unconverted outflows, not a tooltip nobody opens.
	const notice = page.getByTestId('budget-rollup-excluded');
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('2');
	await expect(notice).toContainText(/floor/i);
});

test('discloses POs left out of committed for an unverifiable total', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 1,
		by_currency: [currencyRow({ unverifiable_po_count: 3 })],
		excluded_row_count: 0,
		unverifiable_po_count: 3,
		insufficient_data: false
	});

	await page.goto('/cfo');

	// A PO the ERP could not price at its last sync is not summed into
	// committed (decisions §323); the card says how many, as a floor.
	const notice = page.getByTestId('budget-rollup-unverifiable');
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('3 linked purchase orders are left out of committed');
	await expect(page.getByTestId('budget-rollup-excluded')).toHaveCount(0);
});

test('an over-budget currency raises the breach banner', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 2,
		by_currency: [
			currencyRow({ remaining: '-150.00', over_budget_count: 1 }),
			currencyRow({ currency: 'GBP', over_budget_count: 1 })
		],
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		insufficient_data: false
	});

	await page.goto('/cfo');

	// The count folds across currencies because it is a COUNT of budgets, not
	// money — the one cross-currency sum this surface may perform.
	const banner = page.getByTestId('budget-rollup-over');
	await expect(banner).toBeVisible({ timeout: 15_000 });
	await expect(banner).toContainText('2');
});

test('a currency allocating nothing reports no utilization, never 0%', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 1,
		by_currency: [
			currencyRow({ allocated: '0.00', committed: '0.00', actual: '0.00', remaining: '0.00', utilization_pct: null })
		],
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		insufficient_data: false
	});

	await page.goto('/cfo');

	const card = page.getByTestId('budget-rollup');
	await expect(card).toBeVisible({ timeout: 15_000 });
	// "0% of the budget is used" and "there is no budget to use" are opposite
	// facts, and 0% renders as the reassuring one.
	await expect(card.locator('tbody tr').first()).toContainText('nothing allocated');
	await expect(card.locator('tbody tr').first()).not.toContainText('0.00%');
});

test('no budgets reads as no budgets, not a row of zeros', async ({ page }) => {
	await stubRollup(page, {
		budget_count: 0,
		by_currency: [],
		excluded_row_count: 0,
		unverifiable_po_count: 0,
		insufficient_data: true
	});

	await page.goto('/cfo');

	const card = page.getByTestId('budget-rollup');
	await expect(card).toBeVisible({ timeout: 15_000 });
	await expect(card).toContainText('No budgets have been set up yet.');
	await expect(card.locator('tbody tr')).toHaveCount(0);
});

test('a failed rollup fetch does not take the cash-flow panels down with it', async ({ page }) => {
	await page.route('**/api/budgets/rollup*', (route) => route.fulfill({ status: 500, body: '{}' }));

	await page.goto('/cfo');

	// Its own error line, and the forecast surface still renders — the two are
	// independent fetches on purpose.
	await expect(page.getByTestId('budget-rollup')).toContainText(
		'Failed to load budget vs actual.',
		{ timeout: 15_000 }
	);
	await expect(page.getByTestId('forecast-kpi-row')).toBeVisible();
});
