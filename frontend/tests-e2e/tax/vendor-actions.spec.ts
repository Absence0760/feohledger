import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, deleteVendorsWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /tax — the 1099 admin WORKFLOW (not just the read-only report table):
 * per-vendor W-9 upload/edit, TIN verification, the 1099 PDF download, and
 * the "File 1099s" e-filing confirm-then-act flow. Complements
 * `dashboard.spec.ts`, which covers the report table itself.
 *
 * Setup mirrors `vendors/bank-change-fraud-controls.spec.ts`: a fresh vendor
 * + a `done` invoice (via the real CSV importer, exercising that endpoint
 * too) + a completed payment inserted directly (no payment-run UI needed to
 * get money "paid" for a 1099 test), then the vendor is flagged
 * `is_1099_eligible` via direct DB write — a human decision the UI itself
 * makes through `PATCH /api/tax/vendors/{id}/w9`, so a mock/API bypass here
 * is legitimate setup, not the behavior under test.
 */

interface VendorResp {
	id: string;
	name: string;
}

async function importCsvInvoice(
	page: import('@playwright/test').Page,
	vendorName: string,
	invoiceNumber: string,
	amount: string
) {
	const headers = await authedTenantHeaders(page);
	const csv = `invoice_number,vendor_name,amount,status\n${invoiceNumber},${vendorName},${amount},done`;
	const res = await page.request.post(`${API_BASE}/api/invoices/import-csv`, {
		headers,
		multipart: { file: { name: 'invoices.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) } }
	});
	expect(res.status(), 'import invoice CSV').toBe(200);
	const body = (await res.json()) as { imported: number; skipped: number };
	expect(body.imported, JSON.stringify(body)).toBe(1);
}

test.describe('/tax — vendor tax workflow (admin/ap_manager)', () => {
	let vendorId: string;
	let vendorName: string;

	test.beforeEach(async ({ page }) => {
		vendorName = `E2E 1099 Vendor ${Date.now()}`;
		const headers = await authedTenantHeaders(page);

		const vendorRes = await page.request.post(`${API_BASE}/api/vendors`, {
			headers,
			data: { name: vendorName }
		});
		expect(vendorRes.status(), 'create vendor').toBe(201);
		const vendor = (await vendorRes.json()) as VendorResp;
		vendorId = vendor.id;

		const invoiceNumber = `E2E-1099-${Date.now()}`;
		await importCsvInvoice(page, vendorName, invoiceNumber, '2400.00');

		// The imported invoice is the only one this run created for this
		// vendor, so it's safe to look it up by invoice_number for the
		// payment FK.
		const invRow = tenantPsql(
			`SELECT id FROM invoices WHERE invoice_number='${invoiceNumber}'`
		).trim();

		// A completed ACH payment dated this year — `build_1099_report`
		// buckets by `completed_at` year, and only `completed` payments +
		// non-card rails count toward the reportable YTD total.
		tenantPsql(
			`INSERT INTO payments (id, invoice_id, amount, method, status, completed_at, created_at, updated_at)
			 VALUES (gen_random_uuid(), '${invRow}', 2400.00, 'ach', 'completed', now(), now(), now())`
		);

		// Flag the vendor 1099-eligible — the human judgement call
		// `PATCH .../w9` makes; over the year's threshold ($2,000 from 2026) it becomes
		// "reportable" and shows up in the filing batch.
		tenantPsql(`UPDATE vendors SET is_1099_eligible=true WHERE id='${vendorId}'`);
	});

	test.afterEach(async () => {
		tenantPsql(`DELETE FROM payments WHERE invoice_id IN (SELECT id FROM invoices WHERE vendor_name='${vendorName}')`);
		deleteInvoicesWhere(`vendor_name='${vendorName}'`);
		tenantPsql(`DELETE FROM tax_1099_filings WHERE tax_year=${new Date().getFullYear()} AND idempotency_key LIKE '%${new Date().getFullYear()}%'`);
		// `deleteVendorsWhere` owns the vendor's child graph. Vendor create
		// runs synchronous sanctions screening by default
		// (`FEOH_VENDOR_SCREENING_ENABLED`), and that FK does not cascade, so
		// a bare vendor delete would 500 on it.
		deleteVendorsWhere(`id='${vendorId}'`);
	});

	test('uploads a W-9, verifies a TIN, and downloads the 1099 PDF from the vendor tax modal', async ({ page }) => {
		await page.goto('/tax');

		await expect(page.getByRole('cell', { name: vendorName })).toBeVisible({ timeout: 15_000 });

		const row = page.locator('tbody tr', { hasText: vendorName });
		// Reportable + over threshold + no W-9 → the row-flag styling and the
		// "Missing" W-9 chip should be showing before we fix it.
		await expect(row.getByText('Missing').first()).toBeVisible();

		await row.getByRole('button', { name: 'Manage' }).click();
		const modal = page.getByRole('dialog', { name: `Tax details — ${vendorName}` });
		await expect(modal).toBeVisible();

		// --- W-9 upload ---
		// `setInputFiles` sets the hidden `<input>` directly — never click the
		// "Choose file…" trigger button first, which would call the native
		// file-picker `.click()` and hang the test on an unhandled OS dialog.
		await modal.locator('input[type="file"]').setInputFiles({
			name: 'w9.pdf',
			mimeType: 'application/pdf',
			buffer: Buffer.from('%PDF-1.4 fake w9 for e2e\n%%EOF')
		});
		await modal.getByRole('button', { name: 'Upload W-9' }).click();
		// The status-row chip carries the received date; the section title
		// above it reads the same "W-9 on file" prefix with no date, so match
		// the parenthesized date to disambiguate.
		await expect(modal.getByText(/W-9 on file \(/)).toBeVisible({ timeout: 10_000 });

		// --- TIN verify ---
		const tinInput = modal.getByPlaceholder('XX-XXXXXXX');
		await tinInput.fill('12-3456789'); // structurally valid EIN (mock adapter)
		await modal.getByRole('button', { name: 'Verify TIN' }).click();
		// "TIN verified" also appears on the status-row chip, so scope to the
		// verdict paragraph the verify action itself renders.
		await expect(modal.locator('.tin-result')).toHaveText('TIN verified', { timeout: 10_000 });

		// --- 1099 PDF download ---
		const download = page.waitForEvent('download');
		await modal.getByRole('button', { name: /Download \d{4} 1099 PDF/ }).click();
		const dl = await download;
		expect(dl.suggestedFilename()).toMatch(/\.pdf$/);

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).not.toBeVisible();

		// The table row reflects the mutations without a page reload.
		await expect(row.getByText('On file')).toBeVisible();
		await expect(row.getByText('Verified')).toBeVisible();
	});

	test('files 1099s via the arm-then-confirm flow and shows the result', async ({ page }) => {
		await page.goto('/tax');
		await expect(page.getByRole('cell', { name: vendorName })).toBeVisible({ timeout: 15_000 });

		const fileButton = page.getByRole('button', { name: 'File 1099s' });
		await expect(fileButton).toBeEnabled();
		await fileButton.click();

		const modal = page.getByRole('dialog', { name: /File 1099s for \d{4}/ });
		await expect(modal).toBeVisible();
		await expect(modal.getByText(/eligible vendors will be filed/)).toBeVisible();
		await expect(modal.getByText(/cannot be undone/)).toBeVisible();

		// First click arms; the destructive action must not fire on one click.
		const submit = modal.getByRole('button', { name: /File \d+ 1099s for \d{4}/ });
		await submit.click();
		await expect(modal.getByRole('button', { name: 'Confirm — submit to the IRS' })).toBeVisible();

		await modal.getByRole('button', { name: 'Confirm — submit to the IRS' }).click();
		await expect(
			modal.getByText(/forms accepted|Already filed/)
		).toBeVisible({ timeout: 15_000 });
	});
});
