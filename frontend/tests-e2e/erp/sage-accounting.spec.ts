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
 * sage_accounting adapter e2e — the REAL Sage Business Cloud Accounting (v3.1)
 * adapter (backend/app/services/erp_adapters/sage_accounting.py) connected
 * through the REAL OAuth flow against fake-erp's stub Sage identity server
 * and accounting API (tools/fake-erp, `pnpm erp:up`). The committed
 * backend/.env.development points FEOH_ERP_SAGE_ACCOUNTING_AUTHORIZE_URL /
 * _TOKEN_URL / _API_BASE at the fake and supplies its stub app credentials.
 *
 * Two connect details differ from QuickBooks and Xero, and both are under
 * test: Sage's token endpoint takes the client as FORM fields (the fake
 * refuses Basic), and the business id comes from `GET /businesses` after the
 * exchange — a status naming the fake business proves that lookup ran.
 *
 * Coverage:
 *   1. Connect — the org is connected to `fake-sage-business`; the
 *      connection test passes on the stored token.
 *   2. Full send — a GBP invoice stating 20% VAT posts; the purchase invoice
 *      read back FROM THE FAKE carries the exact gross, the synced contact,
 *      our invoice number and dates, and the VAT split 1050.00 + 210.00 at
 *      the ledger's own GB_STANDARD rate.
 *   3. Fail closed — an unlinked vendor, and a taxed invoice on a ledger
 *      with no default tax rate, are refused before anything is posted.
 *   4. Disconnect — clears the connection.
 */

const PROVIDER = 'sage_accounting';
const TENANT = 'fake-sage-business';
const SUPPLIER = 'Fake Sage Supplier Ltd';

async function connectSage(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	expect(authorize_url).toContain('/sage/oauth2/auth/central?');

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

/** A purchase invoice straight from the fake, as Sage holds it. */
async function sageInvoice(request: APIRequestContext, id: string) {
	const resp = await request.get(`${FAKE_ERP_BASE}/sage/v3.1/purchase_invoices/${id}`, {
		headers: { Authorization: 'Bearer spec', 'X-Business': TENANT }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return resp.json();
}

test.describe('/erp sage_accounting adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await connectSage(page);
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

	test('OAuth connect resolves the business through /businesses', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.external_tenant_id).toBe(TENANT);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the purchase invoice in Sage carries the exact gross', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGE',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '1260.00',
			taxAmount: '210.00',
			currency: 'GBP',
			vendor: SUPPLIER,
			glAccount: '7500'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('sent_to_erp');
			const posted = await sageInvoice(request, await erpReferenceFromAudit(page, inv.id));
			expect(posted.contact.id).toBe('sage-contact-1');
			expect(posted.vendor_reference ?? posted.reference).toBe(inv.invoice_number);
			expect(Number(posted.total_amount)).toBe(1260);
			expect(posted.currency.id).toBe('GBP');
			expect([posted.date, posted.due_date]).toEqual(['2026-10-01', '2026-10-31']);
			// The VAT split: 1050.00 net + 210.00 at the ledger's own 20% rate.
			type Line = { net_amount: string; tax_amount: string; tax_rate_id?: string };
			const lines: Line[] = posted.invoice_lines;
			expect(lines.reduce((sum, l) => sum + Number(l.net_amount), 0)).toBe(1050);
			expect(lines.reduce((sum, l) => sum + Number(l.tax_amount), 0)).toBe(210);
			expect(lines.map((l) => l.tax_rate_id)).toEqual(['GB_STANDARD']);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGE-UNLINKED',
			currency: 'GBP',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '7500'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Sage Accounting post refused: vendor_not_linked');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a taxed invoice on a ledger with no default tax rate is refused', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-SAGE-NORATE',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '1260.00',
			taxAmount: '210.00',
			currency: 'GBP',
			vendor: SUPPLIER,
			// sage-ledger-7600 carries no default tax rate and the org sets none.
			glAccount: '7600'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'Sage Accounting post refused: tax_rate_unresolved'
			);
		} finally {
			await deleteInvoice(page, inv.id);
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
