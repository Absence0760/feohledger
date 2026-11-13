import { deleteInvoicesWhere, deleteVendorsWhere, expect, test } from '../fixtures/helpers';

/**
 * /invoices — Day-0 CSV import. `POST /api/invoices/import-csv` is
 * skip-and-report and auto-creates unknown vendors as unverified stubs
 * (see backend/docs/csv-import.md). This exercises the `ImportCsvModal`
 * UI wired into the invoices page — file pick, upload, row-level result
 * rendering, list refresh — not the importer's own parsing/validation
 * rules (covered by backend pytest).
 */

test.describe('/invoices — Import CSV', () => {
	const marker = `E2E-INV-IMPORT-${Date.now()}`;

	test.afterEach(async () => {
		deleteInvoicesWhere(`invoice_number LIKE '${marker}%'`);
		// The importer auto-creates an unverified vendor stub per unknown
		// name; `deleteVendorsWhere` owns whatever the app hung off it.
		deleteVendorsWhere(`name LIKE '${marker}%'`);
	});

	test('imports a CSV, reports the row-level result (skip-and-report), and the invoice lands as done', async ({ page }) => {
		await page.goto('/invoices');

		await page.getByRole('button', { name: 'Import CSV' }).click();
		const modal = page.getByRole('dialog', { name: 'Import CSV' });
		await expect(modal).toBeVisible();
		await expect(modal.getByText(/invoice_number, amount/)).toBeVisible();

		const vendorName = `${marker} Vendor`;
		const goodInvoiceNumber = `${marker}-001`;
		const badInvoiceNumber = `${marker}-002`;
		// Row 1: importable historical invoice (status=done). Row 2: an
		// in-flight pipeline status (`approved`) is rejected per row —
		// bypassing the workflow engine would drop a payable invoice into
		// the queue with no audit trail (issue #174, per the backend doc).
		const csv =
			'invoice_number,vendor_name,amount,currency,status\n' +
			`${goodInvoiceNumber},${vendorName},1250.00,USD,done\n` +
			`${badInvoiceNumber},${vendorName},500.00,USD,approved\n`;

		await modal.locator('input[type="file"]').setInputFiles({
			name: 'invoices.csv',
			mimeType: 'text/csv',
			buffer: Buffer.from(csv)
		});

		const importButton = modal.getByRole('button', { name: 'Import' });
		await expect(importButton).toBeEnabled();
		await importButton.click();

		await expect(modal.getByText('1 imported, 1 skipped')).toBeVisible({ timeout: 15_000 });
		await expect(modal.locator('.error-list li')).toHaveCount(1);
		await expect(modal.locator('.error-list li')).toContainText(/status/i);

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).not.toBeVisible();

		// List refetched without a manual reload — search narrows to the
		// imported row.
		await page.getByRole('textbox', { name: /search invoices/i }).fill(goodInvoiceNumber);
		await expect(page.getByText(goodInvoiceNumber)).toBeVisible({ timeout: 15_000 });
		await expect(page.getByText(badInvoiceNumber)).toHaveCount(0);
	});
});
