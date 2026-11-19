import { expect, test } from '../fixtures/helpers';

/**
 * /tax — 1099 vendor reporting dashboard.
 *
 * Reads GET /api/tax/1099-report?year=. Default storage state signs the
 * worker's admin in, so the page loads directly.
 *
 * Report shape note: the backend returns *every* vendor (outer-joined to
 * payments), so the row set is the tenant's vendor list and changing the
 * year re-aggregates each vendor's YTD/payment-count rather than adding
 * or removing rows. The table is therefore only ever empty when the
 * tenant has no vendors; the "no match" empty state is reachable via a
 * search/filter that nothing satisfies. We assert structure + the
 * year-scoped request + the search empty-state, not exact tallies (the
 * lean e2e seed leaves is_1099_eligible=False, so reportable counts can
 * legitimately be zero).
 */

test.describe('/tax (admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/tax');
	});

	test('renders the 1099 surface — KPIs, year selector, filters, table', async ({ page }) => {
		await expect(page.getByRole('heading', { name: '1099 Reporting' })).toBeVisible();

		// KPI summary row (4 cards) populated from the report summary.
		await expect(page.locator('.kpi').first()).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.kpi')).toHaveCount(4);

		// Year selector defaults to the current calendar year.
		const yearSelect = page.getByLabel('Tax year');
		await expect(yearSelect).toBeVisible();
		await expect(yearSelect).toHaveValue(String(new Date().getFullYear()));

		// Filter chips + the data table are present.
		await expect(page.locator('.filter-chip', { hasText: 'All' })).toBeVisible();
		await expect(page.locator('.filter-chip', { hasText: 'Missing W-9' })).toBeVisible();
		await expect(page.locator('.grid-container table')).toBeVisible();

		// Seeded tenant has vendors → at least one row, no empty placeholder.
		await expect(page.locator('.grid-container tbody tr').first()).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('td.empty')).toHaveCount(0);
	});

	test('switching the tax year re-requests the report for that year', async ({ page }) => {
		const respPromise = page.waitForResponse(
			(r) => r.url().includes('/api/tax/1099-report') && r.url().includes('year=2024')
		);
		await page.getByLabel('Tax year').selectOption('2024');
		const resp = await respPromise;
		expect(resp.ok()).toBeTruthy();
		// Vendor rows still render (year re-aggregates YTD per vendor).
		await expect(page.locator('.grid-container tbody tr').first()).toBeVisible({ timeout: 10_000 });
	});

	test('YTD cells render with a currency symbol, not a bare number', async ({ page }) => {
		// Target the reportable-YTD cell by class, not by position — the row now
		// ends with the card-excluded column beside it.
		const firstYtd = page.locator('.grid-container tbody tr').first().locator('td.ytd-reportable');
		await expect(firstYtd).toBeVisible({ timeout: 10_000 });
		// USD report → "$" + grouped/decimal formatting from Intl.NumberFormat
		// (e.g. "$0.00" or "$1,234.50"). A raw number would lack the symbol.
		await expect(firstYtd).toContainText(/\$[\d,]+\.\d{2}/);
	});

	test('money follows the report currency, not a hardcoded USD', async ({ page }) => {
		// The report response is now authoritative for the display currency (it
		// carries the org's reporting/home currency the totals are denominated
		// in). Patch it to a non-USD currency and assert the money follows. A
		// fresh page per test → no leak into the USD-default tests above.
		await page.route('**/api/tax/1099-report**', async (route) => {
			const resp = await route.fetch();
			const body = await resp.json();
			body.currency = 'EUR';
			await route.fulfill({ response: resp, json: body });
		});

		await page.goto('/tax');

		// The Total-Reportable KPI (4th card) and every per-vendor YTD cell now
		// render in EUR (€) from the report's currency — a hardcoded USD fallback
		// would show "$".
		const totalReportable = page.locator('.kpi').nth(3).locator('.kpi-value');
		await expect(totalReportable).toBeVisible({ timeout: 10_000 });
		await expect(totalReportable).toContainText('€');
		await expect(totalReportable).not.toContainText('$');

		const firstYtd = page.locator('.grid-container tbody tr').first().locator('td.ytd-reportable');
		await expect(firstYtd).toBeVisible({ timeout: 10_000 });
		await expect(firstYtd).toContainText('€');
		await expect(firstYtd).not.toContainText('$');
	});

	/**
	 * Card-rail spend is EXCLUDED from `ytd_paid` (the card settlement entity
	 * files it on a 1099-K), and the report surfaces it as `card_paid` /
	 * `card_payment_count` per vendor + `total_card_excluded` on the summary so
	 * an operator can reconcile our filing against the processor's. The lean
	 * e2e seed has no card-rail payments, so these two tests patch the response
	 * — the same route-interception pattern the currency test above uses — to
	 * pin the rendering deterministically rather than depend on seed drift.
	 */
	test.describe('card-payment exclusion (1099-K reconciliation)', () => {
		async function loadWithCardSpend(page: import('@playwright/test').Page) {
			await page.route('**/api/tax/1099-report**', async (route) => {
				const resp = await route.fetch();
				const body = await resp.json();
				body.total_card_excluded = '9100.00';
				// Give exactly the first vendor a card leg; the rest stay at zero
				// so the "—" placeholder path is exercised in the same table.
				body.rows = body.rows.map((r: Record<string, unknown>, i: number) =>
					i === 0
						? { ...r, card_paid: '1500.00', card_payment_count: 2 }
						: { ...r, card_paid: '0.00', card_payment_count: 0 }
				);
				await route.fulfill({ response: resp, json: body });
			});
			await page.goto('/tax');
		}

		test('the reportable KPI carries the excluded-card amount as a secondary line', async ({
			page
		}) => {
			await loadWithCardSpend(page);

			const totalReportable = page.locator('.kpi').nth(3);
			await expect(totalReportable).toBeVisible({ timeout: 10_000 });
			// The filed figure stays the headline value…
			await expect(totalReportable.locator('.kpi-value')).toBeVisible();
			// …and the excluded money is a labelled sub-line, not a second headline.
			const sub = totalReportable.locator('.kpi-sub');
			await expect(sub).toBeVisible();
			await expect(sub).toContainText('$9,100.00');
			await expect(sub).toContainText('1099-K');
		});

		test('per-vendor card spend renders in its own labelled column', async ({ page }) => {
			await loadWithCardSpend(page);

			// The column exists and is named so the figure can't be read as
			// reportable income.
			await expect(
				page.locator('.grid-container thead th', { hasText: 'Card excluded (1099-K)' })
			).toBeVisible({ timeout: 10_000 });

			const firstRow = page.locator('.grid-container tbody tr').first();
			const cardCell = firstRow.locator('td.card-excluded');
			await expect(cardCell).toContainText('$1,500.00');
			await expect(cardCell).toContainText('2 card payments');
			// The reportable YTD cell is a different, independent figure.
			await expect(firstRow.locator('td.ytd-reportable')).toContainText(/\$[\d,]+\.\d{2}/);

			// The "Card excluded" filter chip narrows to just the vendors with a
			// card leg — the reconciliation worklist.
			await page.locator('.filter-chip', { hasText: 'Card excluded' }).click();
			await expect(page.locator('.grid-container tbody tr')).toHaveCount(1);
			await expect(page.locator('.grid-container tbody tr').first()).toContainText('$1,500.00');
		});
	});

	test('a no-match search shows the empty state', async ({ page }) => {
		await expect(page.locator('.grid-container tbody tr').first()).toBeVisible({ timeout: 10_000 });
		await page.getByLabel('Search vendors').fill('zzz-no-such-vendor-zzz');
		// Client-side filter → centred empty row with the no-match copy.
		await expect(page.locator('td.empty')).toBeVisible();
		await expect(page.locator('td.empty')).toContainText('No vendors match this filter.');
	});

	test('year, chip and search round-trip through the URL', async ({ page }) => {
		const year = String(new Date().getFullYear() - 1);
		await expect(page.locator('.grid-container tbody tr').first()).toBeVisible({ timeout: 10_000 });

		await page.getByLabel('Tax year').selectOption(year);
		await page.locator('.filter-chip', { hasText: 'Missing W-9' }).click();
		await page.getByLabel('Search vendors').fill('acme');
		await expect(page).toHaveURL(new RegExp(`[?&]year=${year}\\b`));
		await expect(page).toHaveURL(/[?&]filter=missing_w9\b/);
		await expect(page).toHaveURL(/[?&]search=acme\b/);

		const reloaded = page.waitForResponse(
			(r) => r.url().includes('/api/tax/1099-report') && r.url().includes(`year=${year}`)
		);
		await page.reload();
		await reloaded;
		await expect(page.getByLabel('Tax year')).toHaveValue(year);
		await expect(page.locator('.filter-chip', { hasText: 'Missing W-9' })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
		await expect(page.getByLabel('Search vendors')).toHaveValue('acme');
	});
});
