import type { ReportResult } from '$lib/types/reports';

import { expect, test } from '../fixtures/helpers';

/**
 * /reports — Custom (ad-hoc) Report Builder.
 *
 * The builder is driven entirely by `GET /api/reports/catalog` (data sources +
 * dimensions / measures / filters); nothing is hardcoded. A spec runs against
 * `POST /api/reports/run` → `ReportResult`, can be saved (`POST /api/reports`),
 * re-loaded, run from the saved list (`POST /api/reports/{id}/run`), and
 * exported (`GET /api/reports/{id}/export?format=csv|pdf`).
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (read is all roles; save/delete is
 * admin/ap_manager/cfo), so the page loads directly.
 *
 * The catalog is server-defined, so we pick fields by option INDEX rather than
 * by label — that also proves the UI is catalog-driven. The build/run/save
 * tests exercise the real backend; the export test route-stubs the catalog +
 * saved list + export endpoint so it stays backend-independent and asserts the
 * download wiring deterministically.
 *
 * Selectors are accessible name / data-testid — never brittle CSS/nth-child,
 * never waitForTimeout.
 */

test.describe('/reports (admin, real backend)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/reports');
	});

	test('renders the builder header + catalog-driven controls', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Report Builder' })).toBeVisible();

		// The data-source select is populated from the catalog.
		const source = page.getByLabel('Data source');
		await expect(source).toBeVisible({ timeout: 10_000 });
		await expect(source.locator('option')).not.toHaveCount(0);

		// The three spec editors render (driven by the selected source).
		await expect(page.getByTestId('dimension-editor')).toBeVisible();
		await expect(page.getByTestId('measure-editor')).toBeVisible();
		await expect(page.getByTestId('filter-editor')).toBeVisible();
	});

	test('build (dimension + measure) → run → result table renders', async ({ page }) => {
		await expect(page.getByLabel('Data source')).toBeVisible({ timeout: 10_000 });

		// Add the first available dimension + measure (by index — catalog-driven).
		await page.getByLabel('Add dimension').selectOption({ index: 1 });
		await page.getByLabel('Add measure').selectOption({ index: 1 });

		const run = page.getByTestId('run-report');
		await expect(run).toBeEnabled();
		await run.click();

		// The result table renders (rows or the empty-state row — either is valid).
		await expect(page.getByTestId('report-result')).toBeVisible({ timeout: 10_000 });
		await expect(page.getByTestId('report-result-caption')).toBeVisible();
	});

	test('save a report → it appears in the saved list → reload → run it', async ({ page }) => {
		await expect(page.getByLabel('Data source')).toBeVisible({ timeout: 10_000 });
		await page.getByLabel('Add dimension').selectOption({ index: 1 });
		await page.getByLabel('Add measure').selectOption({ index: 1 });

		const name = `E2E Report ${Date.now()}`;
		await page.getByRole('button', { name: 'Save report' }).click();
		const dialog = page.getByRole('dialog', { name: 'Save report' });
		await dialog.getByLabel(/Name/).fill(name);
		await dialog.getByRole('button', { name: 'Save report' }).click();

		// The saved report surfaces in the saved-reports section.
		const savedSection = page.getByTestId('saved-reports');
		await expect(savedSection.getByText(name)).toBeVisible({ timeout: 10_000 });

		// Reload — the saved report survives (server-persisted) and can be run.
		await page.reload();
		await expect(savedSection.getByText(name)).toBeVisible({ timeout: 10_000 });

		await savedSection.getByRole('button', { name: `Run report ${name}` }).click();
		await expect(page.getByTestId('report-result')).toBeVisible({ timeout: 10_000 });

		// Clean up so the list doesn't grow unbounded across runs.
		await savedSection.getByRole('button', { name: `Delete report ${name}` }).click();
		await savedSection.getByRole('button', { name: `Delete report ${name}` }).click();
		await expect(savedSection.getByText(name)).toHaveCount(0);
	});
});

test.describe('/reports export (stubbed — backend-independent)', () => {
	const CATALOG = {
		sources: [
			{
				key: 'invoices',
				label: 'Invoices',
				dimensions: [{ key: 'vendor_name', label: 'Vendor', type: 'string' }],
				measures: [{ key: 'amount', label: 'Amount', aggs: ['sum', 'avg'], type: 'money' }],
				filters: [
					{
						key: 'status',
						label: 'Status',
						type: 'enum',
						ops: ['eq', 'ne', 'in'],
						enumValues: ['approved', 'paid']
					}
				]
			}
		]
	};

	const SAVED = {
		reports: [
			{
				id: 'rep_e2e_stub',
				name: 'Stubbed Vendor Spend',
				description: 'stub row',
				data_source: 'invoices',
				dimensions: [{ key: 'vendor_name', grain: null }],
				measures: [{ key: 'amount', agg: 'sum' }],
				filters: [],
				sort: [],
				limit: null,
				created_by_user_id: null,
				created_at: '2026-06-01T00:00:00Z',
				updated_at: '2026-06-20T00:00:00Z'
			}
		]
	};

	test('exporting a saved report as CSV triggers a file download', async ({ page }) => {
		await page.route('**/api/reports/catalog', (route) =>
			route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CATALOG) })
		);
		// GET /api/reports (list) — must not clobber the export route below, so
		// match the exact list path only.
		await page.route(
			(url) => url.pathname === '/api/reports',
			(route) =>
				route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SAVED) })
		);
		await page.route('**/api/reports/*/export**', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'text/csv',
				headers: { 'content-disposition': 'attachment; filename="report.csv"' },
				body: '# FeohLedger — provenance header\nVendor,Sum of Amount\nAcme,12345.67\n'
			})
		);

		await page.goto('/reports');

		const savedSection = page.getByTestId('saved-reports');
		await expect(savedSection.getByText('Stubbed Vendor Spend')).toBeVisible({ timeout: 10_000 });

		const download = page.waitForEvent('download');
		await savedSection
			.getByRole('button', { name: 'Export Stubbed Vendor Spend as CSV' })
			.click();
		const dl = await download;
		expect(dl.suggestedFilename()).toMatch(/\.csv$/);
	});

	test("a money cell wears its own row's currency, one row per currency", async ({ page }) => {
		// The server never sums across currencies: a money aggregate is grouped
		// by the source's currency dimension and the column names it in
		// `currency_key` (decisions §228). The table must label each cell from
		// THAT row — USD 100 and EUR 50 are two figures, never "150".
		const RESULT = {
			columns: [
				{ key: 'vendor_name', label: 'Vendor', kind: 'dimension', type: 'string' },
				{ key: 'currency', label: 'Currency', kind: 'dimension', type: 'string' },
				{
					key: 'amount_sum',
					label: 'Sum of Amount',
					kind: 'measure',
					type: 'money',
					currency_key: 'currency'
				}
			],
			rows: [
				{ vendor_name: 'Acme', currency: 'USD', amount_sum: '100.00' },
				{ vendor_name: 'Acme', currency: 'EUR', amount_sum: '50.00' }
			],
			total_rows: 2,
			page: 1,
			page_size: 100
		} satisfies ReportResult;

		await page.route('**/api/reports/catalog', (route) =>
			route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CATALOG) })
		);
		await page.route(
			(url) => url.pathname === '/api/reports',
			(route) =>
				route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SAVED) })
		);
		await page.route(
			(url) => url.pathname === '/api/reports/run',
			(route) =>
				route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(RESULT) })
		);

		await page.goto('/reports');
		await expect(page.getByLabel('Data source')).toBeVisible({ timeout: 10_000 });
		await page.getByLabel('Add dimension').selectOption({ index: 1 });
		await page.getByLabel('Add measure').selectOption({ index: 1 });
		await page.getByTestId('run-report').click();

		const result = page.getByTestId('report-result');
		const usdRow = result.getByRole('row').filter({ hasText: 'USD' });
		const eurRow = result.getByRole('row').filter({ hasText: 'EUR' });
		await expect(usdRow.locator('.money')).toHaveText('$100.00');
		await expect(eurRow.locator('.money')).toHaveText('€50.00');
	});
});
