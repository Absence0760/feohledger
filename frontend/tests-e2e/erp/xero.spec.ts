import type { APIRequestContext, Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	deleteVendorsWhere,
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
	syncErpPurchaseOrders,
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
 *   4. Supplier + PO sync — a contact Xero does not yet flag IsSupplier (it
 *      has only been sent POs) still syncs as a vendor, and every PO links to
 *      its vendor by ContactID. Both regressed in the live Demo Company test.
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
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
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

	test('a supplier only ordered from syncs, and each PO links to its vendor', async ({
		page
	}) => {
		// Start without the PO-only supplier or its POs, so an earlier run's
		// rows cannot stand in for what this sync has to produce.
		deleteVendorsWhere(`erp_vendor_id = 'xero-contact-3'`, currentTenantSlug());
		await syncErpVendors(page);
		const { adapter, pos } = await syncErpPurchaseOrders(page);
		expect(adapter).toBe(PROVIDER);
		const vendorOf = (number: string) => pos.find((po) => po.po_number === number)?.vendor_name;
		// Xero marks a contact IsSupplier only once it has a bill, so this one
		// is reached through its unbilled POs (an authorised one and a draft).
		expect(vendorOf('PO-XERO-403')).toBe('Fake Xero PO-only Supplier');
		expect(vendorOf('PO-XERO-404')).toBe('Fake Xero PO-only Supplier');
		expect(vendorOf('PO-XERO-401')).toBe(SUPPLIER);
	});

	test('disconnect clears the connection', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const resp = await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, {
			headers
		});
		// The grant is revoked at Xero, not just forgotten here — otherwise the
		// org stays connected on Xero's side (the live Demo Company showed it
		// "Already connected" on the next consent).
		expect(await resp.json()).toEqual({ disconnected: true, revoked: true });
		const status = await oauthStatus(page);
		expect(status.connected).toBe(false);
	});
});
