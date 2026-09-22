import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';
import { REPORTING_CURRENCY, dashboardResponse, upcomingPaymentRow, type DashboardPatch } from './fixture';

/**
 * Dashboard — the KPI row's partial-conversion disclosures, and whose currency
 * the row is labelled in.
 *
 * **Two lines, because the three counts follow opposite rules**
 * (`docs/decisions.md` §200). `reporting.unconverted_count` comes from
 * `currency_conversion.invoice_reporting_amount_sql`, which counts an invoice
 * with no rate lock at FACE value — so Total Amount mixes currencies. The two
 * payment counts come from `payment_reporting_amount_sql`, which EXCLUDES such
 * a payment — so Paid and Pending are floors. One banner used to OR all three
 * and say "exclude … treat them as a floor", which was true of the payment
 * figures and false of the invoice one. Each line is pinned in both
 * directions here: present on its own count, absent on the other's.
 *
 * **The labels come from the payload.** Every KPI and chart figure is
 * denominated in the `reporting.reporting_currency` the response names. The
 * page used to label them from the `orgCurrency` store instead, which answered
 * `USD` whenever the org's settings resolved nothing — so a EUR-reporting
 * tenant whose code came from the backend's operator default read every KPI in
 * dollars.
 *
 * Stubbed through the shared `./fixture.ts` builder (typed
 * `satisfies DashboardData`), because none of these states is producible on
 * demand against a seeded tenant.
 */

const FACE_VALUE_LINE = 'unconverted-rollup-face-value';
const EXCLUDED_LINE = 'unconverted-rollup-excluded';

async function stubDashboard(page: Page, patch: DashboardPatch) {
	await page.route(
		(url) => url.pathname === '/api/dashboard',
		(route) => route.fulfill({ json: dashboardResponse(patch) })
	);
}

/** One KPI card's value, found by its exact label. */
function kpiValue(page: Page, label: string) {
	return page
		.locator('.kpi')
		.filter({ has: page.locator('.kpi-label', { hasText: new RegExp(`^${label}$`) }) })
		.locator('.kpi-value');
}

/** The KPI row settles when its money cards carry a figure — `data-kpi-state`
 *  is the card's own readiness signal, and the charts render beside it. */
async function awaitKpis(page: Page) {
	await expect(
		page.locator('.kpi', { hasText: 'Total Amount' })
	).toHaveAttribute('data-kpi-state', 'value', { timeout: 15_000 });
	await expect(page.locator('.charts-grid .chart-card').first()).toBeVisible();
}

test.describe('dashboard KPI row — partial-conversion disclosures', () => {
	test('an unconverted INVOICE is disclosed as face value on Total Amount, never as excluded', async ({
		page
	}) => {
		await stubDashboard(page, { totalFaceValue: 3 });
		await page.goto('/');
		await awaitKpis(page);

		const line = page.getByTestId(FACE_VALUE_LINE);
		await expect(line).toBeVisible();
		await expect(line).toHaveAttribute('role', 'alert');
		// Names the KPI it qualifies, the count, the rule, and the currency.
		await expect(line).toContainText('Total Amount (All Invoices)');
		await expect(line).toContainText('3 invoices');
		await expect(line).toContainText('counted at face value');
		await expect(line).toContainText(REPORTING_CURRENCY);
		// The misstatement this pins: that figure is not a floor.
		await expect(line).not.toContainText(/exclude|floor/i);

		await expect(page.getByTestId(EXCLUDED_LINE)).toHaveCount(0);
	});

	test('an unconverted PAYMENT is disclosed as excluded, naming only the KPI that lost it', async ({
		page
	}) => {
		await stubDashboard(page, { paidExcluded: 2 });
		await page.goto('/');
		await awaitKpis(page);

		const line = page.getByTestId(EXCLUDED_LINE);
		await expect(line).toBeVisible();
		await expect(line).toHaveAttribute('role', 'alert');
		await expect(line).toContainText('Excluded from Paid:');
		await expect(line).toContainText('2 payments');
		await expect(line).toContainText(REPORTING_CURRENCY);
		// Pending lost nothing, so it is not blamed.
		await expect(line).not.toContainText('Pending');
		await expect(line).not.toContainText('face value,');

		await expect(page.getByTestId(FACE_VALUE_LINE)).toHaveCount(0);
	});

	test('both payment KPIs losing rows are named together, with one count', async ({ page }) => {
		await stubDashboard(page, { paidExcluded: 2, pendingExcluded: 1 });
		await page.goto('/');
		await awaitKpis(page);

		const line = page.getByTestId(EXCLUDED_LINE);
		await expect(line).toContainText('Excluded from Paid and Pending:');
		// Completed and pending are disjoint statuses, so the counts add.
		await expect(line).toContainText('3 payments');
	});

	test('both rules firing at once give two lines, each saying its own thing', async ({ page }) => {
		await stubDashboard(page, { totalFaceValue: 1, pendingExcluded: 4 });
		await page.goto('/');
		await awaitKpis(page);

		await expect(page.getByTestId(FACE_VALUE_LINE)).toContainText('1 invoice with');
		await expect(page.getByTestId(EXCLUDED_LINE)).toContainText('Excluded from Pending:');
		await expect(page.getByTestId(EXCLUDED_LINE)).toContainText('4 payments');
	});

	test('nothing unconverted — neither line renders', async ({ page }) => {
		await stubDashboard(page, {});
		await page.goto('/');
		await awaitKpis(page);

		// Positive readiness above (the KPI figure landed), so these absences
		// cannot pass against a page that has not rendered.
		await expect(page.getByTestId(FACE_VALUE_LINE)).toHaveCount(0);
		await expect(page.getByTestId(EXCLUDED_LINE)).toHaveCount(0);
	});
});

test.describe('dashboard KPI row — labelled by the payload, not the org store', () => {
	test('the KPIs wear the reporting currency the RESPONSE names, even when the org settings resolve nothing', async ({
		page
	}) => {
		// Settings with no currency rung at all: the store resolves `null`.
		// Before the fix it answered `USD`, and the dashboard labelled every KPI
		// from the store — `$6,000` over a figure the backend summed in euros.
		await page.route(
			(url) => url.pathname === '/api/organization',
			(route) => route.fulfill({ json: { settings: {} } })
		);
		await stubDashboard(page, { reportingCurrency: 'EUR' });
		await page.goto('/');
		await awaitKpis(page);

		const total = kpiValue(page, 'Total Amount \\(All Invoices\\)');
		await expect(total).toContainText('€');
		await expect(total).toContainText('6,000');
		await expect(total).not.toContainText('$');
		await expect(kpiValue(page, 'Paid')).toContainText('€');
		await expect(kpiValue(page, 'Pending')).toContainText('€');
	});

	test('an upcoming payment is labelled in its OWN invoice currency, and bare when it has none', async ({
		page
	}) => {
		// These rows are per-invoice FACE amounts, not reporting figures, so
		// neither the page's reporting currency nor the org's is their label.
		await stubDashboard(page, {
			reportingCurrency: 'USD',
			upcomingPayments: [
				upcomingPaymentRow('INV-9001', 48000, 'JPY'),
				upcomingPaymentRow('INV-9002', 250, null)
			]
		});
		await page.goto('/');
		await awaitKpis(page);

		const rows = page.locator('.upcoming-row');
		await expect(rows).toHaveCount(2);
		const yen = rows.filter({ hasText: 'INV-9001' }).locator('.upcoming-amount');
		await expect(yen).toContainText('¥');
		await expect(yen).not.toContainText('$');
		const unknown = rows.filter({ hasText: 'INV-9002' }).locator('.upcoming-amount');
		await expect(unknown).toHaveText('250.00');
	});
});
