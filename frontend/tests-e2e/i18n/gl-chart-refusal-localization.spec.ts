import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deleteVendorsWhere,
	expect,
	tenantBase,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * The GL-chart refusal is stated in the reader's language.
 *
 * Every invoice GL write refuses a code that is not an active account of the
 * invoice's own chart (`backend/app/services/gl_chart.py`, decisions §194 /
 * §199). The refusal used to be a plain English `detail`, so a German operator
 * importing a CSV read English inside a German frame. It is now STRUCTURED —
 * `{code: 'gl_codes_outside_chart', foreign, retired, unknown, on_lines,
 * message}` — and `api/glChartRefusal.ts` states it from the code.
 *
 * Two halves, because they reach the reader by different roads:
 *
 *   1. A CSV import's per-row error (the real stack): the row error carries the
 *      structured body beside `row`, and `ImportCsvModal` localizes it.
 *   2. A write's 422 (any toast): localized once, at the transport, in
 *      `api.ts`. The recurring form can no longer TYPE a bad code (its GL field
 *      is a picker over the template's own chart), so the 422 is stubbed here —
 *      what is under test is the client's handling of the body, which the
 *      backend half of the contract pins in `test_gl_code_entity_chart.py`.
 */

const MARK = 'GLI18N';

test.describe('the GL-chart refusal is localized', () => {
	let slug: string;

	test.afterEach(() => {
		if (!slug) return;
		deleteInvoicesWhere(`invoice_number LIKE '${MARK}-%'`, slug);
		deleteVendorsWhere(`name LIKE '${MARK} %'`, slug);
		tenantPsql(`DELETE FROM gl_accounts WHERE code LIKE '${MARK}-%'`, slug);
	});

	test('a de reader sees a CSV row refused in German, not the server English', async ({
		page,
		tenantSlug
	}) => {
		slug = tenantSlug;
		const suffix = Date.now().toString(36);
		const headers = {
			...(await authedTenantHeaders(page, tenantSlug)),
			'Content-Type': 'application/json'
		};
		// One active shared account, so the tenant's chart is non-empty and a
		// code in no chart is refused as unknown (§199) rather than accepted.
		const acct = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers,
			data: { code: `${MARK}-OK-${suffix}`, name: 'Chart anchor' }
		});
		expect(acct.ok(), await acct.text()).toBe(true);
		const unknownCode = `${MARK}-NOPE-${suffix}`;

		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto(`${tenantBase(tenantSlug)}/invoices`);
		await page.getByRole('button', { name: 'CSV importieren' }).click();
		const modal = page.getByRole('dialog', { name: 'CSV importieren' });
		await expect(modal).toBeVisible();

		const csv =
			'invoice_number,vendor_name,amount,status,gl_account\n' +
			`${MARK}-001,${MARK} Vendor,10.00,new,${unknownCode}\n`;
		await modal.locator('input[type="file"]').setInputFiles({
			name: 'invoices.csv',
			mimeType: 'text/csv',
			buffer: Buffer.from(csv)
		});

		const imported = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname === '/api/invoices/import-csv' &&
				r.request().method() === 'POST'
		);
		await modal.getByRole('button', { name: 'Importieren' }).click();
		const body = (await (await imported).json()) as {
			errors: { row: number; code?: string; unknown?: string[]; message: string }[];
		};
		// The wire contract: the structured body beside the row, English kept as
		// the fallback.
		expect(body.errors).toHaveLength(1);
		expect(body.errors[0].code).toBe('gl_codes_outside_chart');
		expect(body.errors[0].unknown).toEqual([unknownCode]);
		expect(body.errors[0].message).toContain("not in this invoice's chart");

		const row = modal.locator('.error-list li');
		await expect(row).toHaveCount(1);
		await expect(row).toContainText(`'${unknownCode}'`);
		await expect(row).toContainText('nicht im Kontenplan dieser Rechnung');
		await expect(row).not.toContainText("not in this invoice's chart");
	});

	test("a refused write's toast is localized at the transport", async ({ page, tenantSlug }) => {
		slug = tenantSlug;
		await page.route(
			(url) => url.pathname === '/api/recurring',
			async (route) => {
				if (route.request().method() !== 'POST') return route.fallback();
				await route.fulfill({
					status: 422,
					json: {
						detail: {
							code: 'gl_codes_outside_chart',
							on_lines: false,
							foreign: ['6000'],
							retired: [],
							unknown: [],
							message:
								"GL account '6000' belongs to another entity's chart of accounts, not this invoice's."
						}
					}
				});
			}
		);

		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto(`${tenantBase(tenantSlug)}/recurring`);
		await page.getByRole('button', { name: '+ Neue Vorlage' }).click();
		const dialog = page.getByRole('dialog', { name: 'Neue wiederkehrende Vorlage' });
		await expect(dialog).toBeVisible();
		await dialog.getByLabel('Name').fill(`${MARK} toast`);
		await dialog.getByLabel('Startdatum').fill('2026-01-01');
		await dialog.getByRole('button', { name: 'Erstellen' }).click();

		const toast = page.locator('.toast-text', { hasText: "'6000'" });
		await expect(toast).toContainText('zum Kontenplan einer anderen Einheit');
		await expect(toast).not.toContainText("another entity's chart");
	});
});
