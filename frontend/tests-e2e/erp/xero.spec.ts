import type { APIRequestContext, Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	test
} from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	sendToErpAndAwaitTerminal,
	syncErpGlAccounts,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * xero adapter e2e — the REAL Xero adapter
 * (backend/app/services/erp_adapters/xero.py) connected through the REAL
 * OAuth flow (backend/app/api/erp_oauth.py) against fake-erp's stub Xero
 * identity server and accounting API (tools/fake-erp, `pnpm erp:up`). The
 * committed backend/.env.development points FEOH_ERP_XERO_AUTHORIZE_URL /
 * _TOKEN_URL / _API_BASE at the fake and supplies its stub app credentials.
 *
 * Xero's connect has a step QuickBooks' does not: the callback carries no
 * company id, so the backend calls `GET /connections` with the fresh token
 * and stores the one organisation that consent authorised. A connected
 * status naming the fake tenant proves that lookup ran.
 *
 * Coverage:
 *   1. Connect — authorize → consent 302 → callback → code exchange →
 *      /connections; the org is connected to `fake-xero-tenant-1` and the
 *      connection test passes on the stored token.
 *   2. Full send — after the contact + account syncs, an approved invoice
 *      posts as an ACCPAY bill; the bill read back FROM THE FAKE carries the
 *      exact gross, the synced contact, our invoice number and currency.
 *   3. Fail closed — an unlinked vendor, and a bill with no invoice / due
 *      date, are refused before anything is posted (Xero needs both dates;
 *      the adapter never invents them).
 *   4. Paid in Xero → recorded in FeohLedger (decisions §303) — a pushed bill
 *      rests at `sent_to_erp`; once fake Xero marks it PAID with money paid,
 *      `POST /sync-erp-status` (the on-demand form of the ERP status sweep)
 *      records an external payment and the invoice reaches `paid`. A bill
 *      Xero shows PAID by credit alone, and one VOIDED, are flagged for a
 *      person instead and the invoice stays where it was.
 *   5. Disconnect — clears the connection.
 */

const PROVIDER = 'xero';
const TENANT = 'fake-xero-tenant-1';
const SUPPLIER = 'Fake Xero Supplier Co';

async function connectXero(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	expect(authorize_url).toContain('/xero/identity/connect/authorize?');

	const consent = await page.request.get(authorize_url, { maxRedirects: 0 });
	expect(consent.status(), await consent.text()).toBe(302);
	const callback = consent.headers()['location'];
	expect(new URL(callback).pathname).toBe('/api/erp/oauth/callback');

	const landed = await page.request.get(callback, { maxRedirects: 0 });
	expect(landed.status()).toBe(302);
	const home = new URL(landed.headers()['location']);
	expect(home.pathname).toBe('/organization');
	expect(home.searchParams.get('erp_error')).toBeNull();
}

async function oauthStatus(page: Page) {
	const resp = await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	return resp.json();
}

/** Drive fake Xero's bill status through its test hook. */
async function setXeroStatus(
	request: APIRequestContext,
	id: string,
	status: string,
	amountPaid?: string
): Promise<void> {
	const resp = await request.post(`${FAKE_ERP_BASE}/xero/api.xro/2.0/__set-status`, {
		data: { id, status, ...(amountPaid ? { amount_paid: amountPaid } : {}) }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
}

/** Ask the ERP now what happened to a pushed invoice (the on-demand sweep). */
async function syncErpStatus(page: Page, invoiceId: string) {
	const resp = await page.request.post(`${API_BASE}/api/invoices/${invoiceId}/sync-erp-status`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status(), await resp.text()).toBe(200);
	return (await resp.json()) as { status: string; erp_status: string; outcome: string };
}

/** Push an approved Xero-ready invoice and return it with its bill id. */
async function pushedInvoice(page: Page, prefix: string) {
	await syncErpVendors(page);
	await syncErpGlAccounts(page);
	const inv = await createApprovedInvoice(page, {
		prefix,
		invoiceDate: '2026-10-01',
		dueDate: '2026-10-31',
		amount: '1150.00',
		taxAmount: '150.00',
		currency: 'ZAR',
		vendor: SUPPLIER,
		glAccount: '6100'
	});
	expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('sent_to_erp');
	return { inv, billId: await erpReferenceFromAudit(page, inv.id) };
}

/** A bill straight from the fake, as Xero holds it. */
async function xeroBill(request: APIRequestContext, id: string) {
	const resp = await request.get(`${FAKE_ERP_BASE}/xero/api.xro/2.0/Invoices/${id}`, {
		headers: { Authorization: 'Bearer spec', 'Xero-Tenant-Id': TENANT }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return (await resp.json()).Invoices[0];
}

test.describe('/erp xero adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await connectXero(page);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		const headers = await authedTenantHeaders(page);
		await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, { headers });
		await page.request.patch(`${API_BASE}/api/organization`, {
			headers: { ...headers, 'Content-Type': 'application/json' },
			data: { settings: { erp: null } }
		});
		await context.close();
	});

	test('OAuth connect resolves the organisation through /connections', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.external_tenant_id).toBe(TENANT);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the bill in Xero carries the exact approved gross', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '1150.00',
			taxAmount: '150.00',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('sent_to_erp');
			const bill = await xeroBill(request, await erpReferenceFromAudit(page, inv.id));
			expect(bill.Type).toBe('ACCPAY');
			expect(bill.Contact.ContactID).toBe('xero-contact-1');
			expect(bill.InvoiceNumber).toBe(inv.invoice_number);
			expect(bill.CurrencyCode).toBe('ZAR');
			expect(Number(bill.Total)).toBe(1150);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO-UNLINKED',
			currency: 'ZAR',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Xero post refused: vendor_not_linked');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill without its dates is refused, never dated by us', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO-NODATE',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Xero post refused: missing_dates');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill paid in Xero is recorded in FeohLedger as an external payment', async ({
		page,
		request
	}) => {
		const { inv, billId } = await pushedInvoice(page, 'E2E-XERO-PAID');
		const headers = await authedTenantHeaders(page);

		// Still owed in Xero: nothing changes.
		expect(await syncErpStatus(page, inv.id)).toMatchObject({
			status: 'sent_to_erp',
			erp_status: 'open',
			outcome: 'unchanged'
		});

		await setXeroStatus(request, billId, 'PAID', '1150.00');
		expect(await syncErpStatus(page, inv.id)).toEqual({
			id: inv.id,
			status: 'paid',
			erp_status: 'paid',
			outcome: 'recorded_paid'
		});

		// The ERP-sourced payment: completed, external, referencing the bill.
		const listed = await page.request.get(`${API_BASE}/api/payments`, {
			headers,
			params: { invoice_id: inv.id }
		});
		expect(listed.status(), await listed.text()).toBe(200);
		const payments = (await listed.json()).items as Array<{
			status: string;
			provider: string | null;
			reference: string | null;
		}>;
		expect(payments).toHaveLength(1);
		expect(payments[0]).toMatchObject({
			status: 'completed',
			provider: 'external',
			reference: billId
		});

		const audit = await page.request.get(`${API_BASE}/api/invoices/${inv.id}/audit-log`, {
			headers
		});
		const actions = ((await audit.json()) as Array<{ action: string }>).map((r) => r.action);
		expect(actions).toContain('invoice.paid_via_erp_report');

		// Asked again, it is no longer waiting on the ERP: nothing is recorded twice.
		const again = await page.request.post(
			`${API_BASE}/api/invoices/${inv.id}/sync-erp-status`,
			{ headers }
		);
		expect(again.status()).toBe(409);
	});

	test('a bill Xero settled by credit, or voided, is flagged and never recorded as paid', async ({
		page,
		request
	}) => {
		const headers = await authedTenantHeaders(page);
		const credited = await pushedInvoice(page, 'E2E-XERO-CREDITED');
		const voided = await pushedInvoice(page, 'E2E-XERO-VOIDED');

		// PAID with nothing paid against it is a credit note, not money.
		await setXeroStatus(request, credited.billId, 'PAID');
		expect(await syncErpStatus(page, credited.inv.id)).toMatchObject({
			status: 'sent_to_erp',
			erp_status: 'settled_no_payment',
			outcome: 'flagged'
		});

		await setXeroStatus(request, voided.billId, 'VOIDED');
		expect(await syncErpStatus(page, voided.inv.id)).toMatchObject({
			status: 'sent_to_erp',
			erp_status: 'cancelled',
			outcome: 'flagged'
		});

		for (const { inv } of [credited, voided]) {
			const listed = await page.request.get(`${API_BASE}/api/payments`, {
				headers,
				params: { invoice_id: inv.id }
			});
			expect((await listed.json()).items).toHaveLength(0);
			const flags = await page.request.get(`${API_BASE}/api/exceptions`, {
				headers,
				params: { type: 'erp_reconciliation', search: inv.invoice_number }
			});
			expect(flags.status(), await flags.text()).toBe(200);
			expect(await flags.text()).toContain(inv.id);
		}
	});

	test('disconnect clears the connection', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const resp = await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, {
			headers
		});
		expect((await resp.json()).disconnected).toBe(true);
		const status = await oauthStatus(page);
		expect(status.connected).toBe(false);
	});
});
