import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * Invoice detail modal — opens when the row's invoice-number link
 * ("Edit invoice …" RowLink) is clicked. The modal renders three
 * things we care about end-to-end:
 *   1. A heading with the invoice number ("Edit Invoice — INV-2024-…")
 *   2. The Line Items section (or its empty placeholder)
 *   3. The Activity / audit-log section (visible because seed creates
 *      audit entries during invoice creation + status transitions)
 *
 * The PDF preview pane is best-effort — seeded invoices may or may not
 * have an attached file_url. Those assertions go in a separate spec
 * once we seed an invoice with a known PDF.
 */

test.describe('/invoices invoice detail modal', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
		// Wait for the table to actually populate before opening a row.
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});

	test('Edit opens the modal with the invoice number in the heading', async ({
		page
	}) => {
		// Read the invoice number from the first row's "Invoice #"
		// column. Columns: 0 = checkbox, 1 = invoice number, 2 = vendor,
		// ..., last = actions.
		const firstInvoiceNumber = (
			await page.locator('table tbody tr').first().locator('td').nth(1).textContent()
		)?.trim();
		expect(firstInvoiceNumber).toBeTruthy();

		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();

		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();
		await expect(modal.locator('header h2')).toContainText(firstInvoiceNumber!);
	});

	test('modal shows the Line Items section', async ({ page }) => {
		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();

		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();

		// The line-items-section is always rendered; its content is
		// either a table or a "no line items" placeholder. The section
		// title "Line Items" is the stable contract.
		await expect(modal.locator('.line-items-title')).toHaveText('Line Items');
	});

	test('modal shows the Activity audit timeline once loaded', async ({ page }) => {
		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();

		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();

		// The .activity-section renders only when audit entries exist
		// for the invoice (loadAuditLog populates it). Seed creates at
		// least one audit row per invoice (creation event).
		await expect(modal.locator('.activity-section .activity-title')).toHaveText(
			'Activity',
			{ timeout: 10_000 }
		);
	});

	test('modal close button dismisses', async ({ page }) => {
		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();
	});

	test('open modal locks background page scroll; close restores it', async ({ page }) => {
		// Regression: scrolling over the PDF pane (which has no internal scroll)
		// used to bleed through to the invoice grid behind the backdrop. The modal
		// now freezes `body` overflow while open and restores it on close.
		const bodyOverflow = () => page.evaluate(() => document.body.style.overflow);

		const before = await bodyOverflow();

		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();
		await expect.poll(bodyOverflow).toBe('hidden');

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();
		await expect.poll(bodyOverflow).toBe(before);
	});

	test('Activity timeline renders the per-field before/after change diff', async ({
		page
	}) => {
		// SOX change history: editing an invoice writes an `invoice.edited`
		// audit row carrying details.changes = {field: {old, new}}. The modal's
		// Activity tab renders that as a struck-through old → new diff. Drive the
		// edit through the API (the UI form-submit path is covered in edit.spec),
		// then reopen the modal and assert the diff is visible.
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as {
			items: Array<{ id: string; invoice_number: string; description: string | null; status: string }>;
		};
		const immutable = new Set([
			'sending_to_erp',
			'sent_to_erp',
			'posted_in_erp',
			'payment_scheduled',
			'paid',
			'done'
		]);
		const target = listed.items.find((i) => !immutable.has(i.status));
		expect(target, 'no editable invoice in the seed').toBeTruthy();
		const original = target!.description ?? '';
		const next = `e2e-diff-${Date.now()}`;

		try {
			const patch = await page.request.fetch(`${API_BASE}/api/invoices/${target!.id}`, {
				method: 'PATCH',
				headers: { ...headers, 'Content-Type': 'application/json' },
				data: JSON.stringify({ description: next })
			});
			expect(patch.status()).toBe(200);

			// Reload the list, open the edited invoice's modal.
			await page.reload();
			await page
				.locator('table tbody tr', { hasText: target!.invoice_number })
				.first()
				.getByRole('button', { name: 'Edit' })
				.click();
			const modal = page.locator('div.modal[role="dialog"]');
			await expect(modal).toBeVisible();

			// The change diff row shows the new value (and the struck-through old).
			const changes = modal.locator('.activity-changes');
			await expect(changes.first()).toBeVisible({ timeout: 10_000 });
			await expect(changes.locator('.change-new', { hasText: next })).toBeVisible();
		} finally {
			await page.request.fetch(`${API_BASE}/api/invoices/${target!.id}`, {
				method: 'PATCH',
				headers: { ...headers, 'Content-Type': 'application/json' },
				data: JSON.stringify({ description: original })
			});
		}
	});
});

/**
 * The modal offers only the writes the server will take.
 *
 * - Status is read-only for every role. It was a `<select>` of all twelve
 *   statuses for anyone but a clerk, but `PATCH /api/invoices/{id}` does not
 *   accept `status`, so a pick was never saved — and because every gate in the
 *   modal reads it, picking e.g. Ready for Review conjured Approve/Reject on a
 *   `new` invoice (a guaranteed 409).
 * - A pure clerk gets no Save and no Submit: `PATCH` and `POST /{id}/complete`
 *   are both `require_roles(ADMIN, AP_MANAGER, CFO)`, so each was a 403.
 */
test.describe('/invoices detail modal — controls match the server', () => {
	const MARKER = `E2E-MODALGATE-${Date.now()}`;
	let invoiceId = '';

	test.beforeEach(async ({ page }) => {
		const res = await page.request.post(`${API_BASE}/api/invoices`, {
			headers: await authedTenantHeaders(page),
			data: { vendor: 'E2E Modal Gate Vendor', invoice_number: MARKER, amount: '15.00', currency: 'USD' }
		});
		expect(res.status()).toBe(201);
		invoiceId = ((await res.json()) as { id: string }).id;
	});

	test.afterEach(() => deleteInvoicesWhere(`invoice_number = '${MARKER}'`));

	test('status is shown read-only, never as a picker of every status', async ({ page }) => {
		await page.goto(`/invoices?id=${invoiceId}`);
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(MARKER);

		const status = modal.locator('[data-testid="invoice-modal-status"]');
		await expect(status).toBeDisabled();
		await expect(status).toHaveValue('New');
		await expect(modal.locator('select:has(option[value="paid"])')).toHaveCount(0);
		// The admin still edits the fields and advances the workflow.
		await expect(modal.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
		await expect(modal.getByRole('button', { name: 'Submit for Review' })).toBeVisible();
	});

	test('a clerk is offered neither Save nor Submit', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await page.goto(`/invoices?id=${invoiceId}`);
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(MARKER);
		// Positive anchor first, so the absence checks below can't pass against a
		// half-rendered footer.
		await expect(modal.getByRole('button', { name: 'Cancel', exact: true })).toBeVisible();

		await expect(modal.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
		await expect(modal.getByRole('button', { name: 'Submit for Review' })).toHaveCount(0);
	});
});
