import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deleteVendorsWhere,
	expect,
	tenantBase,
	test
} from '../fixtures/helpers';

/**
 * A CSV invoice row whose number is the same payable as one already in the
 * ledger once formatting is ignored (`INV-1` vs `INV-001`, decisions §77) is a
 * row error naming that invoice — not a silent skip, and not a second payable
 * the duplicate gate then has to flag (`backend/app/services/csv_import.py`,
 * `backend/docs/csv-import.md` § Near-duplicate invoice numbers).
 *
 * The error is coded (`csv_invoice_number_matches_existing` + params), so
 * `ImportCsvModal` states it in the reader's language through
 * `api/codedRefusals.ts`. The real stack end to end: a first import lands the
 * invoice, the second is refused per row, and a `de` reader reads German.
 */

const MARK = 'CSVNORM';

test.describe('a near-duplicate CSV invoice number is reported, localized', () => {
	let slug: string;

	test.afterEach(() => {
		if (!slug) return;
		deleteInvoicesWhere(`invoice_number LIKE '${MARK}-%'`, slug);
		deleteVendorsWhere(`name LIKE '${MARK} %'`, slug);
	});

	test('a de reader sees the row refused in German, naming the existing invoice', async ({
		page,
		tenantSlug
	}) => {
		slug = tenantSlug;
		const suffix = Date.now().toString(36).toUpperCase();
		const existing = `${MARK}-${suffix}-001`;
		const nearMatch = `${MARK}-${suffix}-1`;
		const vendor = `${MARK} Vendor ${suffix}`;

		// The invoice already in the ledger, linked to the vendor the row names.
		const seed = await page.request.post(`${API_BASE}/api/invoices/import-csv`, {
			headers: await authedTenantHeaders(page, tenantSlug),
			multipart: {
				file: {
					name: 'seed.csv',
					mimeType: 'text/csv',
					buffer: Buffer.from(
						`invoice_number,vendor_name,amount,status\n${existing},${vendor},10.00,new\n`
					)
				}
			}
		});
		expect(seed.ok(), await seed.text()).toBe(true);
		expect((await seed.json()).imported).toBe(1);

		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto(`${tenantBase(tenantSlug)}/invoices`);
		await page.getByRole('button', { name: 'CSV importieren' }).click();
		const modal = page.getByRole('dialog', { name: 'CSV importieren' });
		await expect(modal).toBeVisible();
		await modal.locator('input[type="file"]').setInputFiles({
			name: 'invoices.csv',
			mimeType: 'text/csv',
			buffer: Buffer.from(`invoice_number,vendor_name,amount,status\n${nearMatch},${vendor},10.00,new\n`)
		});

		const imported = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname === '/api/invoices/import-csv' &&
				r.request().method() === 'POST'
		);
		await modal.getByRole('button', { name: 'Importieren' }).click();
		const body = (await (await imported).json()) as {
			imported: number;
			errors: { row: number; code?: string; params?: Record<string, unknown>; message: string }[];
		};
		// The wire contract: not imported, the code + params beside the row, the
		// English kept as the fallback.
		expect(body.imported).toBe(0);
		expect(body.errors).toHaveLength(1);
		expect(body.errors[0].code).toBe('csv_invoice_number_matches_existing');
		expect(body.errors[0].params?.existing_invoice_number).toBe(existing);

		const row = modal.locator('.error-list li');
		await expect(row).toHaveCount(1);
		await expect(row).toContainText(existing);
		await expect(row).toContainText(nearMatch);
		await expect(row).toContainText('wurde daher nicht importiert');
		await expect(row).not.toContainText('once formatting is ignored');
	});
});
