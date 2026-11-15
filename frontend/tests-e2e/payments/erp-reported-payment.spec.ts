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
import { expectNoA11yViolations } from '../a11y/axe-helper';

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
 *    dialog, pre-filled with the rail, reference and date the ERP reported,
 *    beside the figure the ERP states and the invoice's own (§386);
 *  - a payment the ERP split across two rails shows the split, offers no rail
 *    picker, and is booked with that split and no rail named (§396, §413);
 *  - a split whose parts do not add up is shown with the picker kept, and the
 *    rail chosen is sent and booked; a row stating no figure says so, shows
 *    its split without claiming the sum fails, and keeps the picker (§413);
 *  - a blocked row says WHICH duty stops this reader clearing it: creator,
 *    raiser, or the invoice's approver (§169, §420);
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

/** The row a QuickBooks sync keeps since decisions §393: it states the cash
 *  QuickBooks shows paid. A row with no `amount` (one kept before §393) is
 *  not booked as it stands; the bill is read back first, and with no
 *  QuickBooks connected here that read confirms nothing, so it is refused. */
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
		// The cash QuickBooks states for the bill (§393): a row without it is
		// re-read from the ERP before anything is booked, never booked at our
		// own figure.
		amount: '150.00',
		amount_currency: 'USD',
		payment_parts: null,
		refusal_code: 'external_payment_blocking_exception'
	};
}

function queueRow(page: Page, tag: string) {
	return page.locator('table tbody tr', { hasText: tag });
}

const DESKTOP = { width: 1280, height: 800 };
const PHONE = { width: 320, height: 800 };

/** Scan the open record dialog at desktop and at phone width (WCAG 1.4.10
 *  reflow: the figures, the split list and the read-only method block must
 *  hold up at 320 px as well), leaving the page at desktop width. */
async function expectDialogAccessibleAtBothWidths(page: Page) {
	await expect(page.getByTestId('erp-payment-panel')).toBeVisible();
	await expectNoA11yViolations(page);
	await page.setViewportSize(PHONE);
	await expect(page.getByTestId('erp-payment-panel')).toBeVisible();
	await expectNoA11yViolations(page);
	await page.setViewportSize(DESKTOP);
}

/** Rewrite `GET /api/payments/queue` so the row for `tag` carries
 *  `clearRefusal` as this reader's verdict on its blocking exception. The rest
 *  of the payload is the real backend's. */
async function injectClearRefusal(page: Page, tag: string, clearRefusal: string) {
	await page.route('**/api/payments/queue**', async (route) => {
		if (route.request().method() !== 'GET') {
			await route.continue();
			return;
		}
		const response = await route.fetch();
		const body = (await response.json()) as {
			items?: {
				invoice_number: string;
				blocking_exception?: { clear_refusal: string | null } | null;
			}[];
		};
		for (const item of body.items ?? []) {
			if (item.invoice_number === tag && item.blocking_exception) {
				item.blocking_exception.clear_refusal = clearRefusal;
			}
		}
		await route.fulfill({ response, json: body });
	});
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
			// The figure the record books, beside what the invoice is for (§386).
			await expect(panel.getByTestId('erp-payment-reported-amount')).toHaveText('$150.00');
			await expect(panel.getByTestId('erp-payment-invoice-amount')).toHaveText('$150.00');
			// One rail: no split is shown and the rail stays a choice.
			await expect(panel.getByTestId('erp-payment-split')).toHaveCount(0);

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

	test('a payment the ERP split across two rails shows the split and books it with no rail picked', async ({
		page
	}) => {
		const tag = `E396S-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			const erpId = openException(invoiceId, 'payment_reconciliation', {
				...chequeReport(tag),
				method: null,
				payment_parts: [
					{ method: 'check', amount: '100.00' },
					{ method: 'card', amount: '50.00' }
				],
				refusal_code: 'external_payment_in_run'
			});

			await page.goto(`/exceptions?id=${erpId}`);
			const panel = page.getByTestId('erp-payment-panel');
			await expect(panel).toBeVisible();
			const parts = panel.getByTestId('erp-payment-split-part');
			await expect(parts).toHaveCount(2);
			await expect(parts.nth(0)).toContainText('Check');
			await expect(parts.nth(0)).toContainText('$100.00');
			await expect(parts.nth(1)).toContainText('$50.00');
			await expect(panel.getByTestId('erp-payment-split-note')).toContainText(
				'recorded with this split'
			);
			// The split is the record whatever rail is named, so there is no picker.
			await expect(panel.getByTestId('erp-payment-method')).toHaveCount(0);
			await expect(panel.getByTestId('erp-payment-method-fixed')).toBeVisible();
			// The figures, the split list and the read-only method block.
			await expectDialogAccessibleAtBothWidths(page);

			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/exceptions/${erpId}/record-erp-payment` &&
					r.request().method() === 'POST'
			);
			await panel.getByTestId('erp-payment-submit').click();
			const response = await recorded;
			expect(response.status()).toBe(201);
			expect(response.request().postDataJSON()).not.toHaveProperty('method');
			const payment = (await response.json()) as {
				method: string | null;
				rail_breakdown: { method: string | null; amount: number }[] | null;
			};
			expect(payment.method).toBeNull();
			expect(payment.rail_breakdown).toEqual([
				{ method: 'check', amount: 100 },
				{ method: 'card', amount: 50 }
			]);
			await expect(panel).toBeHidden();
			expect(await getInvoiceStatus(page, invoiceId)).toBe('paid');
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('a split whose parts do not add up is shown, keeps the picker, and books the rail chosen', async ({
		page
	}) => {
		const tag = `E413D-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			// 100 + 40 is not the 150 QuickBooks states: the server judges the
			// split does not stand (`erp_payment_split_stands: false`).
			const erpId = openException(invoiceId, 'payment_reconciliation', {
				...chequeReport(tag),
				method: null,
				payment_parts: [
					{ method: 'check', amount: '100.00' },
					{ method: 'card', amount: '40.00' }
				],
				refusal_code: 'external_payment_in_run'
			});

			await page.goto(`/exceptions?id=${erpId}`);
			const panel = page.getByTestId('erp-payment-panel');
			await expect(panel).toBeVisible();
			await expect(panel.getByTestId('erp-payment-split-part')).toHaveCount(2);
			await expect(panel.getByTestId('erp-payment-split-note')).toContainText(
				'do not add up to the amount paid'
			);
			await expect(panel.getByTestId('erp-payment-method-fixed')).toHaveCount(0);
			const picker = panel.getByTestId('erp-payment-method');
			await expect(picker).toBeVisible();
			await expectDialogAccessibleAtBothWidths(page);

			await picker.selectOption('check');
			const recorded = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/exceptions/${erpId}/record-erp-payment` &&
					r.request().method() === 'POST'
			);
			await panel.getByTestId('erp-payment-submit').click();
			const response = await recorded;
			expect(response.status(), await response.text()).toBe(201);
			expect(response.request().postDataJSON()).toMatchObject({ method: 'check' });
			// The person's rail is the record for the whole payment.
			expect(((await response.json()) as { method: string | null }).method).toBe('check');
			await expect(panel).toBeHidden();
			expect(await getInvoiceStatus(page, invoiceId)).toBe('paid');
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('a row stating no figure says so, shows its split without judging it, and keeps the picker', async ({
		page
	}) => {
		const tag = `E413N-${Date.now().toString(36)}`;
		try {
			const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
			// A row kept before §393 states no cash, so nobody can say whether
			// its parts add up (`erp_payment_split_stands: null`).
			const erpId = openException(invoiceId, 'payment_reconciliation', {
				...chequeReport(tag),
				method: null,
				amount: null,
				amount_currency: null,
				payment_parts: [
					{ method: 'check', amount: '100.00' },
					{ method: 'card', amount: '50.00' }
				],
				refusal_code: 'external_payment_in_run'
			});

			await page.goto(`/exceptions?id=${erpId}`);
			const panel = page.getByTestId('erp-payment-panel');
			await expect(panel).toBeVisible();
			await expect(panel.getByTestId('erp-payment-reported-amount')).toHaveText(
				'Not stated. The bill is read again in the ERP when you record the payment.'
			);
			await expect(panel.getByTestId('erp-payment-invoice-amount')).toHaveText('$150.00');
			await expect(panel.getByTestId('erp-payment-split-part')).toHaveCount(2);
			const note = panel.getByTestId('erp-payment-split-note');
			await expect(note).toContainText('decided when the payment is recorded');
			await expect(note).not.toContainText('do not add up');
			await expect(panel.getByTestId('erp-payment-method')).toBeVisible();
			await expect(panel.getByTestId('erp-payment-method-fixed')).toHaveCount(0);
			await expectDialogAccessibleAtBothWidths(page);
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	for (const [code, sentence] of [
		['segregation_approver', 'Someone else must clear it: you approved this invoice.'],
		['segregation_raiser', 'Someone else must clear it: your own action raised it.']
	] as const) {
		test(`a blocked row states the ${code} duty that stops this reader clearing it`, async ({
			page
		}) => {
			const tag = `E420${code === 'segregation_approver' ? 'A' : 'R'}-${Date.now().toString(36)}`;
			try {
				const invoiceId = await payableInvoice(page, tag, { uploadedBy: false });
				const blockerId = openException(invoiceId, 'po_mismatch');
				await injectClearRefusal(page, tag, code);

				await page.goto('/payments');
				const row = queueRow(page, tag);
				await loadMoreUntilRow(page, row);
				await expect(row.getByTestId('queue-blocked-exception-link')).toHaveAttribute(
					'href',
					`/exceptions?id=${blockerId}`
				);
				await expect(row.getByTestId('queue-blocked-clear-refusal')).toHaveText(sentence);
			} finally {
				deleteVendorsWhere(`name='${tag} Supplier'`);
			}
		});
	}

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
