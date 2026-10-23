import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	loadMoreUntilRow,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * A payment the ERP reported, kept and recorded without re-keying it
 * (decisions §319). The production confusion this pins:
 *
 *  - a payment-queue row blocked by an exception now NAMES it, links to it
 *    (`/exceptions?id=` opens that exception's dialog), and says when someone
 *    else must clear it; "Record as paid" is shown disabled with its reason,
 *    never hidden;
 *  - dismissing the stale blocker from that dialog records the ERP's payment
 *    on its own — the invoice is paid with nothing typed;
 *  - an ERP row that is itself blocking is recorded in one click from its
 *    dialog, pre-filled with the rail, reference and date the ERP reported;
 *  - "Record as paid" opens pre-filled from the ERP's report (a cheque stays a
 *    cheque, not the old ACH default).
 *
 * Each test builds its own vendor, invoice and exceptions with a unique tag and
 * deletes them (vendor → invoices → payments → exceptions) afterwards.
 */

const PAID_ON = '2026-10-01';

async function getInvoiceStatus(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await resp.json()) as { status: string }).status;
}

/** An active vendor's approved invoice. `uploadedBy: false` clears the
 *  uploader, so segregation of duties lets the signed-in admin clear its
 *  exceptions; `true` keeps the admin as uploader. */
async function payableInvoice(
	page: Page,
	tag: string,
	{ uploadedBy }: { uploadedBy: boolean }
): Promise<string> {
	const vendorName = `${tag} Supplier`;
	tenantPsql(
		`INSERT INTO vendors (id, organization_id, name, status, source, accepts_virtual_cards, is_1099_eligible, kyc_status, screening_status, payments_blocked, risk_level, created_at, updated_at)
		 SELECT gen_random_uuid(), (SELECT organization_id FROM vendors LIMIT 1), '${vendorName}', 'active', 'manual', false, false, 'not_required', 'clear', false, 'low', now(), now()`
	);
	const vendorId = tenantPsql(`SELECT id FROM vendors WHERE name='${vendorName}'`).trim();
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: vendorName, invoice_number: tag, amount: 150.0, currency: 'USD' }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	const id = ((await resp.json()) as { id: string }).id;
	tenantPsql(
		`UPDATE invoices SET status='approved', vendor_id='${vendorId}'${uploadedBy ? '' : ', uploaded_by_id=NULL'} WHERE id='${id}'`
	);
	return id;
}

/** An open exception on the invoice; `erpReport` makes it an ERP row holding
 *  the payment QuickBooks reported. Returns its id. */
function openException(
	invoiceId: string,
	type: string,
	erpReport: Record<string, unknown> | null = null
): string {
	const report = erpReport ? `'${JSON.stringify(erpReport)}'::jsonb` : 'NULL';
	return tenantPsql(
		`INSERT INTO exceptions (id, invoice_id, organization_id, entity_id, exception_type, severity, description, status, erp_report, created_at, updated_at)
		 SELECT gen_random_uuid(), i.id, i.organization_id, i.entity_id, '${type}', 'error', 'e2e §319', 'open', ${report}, now(), now()
		 FROM invoices i WHERE i.id='${invoiceId}' RETURNING id`
	)
		.split('\n')[0]
		.trim();
}

function chequeReport(tag: string): Record<string, unknown> {
	return {
		kind: 'paid_not_recorded',
		erp_type: 'quickbooks_online',
		erp_document_id: `BILL-${tag}`,
		event_id: null,
		method: 'check',
		reference: `BILL-${tag}`,
		paid_on: PAID_ON,
		paid_on_reported: true,
		refusal_code: 'external_payment_blocking_exception'
	};
}

function queueRow(page: Page, tag: string) {
	return page.locator('table tbody tr', { hasText: tag });
}

test.describe('/payments + /exceptions — the ERP-reported payment', () => {
	test('a blocked row names its exception, says who may clear it, and shows Record as paid disabled', async ({
		page
	}) => {
		const tag = `E318A-${Date.now().toString(36)}`;
		try {
			// The admin uploaded it, so the admin may not clear its blocker.
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: true });
			const blockerId = openException(invoiceId, 'payment_reconciliation');

			await page.goto('/payments');
			const row = queueRow(page, tag);
			await loadMoreUntilRow(page, row);
			await expect(row.getByTestId('queue-blocked-chip')).toBeVisible();

			const link = row.getByTestId('queue-blocked-exception-link');
			await expect(link).toHaveAttribute('href', `/exceptions?id=${blockerId}`);
			await expect(row.getByTestId('queue-blocked-clear-refusal')).toHaveText(
				'Someone else must clear it: you created or changed this invoice.'
			);

			const record = row.getByRole('button', { name: `Record as paid — invoice ${tag}` });
			await expect(record).toBeDisabled();
			await expect(record).toHaveAccessibleDescription(/unresolved Payment Reconciliation exception/);

			// The link lands on that exception's dialog, not a bare queue.
			await link.click();
			await expect(page).toHaveURL(new RegExp(`/exceptions\\?.*id=${blockerId}`));
			const form = page.getByTestId('exception-resolve-form');
			await expect(form).toBeVisible();
			await expect(page.getByTestId('exception-decision-refusal')).toBeVisible();
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('dismissing the stale blocker records the payment the ERP reported', async ({ page }) => {
		const tag = `E318B-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			const blockerId = openException(invoiceId, 'payment_reconciliation');
			openException(invoiceId, 'erp_reconciliation', chequeReport(tag));

			await page.goto(`/exceptions?id=${blockerId}`);
			const form = page.getByTestId('exception-resolve-form');
			await expect(form).toBeVisible();
			const dismissed = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/exceptions/${blockerId}/resolve` &&
					r.request().method() === 'POST'
			);
			await form.getByRole('button', { name: 'Dismiss' }).click();
			expect((await dismissed).status()).toBe(200);
			await expect(form).toBeHidden();

			expect(await getInvoiceStatus(page, invoiceId)).toBe('paid');
			const payment = tenantPsql(
				`SELECT method || '|' || reference || '|' || to_char(completed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') FROM payments WHERE invoice_id='${invoiceId}' AND status='completed'`
			).trim();
			expect(payment).toBe(`check|BILL-${tag}|${PAID_ON}`);
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('a blocking ERP row is recorded in one click, pre-filled from the report', async ({
		page
	}) => {
		const tag = `E318C-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			const erpId = openException(invoiceId, 'payment_reconciliation', {
				...chequeReport(tag),
				refusal_code: 'external_payment_in_run'
			});

			await page.goto(`/exceptions?id=${erpId}`);
			const panel = page.getByTestId('erp-payment-panel');
			await expect(panel).toBeVisible();
			await expect(panel.getByRole('heading')).toHaveText('Payment reported by QuickBooks Online');
			await expect(panel.getByTestId('erp-payment-method')).toHaveValue('check');
			await expect(panel.getByTestId('erp-payment-reference')).toHaveValue(`BILL-${tag}`);
			await expect(panel.getByTestId('erp-payment-paid-on')).toHaveValue(PAID_ON);

			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/exceptions/${erpId}/record-erp-payment` &&
					r.request().method() === 'POST'
			);
			await panel.getByTestId('erp-payment-submit').click();
			expect((await recorded).status()).toBe(201);
			await expect(panel).toBeHidden();
			expect(await getInvoiceStatus(page, invoiceId)).toBe('paid');
			expect(tenantPsql(`SELECT status FROM exceptions WHERE id='${erpId}'`).trim()).toBe(
				'resolved'
			);
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('Record as paid opens pre-filled from what the ERP reported', async ({ page }) => {
		const tag = `E318D-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			openException(invoiceId, 'erp_reconciliation', chequeReport(tag));

			await page.goto('/payments');
			const row = queueRow(page, tag);
			await loadMoreUntilRow(page, row);
			await row.getByRole('button', { name: `Record as paid — invoice ${tag}` }).click();

			const dialog = page.getByRole('dialog', {
				name: 'Record a payment made outside FeohLedger'
			});
			await expect(dialog).toBeVisible();
			await expect(dialog.getByTestId('record-prefilled-from-erp')).toContainText(
				'QuickBooks Online'
			);
			// A cheque stays a cheque — the dialog used to start on ACH.
			await expect(dialog.getByTestId('record-method')).toHaveValue('check');
			await expect(dialog.getByTestId('record-reference')).toHaveValue(`BILL-${tag}`);
			await expect(dialog.getByTestId('record-paid-on')).toHaveValue(PAID_ON);
			await dialog.getByRole('button', { name: 'Cancel' }).click();
			await expect(dialog).toBeHidden();
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});
});
