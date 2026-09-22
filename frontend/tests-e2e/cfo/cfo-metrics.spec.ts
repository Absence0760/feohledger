import type { CfoAnalytics } from '$lib/types/analytics';

import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /cfo — "CFO metrics" section (`CfoMetrics.svelte`).
 *
 * `GET /api/analytics/cfo` computed DPO, cash conversion cycle, accruals,
 * supplier concentration, fraud-rate trend, and rebate yield correctly, but
 * had no frontend surface at all until this component. Found by
 * exploratory persona-driven testing (CFO persona); filed as #236.
 *
 * The seeded tenant's data drives real numbers, so this spec asserts
 * structure (section renders, KPI cards populate, subsections render) rather
 * than exact figures, which the seed may evolve.
 */

test.describe('/cfo CFO-metrics section (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/cfo');
	});

	test('renders KPI cards and the accruals + concentration subsections', async ({ page }) => {
		const section = page.getByTestId('cfo-metrics-section');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByRole('heading', { name: 'CFO metrics' })).toBeVisible();

		// Four KPI cards: DPO, cash conversion cycle, AP balance, rebate yield.
		await expect(section.locator('.kpi')).toHaveCount(4);
		await expect(section.locator('.kpi', { hasText: 'Days payable outstanding' })).toBeVisible();
		await expect(section.locator('.kpi', { hasText: 'Cash conversion cycle' })).toBeVisible();
		await expect(section.locator('.kpi', { hasText: 'Accounts payable balance' })).toBeVisible();
		await expect(section.locator('.kpi', { hasText: 'Card rebate yield' })).toBeVisible();

		await expect(section.locator('h3', { hasText: 'Accruals' })).toBeVisible();
		await expect(section.locator('.cfm-stat', { hasText: 'Open POs' })).toBeVisible();
		await expect(section.locator('.cfm-stat', { hasText: 'Total accrual' })).toBeVisible();

		await expect(section.locator('h3', { hasText: 'Supplier concentration' })).toBeVisible();
		await expect(section.locator('.cfm-stat', { hasText: 'Top 10 vendors' })).toBeVisible();
	});

	test('shows the accruals one line per currency, never a sum across them', async ({ page }) => {
		// The card used to render the flat accruals — a EUR purchase order added
		// to a USD one — under the org's symbol. It now renders
		// `accruals.by_currency`, each currency netted within itself, and POs that
		// record no currency on their own bare line (decisions §197). The real
		// response is fetched and only its accruals replaced, so the rest of the
		// section stays the backend's.
		const byCurrency: CfoAnalytics['accruals']['by_currency'] = [
			{
				currency: 'EUR',
				open_po_amount: '3000.00',
				received_amount: '1500.00',
				unposted_invoice_amount: '1000.00',
				total_accrual: '3500.00'
			},
			{
				currency: 'USD',
				open_po_amount: '5000.00',
				received_amount: '0.00',
				unposted_invoice_amount: '1000.00',
				total_accrual: '4000.00'
			},
			{
				currency: null,
				open_po_amount: '700.00',
				received_amount: '0.00',
				unposted_invoice_amount: '0.00',
				total_accrual: '700.00'
			}
		];
		await page.route(
			(url) => url.pathname === '/api/analytics/cfo',
			async (route) => {
				const response = await route.fetch();
				const body = (await response.json()) as CfoAnalytics;
				body.accruals = {
					open_po_amount: '8700.00',
					received_amount: '1500.00',
					unposted_invoice_amount: '2000.00',
					total_accrual: '8200.00',
					by_currency: byCurrency
				};
				await route.fulfill({ response, json: body });
			}
		);
		await page.reload();

		const accruals = page.getByTestId('cfm-accruals');
		const openPo = accruals.locator('.cfm-stat', { hasText: 'Open POs' }).locator('.mbc-line');
		await expect(openPo).toHaveCount(3);
		await expect(openPo.nth(0)).toContainText(/€|EUR/);
		await expect(openPo.nth(0)).toContainText(/3[,.]?000/);
		await expect(openPo.nth(1)).toContainText('$');
		// No currency recorded: no symbol at all, and not added to either.
		await expect(openPo.nth(2)).toHaveText(/^\s*700\s*$/);

		const total = accruals.locator('.cfm-stat', { hasText: 'Total accrual' }).locator('.mbc-line');
		await expect(total.nth(0)).toContainText(/3[,.]?500/);
		await expect(total.nth(1)).toContainText(/4[,.]?000/);
		// The naive cross-currency 8,200 is never shown.
		await expect(accruals).not.toContainText(/8[,.]?200/);
		await expect(accruals.getByTestId('accruals-no-currency')).toBeVisible();
	});

	test('re-fetches CFO metrics when the horizon control changes', async ({ page }) => {
		const section = page.getByTestId('cfo-metrics-section');
		await expect(section).toBeVisible({ timeout: 10_000 });

		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/analytics/cfo') && r.url().includes('period_days=180')
		);
		await page.locator('.seg-btn', { hasText: '180d' }).click();
		const resp = await respPromise;
		expect(resp.status()).toBe(200);
	});
});

test.describe('/cfo CFO-metrics section RBAC', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk cannot reach the CFO-metrics section', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/assistant'); // the clerk's Insights landing
		await expect(page.locator('aside.sidebar a.nav-item[href="/cfo"]')).toHaveCount(0);
	});

	test('cfo sees the CFO-metrics section', async ({ page, tenantCfo }) => {
		await signInAndWait(page, tenantCfo);
		await page.locator('aside.sidebar a.nav-item', { hasText: 'Insights' }).click();
		const tab = page.locator('a.section-tab[href="/cfo"]');
		await expect(tab).toBeVisible();
		await tab.click();
		await expect(page.getByTestId('cfo-metrics-section')).toBeVisible({ timeout: 10_000 });
	});
});
