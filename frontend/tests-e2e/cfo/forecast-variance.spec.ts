import { expect, test } from '../fixtures/helpers';

/**
 * /cfo — forecast vs actual (`POST /api/analytics/forecast_variance`).
 *
 * That endpoint shipped complete, and disclosure-correct, with no UI at all:
 * a completed payment whose outflow cannot be expressed in the org's reporting
 * currency is EXCLUDED from the month's `actual` and counted on
 * `unconverted_count`, which makes the variance a FLOOR — and nothing rendered
 * that fact. These specs pin the two halves that must not regress: the
 * disclosure reaches the screen with the figures it qualifies, and money the
 * form cannot read is refused rather than repaired.
 *
 * The response is stubbed so the assertions don't depend on what the shard's
 * tenant happens to hold — a seeded tenant reliably produces no unconvertible
 * payment at all, which is exactly the state that let this ship unnoticed.
 */

type Route = import('@playwright/test').Route;
type Page = import('@playwright/test').Page;

function varianceRow(over: Record<string, unknown> = {}) {
	return {
		month: '2026-05',
		forecast: '100000.00',
		actual: '120000.00',
		variance: '20000.00',
		variance_pct: 20,
		unconverted_count: 0,
		...over
	};
}

/** Stub the POST and capture the body the page actually sent. */
async function stubVariance(page: Page, body: unknown) {
	const sent: unknown[] = [];
	await page.route('**/api/analytics/forecast_variance', (route: Route) => {
		sent.push(JSON.parse(route.request().postData() ?? 'null'));
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(body)
		});
	});
	return sent;
}

async function openForm(page: Page) {
	await page.goto('/cfo');
	const card = page.getByTestId('forecast-variance');
	await expect(card).toBeVisible({ timeout: 15_000 });
	await page.getByTestId('forecast-variance-open').click();
	const modal = page.getByRole('dialog', { name: 'Enter your forecast' });
	await expect(modal).toBeVisible();
	return { card, modal };
}

test('renders the partial-figure disclosure above the amounts it qualifies', async ({ page }) => {
	await stubVariance(page, {
		reporting_currency: 'USD',
		rows: [varianceRow({ unconverted_count: 2 }), varianceRow({ month: '2026-06' })]
	});

	const { card, modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	await modal.getByLabel('Forecast outflow for row 1').fill('100000');
	await page.getByTestId('forecast-variance-submit').click();

	// A partial figure has to SAY it is partial, at the point of reading — the
	// same `role="alert"` treatment the cash-position card gives its
	// unconverted outflows, not a tooltip nobody opens.
	const notice = page.getByTestId('forecast-variance-unconverted');
	await expect(notice).toBeVisible({ timeout: 15_000 });
	// The alert is the message alone — the ⓘ HelpTip beside it is not part of
	// the warning, so it sits outside the announced region.
	const alert = notice.getByRole('alert');
	await expect(alert).toContainText('2');
	await expect(alert).toContainText('USD');
	await expect(alert).toContainText(/floor/i);

	// And it sits ABOVE the table it qualifies, not below it.
	const noticeBox = await notice.boundingBox();
	const tableBox = await card.locator('table').boundingBox();
	expect(noticeBox).not.toBeNull();
	expect(tableBox).not.toBeNull();
	expect(noticeBox!.y).toBeLessThan(tableBox!.y);

	// The month carrying the exclusion says so on its own row too.
	await expect(card.locator('tbody tr').first()).toContainText('2 payments excluded');
});

test('no disclosure when every payment converted', async ({ page }) => {
	await stubVariance(page, { reporting_currency: 'USD', rows: [varianceRow()] });

	const { card, modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	await modal.getByLabel('Forecast outflow for row 1').fill('100000');
	await page.getByTestId('forecast-variance-submit').click();

	await expect(card.locator('tbody tr')).toHaveCount(1, { timeout: 15_000 });
	await expect(page.getByTestId('forecast-variance-unconverted')).toHaveCount(0);
});

test('refuses unreadable money instead of sending a repaired figure', async ({ page }) => {
	const sent = await stubVariance(page, { reporting_currency: 'USD', rows: [varianceRow()] });

	const { modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	// A pasted FP&A figure, complete with symbol and separators. Nothing in the
	// client may strip those and send `1200.50` — the amount the variance is
	// measured against must be the amount that was typed, or nothing at all.
	await modal.getByLabel('Forecast outflow for row 1').fill('$1,200.50');
	await page.getByTestId('forecast-variance-submit').click();

	await expect(page.locator('.toast, [role="alert"]').filter({ hasText: /plain number/i })).toBeVisible();
	// The refusal is the point: no request went out at all.
	expect(sent).toHaveLength(0);
	// The dialog stays open on the typed value rather than closing on a lie.
	await expect(modal).toBeVisible();
	await expect(modal.getByLabel('Forecast outflow for row 1')).toHaveValue('$1,200.50');
});

test('sends the typed decimal string verbatim, never a JSON number', async ({ page }) => {
	const sent = await stubVariance(page, {
		reporting_currency: 'USD',
		rows: [varianceRow({ forecast: '100000.55' })]
	});

	const { modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	await modal.getByLabel('Forecast outflow for row 1').fill('100000.55');
	await page.getByTestId('forecast-variance-submit').click();

	await expect(page.getByTestId('forecast-variance')).toContainText('May', { timeout: 15_000 });
	expect(sent).toEqual([{ months: [{ month: '2026-05', forecast: '100000.55' }] }]);
});

test('a month with no forecast reports no variance %, never 0%', async ({ page }) => {
	await stubVariance(page, {
		reporting_currency: 'USD',
		rows: [
			varianceRow({ forecast: '0.00', actual: '5000.00', variance: '5000.00', variance_pct: 0 })
		]
	});

	const { card, modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	await modal.getByLabel('Forecast outflow for row 1').fill('0');
	await page.getByTestId('forecast-variance-submit').click();

	// "We landed exactly on plan" and "there was no plan" are opposite facts,
	// and 0% renders as the reassuring one.
	const firstRow = card.locator('tbody tr').first();
	await expect(firstRow).toContainText('no forecast to compare against', { timeout: 15_000 });
	await expect(firstRow).not.toContainText('0%');
});

test('a failed comparison does not take the cash-flow panels down with it', async ({ page }) => {
	await page.route('**/api/analytics/forecast_variance', (route: Route) =>
		route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'bad month value' }) })
	);

	const { modal } = await openForm(page);
	await modal.getByLabel('Month for row 1').fill('2026-05');
	await modal.getByLabel('Forecast outflow for row 1').fill('100000');
	await page.getByTestId('forecast-variance-submit').click();

	// The backend's own explanation is the actionable half of the refusal, and
	// it stays beside the form the user is still looking at.
	await expect(modal.getByTestId('forecast-variance-error')).toContainText('bad month value', {
		timeout: 15_000
	});
	// Independent surfaces stay up — the panel renders outside the forecast
	// `{#if}` for exactly this reason.
	await expect(page.getByTestId('forecast-kpi-row')).toBeVisible();
});
