import type { Page } from '@playwright/test';

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
	ERP_OAUTH_CALLBACK,
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	syncErpGlAccounts,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * quickbooks_online adapter e2e — the REAL QuickBooks Online adapter
 * (backend/app/services/erp_adapters/quickbooks_online.py) connected through
 * the REAL OAuth flow (backend/app/api/erp_oauth.py) against the fake ERP's
 * `/qbo` surface (tools/fake-erp, `pnpm erp:up`). The committed
 * backend/.env.development points every FEOH_ERP_QBO_* URL at the fake and
 * supplies the platform app's client id/secret, so nothing here needs an
 * Intuit account.
 *
 * Unlike the NetSuite / Business Central specs there is no static credential
 * to PATCH in: the tokens only exist if the authorize → consent → callback →
 * code-exchange chain works end to end, so the connect itself is under test.
 *
 * Coverage:
 *   1. Connect — the backend's consent URL, the fake's 302 back to the
 *      registered callback, and the callback's code exchange leave the org
 *      connected to realm `fake-realm-1`; the connection test then passes on
 *      the stored bearer.
 *   2. Full send — after the vendor + chart syncs, an approved invoice posts
 *      as a Bill. The bill read back FROM THE FAKE carries the exact approved
 *      amount, the synced vendor id and the synced account id — so the
 *      assertion is on what the ERP holds, not on what we sent.
 *   3. Fail closed — an invoice whose vendor never synced is refused before
 *      anything is posted, and so is an invoice number longer than QBO's
 *      21-character DocNumber (refused, never silently truncated).
 *   4. Disconnect — clears the connection, and a later send cannot post.
 */

const PROVIDER = 'quickbooks_online';
const REALM = 'fake-realm-1';

/** Run the whole OAuth connect chain the way a browser would. */
async function connectQbo(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`, {
		headers
	});
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();

	// The fake skips the consent screen and 302s to the registered callback.
	const consent = await page.request.get(authorize_url, { maxRedirects: 0 });
	expect(consent.status()).toBe(302);
	const callback = consent.headers()['location'];
	expect(new URL(callback).pathname).toBe('/api/erp/oauth/callback');
	expect(new URL(callback).searchParams.get('realmId')).toBe(REALM);

	// The backend exchanges the code and sends the admin home, error-free.
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

/** Read a bill straight from the fake, with a bearer of our own. */
async function billFromFake(page: Page, id: string) {
	const qbo = `${FAKE_ERP_BASE}/qbo`;
	const authorize = await page.request.get(`${qbo}/oauth2/authorize`, {
		params: {
			redirect_uri: ERP_OAUTH_CALLBACK,
			state: 'spec',
			client_id: 'fake-qbo-client'
		},
		maxRedirects: 0
	});
	const code = new URL(authorize.headers()['location']).searchParams.get('code') ?? '';
	const token = await page.request.post(`${qbo}/oauth2/token`, {
		headers: {
			Authorization: `Basic ${Buffer.from('fake-qbo-client:fake-qbo-secret').toString('base64')}`,
			'Content-Type': 'application/x-www-form-urlencoded'
		},
		data: `grant_type=authorization_code&code=${code}`
	});
	const bearer = `Bearer ${(await token.json()).access_token}`;
	const bill = await page.request.get(`${qbo}/v3/company/${REALM}/bill/${id}`, {
		headers: { Authorization: bearer }
	});
	expect(bill.ok()).toBe(true);
	return (await bill.json()).Bill;
}

test.describe('/erp quickbooks_online adapter against fake-erp', () => {
	test.beforeAll(async () => {
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await connectQbo(page);
	});

	// Other suites assume the seeded org has NO ERP configured; disconnect
	// clears the sealed tokens, and the PATCH clears the block itself.
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

	test('OAuth connect lands connected to the fake realm', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.needs_reconnect).toBeFalsy();
		expect(status.external_tenant_id).toBe(REALM);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the bill in QuickBooks carries the exact approved amount', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBO',
			// QBO's DocNumber limit is 21 characters; the default number is longer.
			invoiceNumber: `Q${Date.now() % 1e9}-${Math.floor(Math.random() * 1e4)}`,
			amount: '1234.56',
			vendor: 'Fake ERP Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');

			const billId = await erpReferenceFromAudit(page, inv.id);
			const bill = await billFromFake(page, billId);
			expect(bill.TotalAmt).toBe(1234.56);
			expect(bill.VendorRef.value).toBe('56');
			expect(
				bill.Line.map(
					(l: { AccountBasedExpenseLineDetail?: { AccountRef?: { value: string } } }) =>
						l.AccountBasedExpenseLineDetail?.AccountRef?.value
				)
			).toEqual(['7']);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBO-UNLINKED',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toContain('vendor_not_linked');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice number over QBO\'s 21-character limit is refused, not truncated', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBO-LONG',
			invoiceNumber: `QBO-NUMBER-TOO-LONG-${Date.now()}`,
			vendor: 'Fake ERP Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'QuickBooks Online post refused: doc_number_too_long'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('disconnect clears the connection', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const resp = await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, { headers });
		expect(resp.ok()).toBe(true);
		expect((await resp.json()).disconnected).toBe(true);

		const status = await oauthStatus(page);
		expect(status.connected).toBe(false);
		expect(status.external_tenant_id ?? null).toBeNull();

		// Disconnecting twice is a no-op, not an error.
		const again = await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, { headers });
		expect((await again.json()).disconnected).toBe(false);
	});
});
