import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Invoice PDF file management — replace / delete an already-attached file
 * from the invoice detail modal (the companion to the manual-entry
 * attach-only `POST /api/invoices/{id}/file` covered in
 * `create-manual.spec.ts`).
 *
 * Backend contract (`backend/app/api/invoices.py`):
 *   - `PUT /api/invoices/{id}/file`    — replace an existing file. 200 on
 *     success, 404 if there's no file to replace, 409 once the invoice is
 *     `done`.
 *   - `DELETE /api/invoices/{id}/file` — delete the file. 200 on success,
 *     404 if there's no file, 409 once the invoice is `done`.
 *   Both take the entry roles, ap_clerk included — a clerk only inside the
 *   entry window (`backend/app/api/invoice_entry.py`).
 *
 * Frontend contract (`InvoiceModal.svelte`): a file-management toolbar,
 * hidden once the invoice is `done`, and for an entry-only ap_clerk once it
 * leaves the entry window (submitted, or ever approved).
 *   - No file yet  → one "Upload File" button (aria-label "Upload invoice
 *     file"). Selecting a file attaches it.
 *   - Has a file   → "Replace" (picks + swaps the file) + a two-click
 *     armed-confirm "Delete File" → "Confirm Delete" button.
 *   - Every action leaves the modal open and its file pane refreshed live.
 *
 * Roadmap item "Invoice PDF Management (Upload / Replace / Delete from
 * Invoice Detail)" in docs/roadmap.md § Priority 1.
 */

async function createManualInvoice(page: import('@playwright/test').Page, invoiceNumber: string) {
	await page.goto('/invoices');
	await page.getByRole('button', { name: 'Create Invoice' }).click();
	const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
	await expect(modal).toBeVisible();

	await modal.locator('label', { hasText: 'Vendor' }).locator('input').fill('E2E File Mgmt Vendor');
	await modal.locator('label', { hasText: 'Invoice #' }).locator('input').fill(invoiceNumber);
	await modal.locator('label', { hasText: 'Amount' }).locator('input').fill('123.45');
	await modal.getByRole('button', { name: 'Create' }).click();
	await expect(modal).not.toBeVisible({ timeout: 10_000 });
}

async function openInvoiceDetail(page: import('@playwright/test').Page, invoiceNumber: string) {
	const row = page.locator('table tbody tr', { hasText: invoiceNumber });
	await expect(row).toBeVisible({ timeout: 10_000 });
	await row.getByRole('button', { name: 'Edit' }).click();
	const detail = page.locator('div.modal[role="dialog"]', { hasText: invoiceNumber });
	await expect(detail).toBeVisible();
	return detail;
}

test.describe('/invoices — Invoice detail file management', () => {
	test('upload -> replace -> delete lifecycle updates the toolbar and Activity timeline', async ({
		page
	}) => {
		const uniqueNumber = `E2E-FILEMGMT-${Date.now()}`;
		await createManualInvoice(page, uniqueNumber);
		const detail = await openInvoiceDetail(page, uniqueNumber);

		// (a) No file yet: "Upload File" is the only file-management affordance.
		const uploadBtn = detail.getByRole('button', { name: 'Upload invoice file' });
		await expect(uploadBtn).toBeVisible();
		await expect(uploadBtn).toHaveText('Upload File');
		await expect(detail.getByRole('button', { name: 'Replace' })).toHaveCount(0);
		await expect(detail.getByRole('button', { name: 'Delete File' })).toHaveCount(0);

		await detail
			.locator('input[type="file"]')
			.first()
			.setInputFiles({
				name: 'first.pdf',
				mimeType: 'application/pdf',
				buffer: Buffer.from('%PDF-1.4 first upload')
			});

		// Upload succeeded: toolbar swaps to Replace + Delete File, no error
		// toast, modal stays open.
		await expect(detail.getByRole('button', { name: 'Replace' })).toBeVisible({ timeout: 10_000 });
		await expect(detail.getByRole('button', { name: 'Delete File' })).toBeVisible();
		await expect(detail.getByRole('button', { name: 'Upload invoice file' })).toHaveCount(0);
		await expect(page.locator('.toast.error')).toHaveCount(0);
		await expect(detail).toBeVisible();

		// (b) Replace with a different file.
		await detail
			.locator('input[type="file"]')
			.first()
			.setInputFiles({
				name: 'second.pdf',
				mimeType: 'application/pdf',
				buffer: Buffer.from('%PDF-1.4 second upload, replacing the first')
			});

		await expect(page.locator('.toast.error')).toHaveCount(0);
		await expect(detail).toBeVisible();
		await expect(detail.locator('.activity-action', { hasText: 'File replaced' })).toBeVisible({
			timeout: 5_000
		});
		// Still in the "has a file" state — replace didn't clear it.
		await expect(detail.getByRole('button', { name: 'Replace' })).toBeVisible();
		await expect(detail.getByRole('button', { name: 'Delete File' })).toBeVisible();

		// (c) Delete is a two-click armed-confirm.
		const deleteBtn = detail.getByRole('button', { name: 'Delete File' });
		await deleteBtn.click();
		await expect(detail.getByRole('button', { name: 'Confirm Delete' })).toBeVisible();
		// Not yet deleted — the first click only arms the button.
		await expect(detail.getByRole('button', { name: 'Upload invoice file' })).toHaveCount(0);

		await detail.getByRole('button', { name: 'Confirm Delete' }).click();

		// File is gone: toolbar reverts to just "Upload File".
		await expect(detail.getByRole('button', { name: 'Upload invoice file' })).toBeVisible({
			timeout: 10_000
		});
		await expect(detail.getByRole('button', { name: 'Replace' })).toHaveCount(0);
		await expect(detail.getByRole('button', { name: 'Delete File' })).toHaveCount(0);
		await expect(detail.getByRole('button', { name: 'Confirm Delete' })).toHaveCount(0);
		await expect(page.locator('.toast.error')).toHaveCount(0);
		await expect(detail.locator('.activity-action', { hasText: 'File deleted' })).toBeVisible({
			timeout: 5_000
		});
	});

	// A clerk ENTERS invoices (`backend/app/api/invoice_entry.py`), so the file
	// routes take them — but only inside the entry window, which closes at
	// submit. The toolbar has to follow the same line: offered on a `new`
	// invoice, gone once it is `ready_for_review`.
	test('ap_clerk gets the file toolbar inside the entry window and loses it at submit', async ({
		page,
		tenantClerk
	}) => {
		const invoiceNumber = `E2E-FILEMGMT-CLERK-${Date.now()}`;
		const create = await page.request.post(`${API_BASE}/api/invoices`, {
			headers: await authedTenantHeaders(page),
			data: { vendor: 'E2E File Mgmt Vendor', invoice_number: invoiceNumber, amount: '12.00' }
		});
		expect(create.status()).toBe(201);
		const invoiceId = ((await create.json()) as { id: string }).id;

		try {
			await signInAndWait(page, tenantClerk);
			await page.goto(`/invoices?id=${invoiceId}`);
			const detail = page.locator('div.modal[role="dialog"]', { hasText: invoiceNumber });
			await expect(detail.getByRole('button', { name: 'Upload invoice file' })).toBeVisible({
				timeout: 10_000
			});

			tenantPsql(
				`UPDATE invoices SET status = 'ready_for_review' WHERE id = '${invoiceId}'`,
				currentTenantSlug()
			);
			await page.reload();
			// Positive anchor first, so the absence checks can't pass against a
			// half-rendered modal.
			await expect(detail.getByTestId('invoice-modal-status')).toHaveValue('Ready for Review', {
				timeout: 10_000
			});
			await expect(detail.getByRole('button', { name: 'Upload invoice file' })).toHaveCount(0);
			await expect(detail.getByRole('button', { name: 'Replace' })).toHaveCount(0);
			await expect(detail.getByRole('button', { name: 'Delete File' })).toHaveCount(0);
		} finally {
			deleteInvoicesWhere(`id = '${invoiceId}'`, currentTenantSlug());
		}
	});

	test('replace/delete are refused (409) once the invoice is done — bypassing the UI', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const invoiceNumber = `E2E-FILEMGMT-DONE-${Date.now()}`;

		// Create a fresh invoice directly (no UI needed for this probe).
		const create = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: { vendor: 'E2E Done Vendor', invoice_number: invoiceNumber, amount: '77.00' }
		});
		expect(create.status()).toBe(201);
		const created = (await create.json()) as { id: string };
		const invoiceId = created.id;

		// Attach a file so both replace/delete pass the "has a file" gate and
		// hit the status gate we're actually probing.
		const attach = await page.request.post(`${API_BASE}/api/invoices/${invoiceId}/file`, {
			headers,
			multipart: {
				file: { name: 'done.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 done') }
			}
		});
		expect(attach.status()).toBe(201);

		// Move the invoice straight to `done` via direct SQL — there's no API
		// path that lands a fresh invoice there without a full approval +
		// payment + ERP lifecycle, and this probe only cares about the file
		// endpoints' status gate.
		tenantPsql(`UPDATE invoices SET status = 'done' WHERE id = '${invoiceId}'`, currentTenantSlug());
		const status = tenantPsql(
			`SELECT status FROM invoices WHERE id = '${invoiceId}'`,
			currentTenantSlug()
		).trim();
		expect(status).toBe('done');

		const replace = await page.request.put(`${API_BASE}/api/invoices/${invoiceId}/file`, {
			headers,
			multipart: {
				file: {
					name: 'replacement.pdf',
					mimeType: 'application/pdf',
					buffer: Buffer.from('%PDF-1.4 replacement')
				}
			}
		});
		expect(replace.status()).toBe(409);

		const del = await page.request.delete(`${API_BASE}/api/invoices/${invoiceId}/file`, { headers });
		expect(del.status()).toBe(409);
	});
});
