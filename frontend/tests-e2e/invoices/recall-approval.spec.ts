import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * "Recall Approval" in the invoice modal (decisions §374): the exit a
 * financially locked invoice whose approved figures are wrong never had. The
 * control is offered from the server's `recall_approval` verdict
 * (`GET /api/invoices/{id}/review-actions`, `services/review.recall_refusal`),
 * opens the shared reason dialog, and lands the invoice back in review.
 *
 * Backend parity (verdict null exactly when the door accepts, every role and
 * row state) and every refusal: `backend/tests/test_invoice_recall_approval.py`.
 */

const PREFIX = 'E2E-RECALL';

async function createApproved(
	page: import('@playwright/test').Page,
	number: string,
	{ erpBill = false }: { erpBill?: boolean } = {}
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E Recall Vendor', invoice_number: number, amount: '58.31', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	// The stage is the fixture, not the path that leads there.
	tenantPsql(
		`UPDATE invoices SET status='approved', approved_by='Approver', approval_date=CURRENT_DATE` +
			(erpBill ? `, erp_document_id='E2E-BILL-1'` : '') +
			` WHERE id='${id}'`
	);
	return id;
}

async function openWithVerdict(page: import('@playwright/test').Page, id: string, number: string) {
	const verdict = page.waitForResponse(
		(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
	);
	await page.goto(`/invoices?id=${id}`);
	await verdict;
	const modal = page.getByRole('dialog', { name: `Edit invoice ${number}` });
	await expect(modal.locator('header h2')).toContainText(number);
	return modal;
}

test.describe('invoice modal — recall an approval', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	test('recalls an approved invoice back to review, with the reason audited', async ({ page }) => {
		const number = `${PREFIX}-OK-${Date.now()}`;
		const id = await createApproved(page, number);
		const modal = await openWithVerdict(page, id, number);

		const open = modal.getByTestId('recall-approval');
		await expect(open).toBeEnabled();
		await open.click();

		const dialog = page.getByRole('dialog', { name: 'Recall this approval?' });
		await expect(dialog).toBeVisible();
		const confirm = dialog.getByTestId('recall-approval-confirm');
		// A reason is required: blank and whitespace-only keep it disabled.
		await expect(confirm).toBeDisabled();
		await dialog.getByTestId('recall-approval-reason').fill('   ');
		await expect(confirm).toBeDisabled();
		await dialog.getByTestId('recall-approval-reason').fill('Amount keyed wrong');

		const recall = page.waitForResponse(
			(r) => r.request().method() === 'POST' && r.url().endsWith(`/api/invoices/${id}/recall-approval`)
		);
		await confirm.click();
		expect((await recall).status()).toBe(200);
		await expect(dialog).toHaveCount(0);
		await expect(modal).toHaveCount(0);

		expect(tenantPsql(`SELECT status FROM invoices WHERE id='${id}'`).trim()).toBe(
			'ready_for_review'
		);
		expect(
			tenantPsql(
				`SELECT details->>'reason' FROM audit_log WHERE entity_id='${id}' AND action='invoice.approval_recalled'`
			).trim()
		).toBe('Amount keyed wrong');
	});

	test('Cancel closes only the reason dialog and recalls nothing', async ({ page }) => {
		const number = `${PREFIX}-CANCEL-${Date.now()}`;
		const id = await createApproved(page, number);
		const modal = await openWithVerdict(page, id, number);

		await modal.getByTestId('recall-approval').click();
		const dialog = page.getByRole('dialog', { name: 'Recall this approval?' });
		await dialog.getByRole('button', { name: 'Cancel' }).click();
		await expect(dialog).toHaveCount(0);
		await expect(modal).toBeVisible();
		expect(tenantPsql(`SELECT status FROM invoices WHERE id='${id}'`).trim()).toBe('approved');
	});

	test('a bill already in the ERP disables Recall and says why', async ({ page }) => {
		const number = `${PREFIX}-ERP-${Date.now()}`;
		const id = await createApproved(page, number, { erpBill: true });
		const modal = await openWithVerdict(page, id, number);

		await expect(modal.getByTestId('recall-approval')).toBeDisabled();
		await expect(modal.getByTestId('recall-refusal')).toHaveText(
			"The ERP may already hold a bill for this invoice, and a bill there can be paid there. Correct or void it in the ERP; its approval can't be recalled from FeohLedger."
		);
	});

	test('an invoice in review is not offered Recall', async ({ page }) => {
		const number = `${PREFIX}-REVIEW-${Date.now()}`;
		const id = await createApproved(page, number);
		tenantPsql(`UPDATE invoices SET status='ready_for_review' WHERE id='${id}'`);
		const modal = await openWithVerdict(page, id, number);

		await expect(modal.getByTestId('recall-approval')).toHaveCount(0);
	});
});
