import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';
import { REPORTING_CURRENCY, dashboardResponse, monthlyTrendRow, vendorSpendRow } from './fixture';

/**
 * Dashboard — the three CHART unconverted disclosures.
 *
 * A foreign invoice with no locked rate into the org's reporting currency is
 * summed at FACE value rather than dropped, and each chart says so at the
 * grain it is read at (`docs/decisions.md` §35): `vendor_spend` NAMES the
 * affected vendors (the tile ranks vendors against each other), the five
 * aging bands carry ONE notice for the set, and `monthly_trend` NAMES the
 * affected months (a trend is read bar against bar).
 *
 * The whole point of a disclosure is that it tracks its own count, so each is
 * pinned in BOTH directions — present when that chart folded a row at face
 * value, absent when it did not. The absent case is the one that needs care:
 * an empty series makes the notice vanish for a reason that has nothing to do
 * with the count (`vendor_spend: []` renders no bars, and the whole
 * monthly-trend card is `{#if data.monthly_trend.length > 0}`), so proving the
 * count is what governs means rendering the series POPULATED with zero counts.
 * A stub with empty series would pass either way.
 *
 * `discount-capture.spec.ts` owns the fourth notice on this page (the discount
 * card's) and `kpi-rollup-disclosures.spec.ts` owns the KPI row's two lines,
 * which are a different question again — every count in `./fixture.ts` that
 * feeds them stays at zero here, so a chart notice can never be one of them
 * misread.
 *
 * The payload lives in `./fixture.ts`, typed `satisfies DashboardData`. That
 * matters most to THIS spec: the shared shape it replaced omitted
 * `aging_reporting`'s `unconverted_count` entirely, `undefined > 0` is false,
 * and the aging notice below could only ever be exercised on its absent
 * branch. `pnpm check` does not typecheck `tests-e2e/`; `pnpm check:e2e` does.
 */

interface Series {
	/** Per-vendor face-value counts, in rank order. */
	vendors?: number[];
	/** The ONE count the API returns for the whole aging band set. */
	aging?: number;
	/** Per-month face-value counts, oldest first. */
	trend?: number[];
}

async function stubDashboard(page: Page, { vendors = [0, 0], aging = 0, trend = [0, 0, 0] }: Series) {
	await page.route('**/api/dashboard*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				dashboardResponse({
					agingUnconverted: aging,
					vendorSpend: [
						vendorSpendRow('Northwind Traders', 3000, vendors[0] ?? 0),
						vendorSpendRow('Contoso Supplies', 1500, vendors[1] ?? 0)
					],
					monthlyTrend: [
						monthlyTrendRow('2026-01', 1800, trend[0] ?? 0),
						monthlyTrendRow('2026-02', 2200, trend[1] ?? 0),
						monthlyTrendRow('2026-03', 2000, trend[2] ?? 0)
					]
				})
			)
		})
	);
}

/** The three chart notices, plus the two KPI-row lines none of them may be. */
const VENDOR_NOTICE = 'unconverted-vendor-spend';
const AGING_NOTICE = 'unconverted-aging';
const TREND_NOTICE = 'unconverted-trend';
const ROLLUP_LINES = ['unconverted-rollup-face-value', 'unconverted-rollup-excluded'];

/** Wait for the dashboard to have actually rendered its charts, so a
 *  `toHaveCount(0)` can't pass against a page that is still loading. */
async function awaitCharts(page: Page) {
	await expect(page.locator('.charts-grid .chart-card').first()).toBeVisible({
		timeout: 15_000
	});
}

test('no chart folded a row at face value — none of the three notices appears', async ({
	page
}) => {
	// Every series is populated; only the counts are zero. That is the whole
	// point: an empty series would hide these notices for the wrong reason.
	await stubDashboard(page, { vendors: [0, 0], aging: 0, trend: [0, 0, 0] });
	await page.goto('/');
	await awaitCharts(page);

	// The bars they qualify ARE on screen, so absence is about the count.
	await expect(page.locator('.vendor-row')).toHaveCount(2);
	await expect(page.locator('.trend-bar-group')).toHaveCount(3);

	await expect(page.getByTestId(VENDOR_NOTICE)).toHaveCount(0);
	await expect(page.getByTestId(AGING_NOTICE)).toHaveCount(0);
	await expect(page.getByTestId(TREND_NOTICE)).toHaveCount(0);
	for (const line of ROLLUP_LINES) await expect(page.getByTestId(line)).toHaveCount(0);
});

test('a part-converted vendor total names the vendor, and only that chart says so', async ({
	page
}) => {
	// Only the SECOND vendor folded rows at face value.
	await stubDashboard(page, { vendors: [0, 3] });
	await page.goto('/');

	const notice = page.getByTestId(VENDOR_NOTICE);
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('3');
	await expect(notice).toContainText(REPORTING_CURRENCY);
	// Naming the vendor is the point — the tile RANKS vendors against each
	// other, so "which total is not comparable" is what a reader acts on.
	await expect(notice).toContainText('Contoso Supplies');
	await expect(notice).not.toContainText('Northwind Traders');

	// The affected row is marked in the bars too, and only that one.
	await expect(page.locator('.vendor-bar.partial')).toHaveCount(1);
	await expect(page.locator('.vendor-amount.partial-amount')).toHaveCount(1);

	// The other two charts converted everything, so they stay silent.
	await expect(page.getByTestId(AGING_NOTICE)).toHaveCount(0);
	await expect(page.getByTestId(TREND_NOTICE)).toHaveCount(0);
});

test('a part-converted aging set says so once, for the whole band set', async ({ page }) => {
	await stubDashboard(page, { aging: 5 });
	await page.goto('/');

	const notice = page.getByTestId(AGING_NOTICE);
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('5');
	await expect(notice).toContainText(REPORTING_CURRENCY);
	// ONE notice for five bands, matching the single count the API returns —
	// not one per band.
	await expect(notice).toHaveCount(1);

	await expect(page.getByTestId(VENDOR_NOTICE)).toHaveCount(0);
	await expect(page.getByTestId(TREND_NOTICE)).toHaveCount(0);
});

test('a part-converted month names the month, and only that bar is marked', async ({ page }) => {
	// Only February folded rows at face value.
	await stubDashboard(page, { trend: [0, 4, 0] });
	await page.goto('/');

	const notice = page.getByTestId(TREND_NOTICE);
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('4');
	await expect(notice).toContainText(REPORTING_CURRENCY);
	// Naming the month is the point — a trend is read bar against bar, so a
	// whole-series count would not say which step not to trust.
	await expect(notice).toContainText('2026-02');
	await expect(notice).not.toContainText('2026-01');

	await expect(page.locator('.trend-bar.partial')).toHaveCount(1);
	await expect(page.locator('.trend-value.partial-amount')).toHaveCount(1);

	await expect(page.getByTestId(VENDOR_NOTICE)).toHaveCount(0);
	await expect(page.getByTestId(AGING_NOTICE)).toHaveCount(0);
});
