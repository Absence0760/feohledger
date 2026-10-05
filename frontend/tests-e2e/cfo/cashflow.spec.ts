import type { CashPosition } from '$lib/types/analytics';
import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /cfo — Predictive cash-flow forecasting dashboard.
 *
 * Default storage state signs the worker's admin in, so admin reaches
 * the page. The CFO surface is gated to admin + cfo: an ap_clerk must
 * neither see the Cash Flow nav item nor render the page content.
 *
 * The forecast/what-if/cash-position panels are driven by the seeded
 * tenant's invoices + payment schedules, so we assert structure (cards,
 * controls, table) rather than exact dollar amounts, which the seed may
 * evolve.
 */

test.describe('/cfo (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/cfo');
	});

	test('renders the forecast surface with KPI cards and controls', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Cash Flow' })).toBeVisible();
		// KPI row populated from /cashflow_forecast + /cashflow_whatif. Scoped to
		// the forecast's own row — the page also embeds the CFO-metrics section
		// (CfoMetrics.svelte) below, which renders its own .kpi cards using the
		// same shared class.
		const forecastKpis = page.getByTestId('forecast-kpi-row').locator('.kpi');
		await expect(forecastKpis.first()).toBeVisible({ timeout: 10_000 });
		await expect(forecastKpis).toHaveCount(4);
		// Granularity + horizon segmented controls present.
		await expect(page.locator('.seg-btn', { hasText: 'week' })).toBeVisible();
	});

	test('toggling granularity re-requests and keeps the chart', async ({ page }) => {
		await expect(page.locator('.chart-card h2', { hasText: /Projected outflows/ })).toBeVisible({
			timeout: 10_000
		});
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/analytics/cashflow_forecast') && r.url().includes('granularity=month')
		);
		const month = page.getByRole('group', { name: 'Granularity' }).getByRole('button', {
			name: 'month'
		});
		await expect(month).toHaveAttribute('aria-pressed', 'false');
		await month.click();
		await respPromise;
		// The chosen value is exposed, not only painted (WCAG 4.1.2).
		await expect(month).toHaveAttribute('aria-pressed', 'true');
		await expect(
			page.getByRole('group', { name: 'Granularity' }).getByRole('button', { pressed: true })
		).toHaveCount(1);
		await expect(page.locator('.chart-card h2', { hasText: 'Projected outflows (month)' })).toBeVisible();
	});

	test('what-if scenario cards render with discount highlighted on early', async ({ page }) => {
		await expect(page.locator('.chart-card h2', { hasText: 'Payment-timing what-if' })).toBeVisible({
			timeout: 10_000
		});
		await expect(page.locator('.scenario-card')).toHaveCount(3);
		await expect(page.locator('.scenario-card.best .scenario-title')).toHaveText('Pay early');
	});

	test('entering an opening balance + threshold updates the cash position', async ({ page }) => {
		await expect(page.locator('.chart-card h2', { hasText: 'Cash position' })).toBeVisible({
			timeout: 10_000
		});
		const respPromise = page.waitForResponse((r) =>
			r.url().includes('/api/analytics/cash_position') && r.url().includes('opening_balance=500000')
		);
		await page.getByLabel('Opening bank balance').fill('500000');
		// Label was renamed in i18n extraction from 'Minimum balance threshold'
		// to 'Min balance alert' (cfo.control.minBalance in en.ts).
		await page.getByLabel('Min balance alert').fill('100000');
		const resp = await respPromise;
		expect(resp.status()).toBe(200);
		// The running-balance table renders rows with opening/closing columns.
		await expect(page.locator('.cf-table thead th', { hasText: 'Opening' })).toBeVisible();
	});

	test('a breaching period is named in text, not only coloured red (WCAG 1.4.1)', async ({
		page
	}) => {
		// The real response, with its first period forced into breach: what is
		// under test is how the table marks a breaching row, and whether the
		// seeded tenant's curve happens to cross a threshold is not.
		await page.route(
			(url) => url.pathname === '/api/analytics/cash_position',
			async (route) => {
				const res = await route.fetch();
				const body = (await res.json()) as CashPosition;
				if (body.periods.length === 0) {
					body.periods = [
						{
							period: '2026-01',
							period_start: '2026-01-01',
							period_end: '2026-01-31',
							opening: '100.00',
							outflow: '150.00',
							inflow: '0.00',
							closing: '-50.00',
							below_threshold: false,
							unconverted_count: 0
						}
					];
				}
				body.periods = body.periods.map((p, i) => ({ ...p, below_threshold: i === 0 }));
				const first = body.periods[0];
				body.breaches = [
					{
						period: first.period,
						period_start: first.period_start,
						period_end: first.period_end,
						closing: first.closing,
						shortfall: '1.00'
					}
				];
				await route.fulfill({ response: res, json: body });
			}
		);
		await page.goto('/cfo');

		// The table scrolls inside a focusable region; it is announced by name,
		// like every <DataTable>'s container, rather than as an anonymous stop.
		const region = page.getByRole('region', { name: 'Cash position' });
		await expect(region).toBeVisible({ timeout: 10_000 });

		const rows = region.locator('tbody tr');
		await expect(rows.first()).toHaveAttribute('data-breach', 'true');
		await expect(rows.first()).toContainText('Below minimum');
		// Only the breaching row says so.
		await expect(region.getByText('Below minimum')).toHaveCount(1);
	});

	test('Export CSV triggers a download', async ({ page }) => {
		const downloadPromise = page.waitForEvent('download');
		await page.getByTestId('export-csv').click();
		const download = await downloadPromise;
		expect(download.suggestedFilename()).toMatch(/^cashflow_forecast_.*\.csv$/);
	});
});

test.describe('/cfo RBAC', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	// Cash Flow lives inside the folded "Insights" sidebar group; it surfaces as
	// a section tab on the group's pages, not as its own sidebar row.
	test('ap_clerk cannot reach Cash Flow (no sidebar row, no section tab)', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/assistant'); // the clerk's Insights landing
		await expect(page.locator('aside.sidebar a.nav-item[href="/cfo"]')).toHaveCount(0);
		await expect(page.locator('a.section-tab[href="/cfo"]')).toHaveCount(0);
	});

	test('cfo opens Cash Flow via the Insights section tabs', async ({ page, tenantCfo }) => {
		await signInAndWait(page, tenantCfo);
		// The Insights group row lands on AI Assistant; Cash Flow is a tab there.
		await page.locator('aside.sidebar a.nav-item', { hasText: 'Insights' }).click();
		const tab = page.locator('a.section-tab[href="/cfo"]');
		await expect(tab).toBeVisible();
		await tab.click();
		await expect(page.getByRole('heading', { name: 'Cash Flow' })).toBeVisible();
	});
});
