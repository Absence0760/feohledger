import { deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * Approval is bound to the version the approver loaded (`docs/decisions.md`
 * §260). `InvoiceModal` sends the `updated_at` it opened with as
 * `expected_updated_at`; an edit that lands while the modal is open makes the
 * server refuse the approval (409 `invoice_stale_approval`) instead of signing
 * figures nobody on this screen saw. The modal then drops its stale copy: it
 * says why and closes, and the invoice is still waiting for review.
 *
 * The edit is made by SQL so it is unmistakably "someone else, elsewhere" —
 * and it sets `updated_at` itself, because the ORM's `onupdate` is what moves
 * the version on every app write and a raw UPDATE would otherwise not.
 */

const PREFIX = 'E2E-STALE-APPROVAL-';

function seedReadyForReview(invoiceNumber: string): void {
	// `uploaded_by_id` stays NULL so segregation of duties never decides this:
	// the only reason left for the approval to fail is the version check.
	tenantPsql(
		`INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, vendor_name,
		   amount, currency, status, created_at, updated_at)
		 SELECT gen_random_uuid(), gen_random_uuid(), organization_id, '${invoiceNumber}',
		   'Stale Approval Vendor', 1500.00, 'USD', 'ready_for_review', now(), now()
		 FROM invoices LIMIT 1`
	);
}

test.describe('InvoiceModal approval is bound to the loaded version', () => {
	test.afterEach(() => {
		deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}%'`);
	});

	test('an edit made while the modal is open is refused, not approved unseen', async ({ page }) => {
		const number = `${PREFIX}${Date.now()}`;
		seedReadyForReview(number);

		await page.goto('/invoices');
		const listed = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes(`search=${encodeURIComponent(number)}`) &&
				r.request().method() === 'GET'
		);
		await page.getByPlaceholder('Search invoices...').fill(number);
		await listed;
		const row = page.locator('table tbody tr', { hasText: number }).first();
		await row.getByRole('button', { name: 'Edit' }).click();

		const modal = page.getByRole('dialog');
		const approve = modal.getByRole('button', { name: /^Approve$/ });
		await expect(approve).toBeVisible();

		// Someone else changes the amount while the approver is reading it.
		tenantPsql(
			`UPDATE invoices SET amount = 9500.00, updated_at = now() WHERE invoice_number = '${number}'`
		);

		const refused = page.waitForResponse(
			(r) => r.url().includes('/approve') && r.request().method() === 'POST'
		);
		await approve.click();
		const resp = await refused;
		expect(resp.status()).toBe(409);
		expect(resp.request().postDataJSON()).toHaveProperty('expected_updated_at');

		await expect(page.getByText('This invoice was changed after you loaded it.')).toBeVisible();
		await expect(modal).toHaveCount(0);
		expect(tenantPsql(`SELECT status FROM invoices WHERE invoice_number = '${number}'`).trim()).toBe(
			'ready_for_review'
		);
	});
});
