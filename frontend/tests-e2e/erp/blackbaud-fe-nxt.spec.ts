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
	ERP_OAUTH_CALLBACK,
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * blackbaud_fe_nxt adapter e2e — the REAL Blackbaud Financial Edge NXT (SKY
 * API) adapter (backend/app/services/erp_adapters/blackbaud_fe_nxt.py)
 * connected through the REAL OAuth flow against fake-erp's stub SKY identity
 * server and API (tools/fake-erp, `pnpm erp:up`). The committed
 * backend/.env.development points FEOH_ERP_BLACKBAUD_AUTHORIZE_URL /
 * _TOKEN_URL / _API_BASE at the fake and supplies stub app credentials and a
 * stub subscription key.
 *
 * Blackbaud's connect has its own shape: the environment id rides on the
 * TOKEN response (no lookup call), and every API call needs the subscription
 * key besides the bearer. The fake's API accepts only a bearer its token
 * endpoint issued, so a passing send proves the stored token is the one the
 * exchange returned.
 *
 * Coverage:
 *   1. Connect — the org is connected to environment `p-fake-env-1`; the
 *      connection test passes.
 *   2. Full send — with the AP account and ledger currency stated, an
 *      approved invoice posts through the SKY background process; the invoice read back FROM THE FAKE carries the exact amount
 *      and the synced vendor (the fake refuses distributions whose debits and
 *      credits do not both equal the amount).
 *   3. Fail closed — an unlinked vendor is refused before anything is posted,
 *      and so is any send while the org has not stated the FE NXT ledger's
 *      currency (the API reports none) or the invoice is in another one.
 *   3b. FE NXT's own state (#651) — an Inactive / OnHold vendor syncs as no
 *      payee; a post date in a closed fiscal period is refused
 *      `period_closed` before the create; a background job that fails is a
 *      coded `job_failed`, not a retry loop; and an invoice number with more
 *      than 500 search hits still posts (the lookup narrows to its date).
 *   4. Token lifecycle — a stored token inside the refresh margin is refreshed
 *      and the rotated refresh token persisted; a consent revoked at the
 *      provider turns the connection to `needs_reconnect`, and reconnecting
 *      clears it.
 *   5. Disconnect — clears the connection.
 */

const PROVIDER = 'blackbaud_fe_nxt';
const TENANT = 'p-fake-env-1';
const SUPPLIER = 'Fake Blackbaud Vendor A';
const BB = `${FAKE_ERP_BASE}/blackbaud`;

/** The two things the admin must state before FE NXT can post: the AP
 *  liability account the Credit distribution goes to, and the ledger's
 *  currency (the SKY API reports neither). Merged into settings.erp — the
 *  OAuth block survives the PATCH. */
async function setLedgerConfig(page: Page, currency: string | null): Promise<void> {
	await setErpSettings(page, {
		type: PROVIDER,
		integration_method: 'direct',
		ap_account_number: '01-2000-00',
		currency
	});
}

async function connectBlackbaud(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	expect(authorize_url).toContain('/blackbaud/oauth2/authorization?');

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

/** An invoice straight from the fake, with a bearer the fake issued to us. */
async function blackbaudInvoice(request: APIRequestContext, id: string) {
	const callback = ERP_OAUTH_CALLBACK;
	const authorize = await request.get(`${BB}/oauth2/authorization`, {
		params: {
			redirect_uri: callback,
			state: 'spec',
			client_id: 'fake-blackbaud-client',
			response_type: 'code'
		},
		maxRedirects: 0
	});
	const code = new URL(authorize.headers()['location']).searchParams.get('code') ?? '';
	const token = await request.post(`${BB}/oauth2/token`, {
		headers: {
			Authorization: `Basic ${Buffer.from('fake-blackbaud-client:fake-blackbaud-secret').toString('base64')}`,
			'Content-Type': 'application/x-www-form-urlencoded'
		},
		data: `grant_type=authorization_code&code=${code}&redirect_uri=${encodeURIComponent(callback)}`
	});
	expect(token.ok(), await token.text()).toBe(true);
	const resp = await request.get(`${BB}/accountspayable/v1/invoices/${id}`, {
		headers: {
			Authorization: `Bearer ${(await token.json()).access_token}`,
			'Bb-Api-Subscription-Key': 'spec'
		}
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return resp.json();
}

test.describe('/erp blackbaud_fe_nxt adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await connectBlackbaud(page);
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

	test('OAuth connect takes the environment from the token response', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.external_tenant_id).toBe(TENANT);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the invoice in Financial Edge carries the exact amount', async ({
		page,
		request
	}) => {
		await setLedgerConfig(page, 'USD');
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-BB',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '1234.56',
			currency: 'USD',
			vendor: SUPPLIER,
			glAccount: '01-5000-00'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			const posted = await blackbaudInvoice(request, await erpReferenceFromAudit(page, inv.id));
			expect(posted.vendor_id).toBe(136);
			expect(posted.amount).toBe(1234.56);
			expect(posted.balance).toBe(1234.56);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-BB-UNLINKED',
			currency: 'USD',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '01-5000-00'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Blackbaud FE NXT post refused: vendor_not_linked');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('the ledger currency must be stated, and must match', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const send = async (currency: string) => {
			const inv = await createApprovedInvoice(page, {
				prefix: 'E2E-BB-CCY',
				invoiceDate: '2026-10-01',
				dueDate: '2026-10-31',
				currency,
				vendor: SUPPLIER,
				glAccount: '01-5000-00'
			});
			try {
				expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
				return await erpFailureFromAudit(page, inv.id);
			} finally {
				await deleteInvoice(page, inv.id);
			}
		};
		// Nothing stated: never assume the ledger is in the invoice's currency.
		await setLedgerConfig(page, null);
		expect(await send('USD')).toBe('Blackbaud FE NXT post refused: currency_not_configured');
		// Stated, and the invoice is in another one: never booked as if it matched.
		await setLedgerConfig(page, 'USD');
		expect(await send('EUR')).toBe('Blackbaud FE NXT post refused: currency_mismatch');
	});

	/** Send an approved invoice and return the `invoice.erp_failed` message. */
	async function sendExpectingRefusal(
		page: Page,
		opts: { glAccount?: string; invoiceDate?: string }
	): Promise<string> {
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-BB-REFUSE',
			invoiceDate: opts.invoiceDate ?? '2026-10-01',
			dueDate: '2026-10-31',
			currency: 'USD',
			vendor: SUPPLIER,
			glAccount: opts.glAccount ?? '01-5000-00'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			return await erpFailureFromAudit(page, inv.id);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	}

	test('Inactive and OnHold FE NXT vendors sync as no payee; Active ones do', async ({
		page
	}) => {
		// Before the fix every vendor synced active: the Inactive one got a
		// payee (its push then died as an uncoded `HTTP 400 (invalid_request)`)
		// and the OnHold one could be paid from FeohLedger around FE NXT's hold.
		await syncErpVendors(page);
		const list = await page.request.get(
			`${API_BASE}/api/vendors?search=Fake%20Blackbaud%20Vendor&page_size=50`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(list.status()).toBe(200);
		const { items } = (await list.json()) as {
			items: { name: string; erp_vendor_id: string | null }[];
		};
		const synced = (name: string) =>
			items.some((v) => v.name === name && v.erp_vendor_id !== null);
		expect(synced(SUPPLIER)).toBe(true);
		expect(synced('Fake Blackbaud Vendor Inactive')).toBe(false);
		expect(synced('Fake Blackbaud Vendor On Hold')).toBe(false);
	});

	test('a post date in a closed fiscal period is refused before the create', async ({ page }) => {
		// fake-erp's FY 2025 has Jan-Mar closed; FE NXT itself refuses with an
		// uncoded 400, which is what this used to surface as.
		await setLedgerConfig(page, 'USD');
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		expect(await sendExpectingRefusal(page, { invoiceDate: '2025-02-15' })).toBe(
			'Blackbaud FE NXT post refused: period_closed'
		);
	});

	test('a background job that fails is a coded refusal, not a retry loop', async ({ page }) => {
		// fake-erp ends the job at status 7 for this account.
		await setLedgerConfig(page, 'USD');
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		expect(await sendExpectingRefusal(page, { glAccount: '01-5900-00' })).toBe(
			'Blackbaud FE NXT post refused: job_failed'
		);
	});

	test('a busy invoice number (over 500 search hits) still posts', async ({ page, request }) => {
		// `search_text` also matches description, vendor name and PO number, so
		// a common number can match more than a page. The lookup used to give
		// up past 500 rows as "truncated", retryably, on every push.
		await setLedgerConfig(page, 'USD');
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const number = `E2E-BB-BUSY-${Date.now()}`;
		const rows = Array.from({ length: 600 }, (_, i) => ({
			vendor_id: 137,
			invoice_number: `OTHER-${i}`,
			description: `re ${number}`,
			invoice_date: '2026-03-01'
		}));
		const seeded = await request.post(`${BB}/__seed-invoices`, { data: { rows } });
		expect(seeded.ok(), await seeded.text()).toBe(true);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-BB-BUSY',
			invoiceNumber: number,
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			currency: 'USD',
			vendor: SUPPLIER,
			glAccount: '01-5000-00'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an expiring token is refreshed; a revoked consent asks for reconnect', async ({
		page,
		request
	}) => {
		const hook = `${FAKE_ERP_BASE}/__oauth/blackbaud`;
		const refreshes = async () => (await (await request.get(`${hook}/stats`)).json()).refreshes;
		try {
			// Tokens that expire inside the backend's 120 s refresh margin: every
			// use must refresh first. Reconnect so the STORED token is one of them.
			await request.post(`${hook}/lifetime`, { data: { seconds: 60 } });
			await connectBlackbaud(page);
			const before = await refreshes();

			const ok = await testErpConnection(page);
			expect(ok.success, ok.message).toBe(true);
			expect(await refreshes()).toBeGreaterThan(before);
			// The rotated refresh token was persisted: a second refresh works too.
			const mid = await refreshes();
			expect((await testErpConnection(page)).success).toBe(true);
			expect(await refreshes()).toBeGreaterThan(mid);

			// The customer revokes the app at Blackbaud: the refresh is refused,
			// and the connection reads "reconnect required" — never retried
			// forever, never silently mock.
			await request.post(`${hook}/revoke`);
			expect((await testErpConnection(page)).success).toBe(false);
			const status = await oauthStatus(page);
			expect(status.needs_reconnect).toBe(true);
			expect(status.connected).toBe(false);
		} finally {
			await request.post(`${hook}/lifetime`, { data: { seconds: 1800 } });
		}

		// Reconnecting through the consent flow clears it.
		await connectBlackbaud(page);
		const after = await oauthStatus(page);
		expect(after.connected).toBe(true);
		expect(after.needs_reconnect).toBeFalsy();
	});

	test('a token rejected before its expiry is refreshed and the call replayed', async ({
		page,
		request
	}) => {
		const hook = `${FAKE_ERP_BASE}/__oauth/blackbaud`;
		const refreshes = async () => (await (await request.get(`${hook}/stats`)).json()).refreshes;
		// The stored token has ~30 min left, so no proactive refresh is due —
		// but the provider stops accepting it now. Before the shared 401 retry
		// (oauth_base.RefreshOnUnauthorized) every call failed until expiry.
		const before = await refreshes();
		await request.post(`${hook}/revoke-access`);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
		expect(await refreshes()).toBe(before + 1);
		expect((await oauthStatus(page)).connected).toBe(true);
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
