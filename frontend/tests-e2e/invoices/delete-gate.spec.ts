import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * `/invoices` row Delete must be offered only where the server will honour it.
 *
 * `DELETE /api/invoices/{id}` answers 409 for every status in
 * `backend/app/api/invoices.py::IMMUTABLE_STATUSES`
 * (sending_to_erp, sent_to_erp, posted_in_erp, payment_scheduled, paid, done).
 * The page used to gate the action on a page-local copy of that set holding
 * only three of the six, so a `posted_in_erp` / `payment_scheduled` / `paid`
 * row rendered a Delete the user could arm and confirm — and every confirm
 * bought a "Cannot delete invoice in this status" error toast. The gate now
 * reads the shared `IMMUTABLE_STATUSES` in `#lib/types/invoice`.
 */

async function createInvoice(
	page: import('@playwright/test').Page,
	invoiceNumber: string
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Delete Gate Vendor',
			invoice_number: invoiceNumber,
			amount: '12.34',
			currency: 'USD'
		}
	});
	expect(res.ok()).toBeTruthy();
	return ((await res.json()) as { id: string }).id;
}

/** Search the list down to one invoice number and return its row. */
async function rowFor(page: import('@playwright/test').Page, invoiceNumber: string) {
	const responsePromise = page.waitForResponse(
		(r) =>
			r.url().includes('/api/invoices?') &&
			r.url().includes(`search=${encodeURIComponent(invoiceNumber)}`) &&
			r.request().method() === 'GET'
	);
	await page.getByPlaceholder('Search invoices...').fill(invoiceNumber);
	await responsePromise;
	const row = page.locator('table tbody tr', { hasText: invoiceNumber }).first();
	await expect(row).toBeVisible();
	return row;
}

test.describe('/invoices — row Delete gate matches the server', () => {
	test('Delete is offered while the invoice is still mutable', async ({ page }) => {
		const number = `E2E-DELGATE-OK-${Date.now()}`;
		await createInvoice(page, number);

		await page.goto('/invoices');
		const row = await rowFor(page, number);
		await expect(row.getByRole('button', { name: 'Delete' })).toBeVisible();
	});

	// The three statuses the page-local set omitted. Each is refused by
	// `DELETE /api/invoices/{id}`, so the control must not be rendered.
	for (const status of ['posted_in_erp', 'payment_scheduled', 'paid'] as const) {
		test(`Delete is withheld once the invoice is ${status}`, async ({ page }) => {
			const number = `E2E-DELGATE-${status.toUpperCase()}-${Date.now()}`;
			const id = await createInvoice(page, number);
			// The API deliberately refuses caller-supplied statuses, so place the
			// row directly — the same setup the transitions spec uses.
			tenantPsql(`UPDATE invoices SET status='${status}' WHERE id='${id}'`);

			await page.goto('/invoices');
			const row = await rowFor(page, number);

			// Sanity: the row really is in the immutable status under test.
			await expect(row).not.toContainText('New');
			await expect(row.getByRole('button', { name: 'Delete' })).toHaveCount(0);

			// And the server agrees — the control was withheld for a real reason.
			const res = await page.request.delete(`${API_BASE}/api/invoices/${id}`, {
				headers: await authedTenantHeaders(page)
			});
			expect(res.status()).toBe(409);
		});
	}
});
