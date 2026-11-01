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
	qboPushLanding,
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	syncAndListGlAccounts,
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
 *   3. Same-named sub-accounts — the fake, like the real sandbox, has a
 *      "Job Materials" under two parents. The chart sync keeps both, each
 *      keyed by its FullyQualifiedName, and an invoice coded to one posts a
 *      Bill on THAT account's id, never the other's (decisions §284).
 *   4. Fail closed — an invoice whose vendor never synced is refused before
 *      anything is posted, and the invoice modal says what to do about it in
 *      the reader's language; so is an invoice number longer than QBO's
 *      21-character DocNumber (refused, never silently truncated).
 *   5. Disconnect — clears the connection, and a later send cannot post.
 *   6. Connect binding — a callback from a browser that did not call
 *      authorize (no nonce cookie) links nothing, and a realmId the new token
 *      cannot reach is refused rather than linked.
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

/** Start a connect in `page`'s browser and return the callback URL the fake
 *  sends back — the step a consent link handed to someone else would reach. */
async function consentCallback(page: Page): Promise<URL> {
	const start = await page.request.get(`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`, {
		headers: await authedTenantHeaders(page)
	});
	expect(start.status(), await start.text()).toBe(200);
	const consent = await page.request.get((await start.json()).authorize_url, { maxRedirects: 0 });
	expect(consent.status()).toBe(302);
	return new URL(consent.headers()['location']);
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

	test('a consent link finished in another browser links nothing', async ({ page, request }) => {
		// The login-CSRF: the admin starts a connect and hands the link on. The
		// callback reaches the API from a browser (`request` — its own cookie
		// jar) that never received the authorize call's nonce cookie.
		const callback = await consentCallback(page);
		const elsewhere = await request.get(callback.href, { maxRedirects: 0 });
		expect(elsewhere.status()).toBe(302);
		expect(new URL(elsewhere.headers()['location']).searchParams.get('erp_error')).toBe(
			'browser_mismatch'
		);
		// The state is spent: not even the starting browser can finish it now.
		const replay = await page.request.get(callback.href, { maxRedirects: 0 });
		expect(new URL(replay.headers()['location']).searchParams.get('erp_error')).toBe(
			'state_expired'
		);
		// The existing connection is untouched.
		expect((await oauthStatus(page)).external_tenant_id).toBe(REALM);
	});

	test('a realmId the new token cannot reach is refused, not linked', async ({ page }) => {
		// QuickBooks' realmId arrives unsigned in the redirect; naming another
		// company's realm must not squat it. The fake answers companyinfo for
		// any other realm with Intuit's 403 (Fault 3100).
		const callback = await consentCallback(page);
		callback.searchParams.set('realmId', 'someone-elses-realm');
		const landed = await page.request.get(callback.href, { maxRedirects: 0 });
		expect(landed.status()).toBe(302);
		expect(new URL(landed.headers()['location']).searchParams.get('erp_error')).toBe(
			'no_external_tenant'
		);
		expect((await oauthStatus(page)).external_tenant_id).toBe(REALM);
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
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe(await qboPushLanding(page));

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

	test('same-named sub-accounts are two accounts, and the bill posts to the one picked', async ({
		page
	}) => {
		await syncErpVendors(page);
		const chart = await syncAndListGlAccounts(page);
		const byCode = new Map(chart.map((a) => [a.code, a]));
		// Keyed by the path QuickBooks keeps unique, never the shared Name.
		expect(byCode.get('Job Expenses:Job Materials')?.name).toBe('Job Expenses:Job Materials');
		expect(byCode.get('Landscaping Services:Job Materials')?.name).toBe(
			'Landscaping Services:Job Materials'
		);
		expect(byCode.has('Job Materials')).toBe(false);

		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBO-SUB',
			invoiceNumber: `S${Date.now() % 1e9}-${Math.floor(Math.random() * 1e4)}`,
			amount: '42.00',
			vendor: 'Fake ERP Vendor A',
			glAccount: 'Job Expenses:Job Materials'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe(await qboPushLanding(page));
			const bill = await billFromFake(page, await erpReferenceFromAudit(page, inv.id));
			expect(
				bill.Line.map(
					(l: { AccountBasedExpenseLineDetail?: { AccountRef?: { value: string } } }) =>
						l.AccountBasedExpenseLineDetail?.AccountRef?.value
				)
			).toEqual(['62']); // Job Expenses:Job Materials — not Landscaping's 63
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

			// The modal states the refusal as something to act on — not the
			// server's "QuickBooks Online post refused: vendor_not_linked" —
			// and keeps the retry for after the fix.
			await page.goto(`/invoices?id=${inv.id}`);
			const error = page.getByTestId('erp-error');
			await expect(error).toHaveText(
				"This vendor isn't in QuickBooks Online yet. Sync vendors from QuickBooks Online, or add the vendor there, then retry."
			);
			await expect(page.getByRole('button', { name: 'Retry ERP Send' })).toBeVisible();

			// ...in the reader's language.
			await page.evaluate(() => localStorage.setItem('feoh_locale', 'de'));
			await page.goto(`/invoices?id=${inv.id}`);
			await expect(page.getByTestId('erp-error')).toHaveText(
				'Dieser Lieferant ist noch nicht in QuickBooks Online. Synchronisieren Sie die Lieferanten aus QuickBooks Online oder legen Sie den Lieferanten dort an und versuchen Sie es dann erneut.'
			);
			await page.evaluate(() => localStorage.removeItem('feoh_locale'));
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

/**
 * What QuickBooks itself refuses a bill for, and the connection test on the
 * connected panel (live sandbox findings, 2026-10-19).
 *
 *   6. The panel's "Test connection" on a connected OAuth ERP tests the saved
 *      connection, and says so naming the ERP — not the backend's key.
 *   7. A bill QuickBooks refuses — books closed through its date, an inactive
 *      vendor, an inactive account, a DocNumber another bill already holds —
 *      fails at once (`retries` 1: a validation answer is deterministic), is
 *      recorded as a coded refusal carrying Intuit's numeric code, and the
 *      invoice modal says what to do about it. The fake answers each with the
 *      Fault code and HTTP 400 Intuit uses (`POST /__qbo/validation`), its
 *      Detail quoting the bill back, which must reach neither the audit row
 *      nor the page.
 */

/** Configure the fake company's validation rules; `{}` keys left out keep. */
async function qboValidation(rules: Record<string, unknown>): Promise<void> {
	const resp = await fetch(`${FAKE_ERP_BASE}/__qbo/validation`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(rules)
	});
	expect(resp.ok).toBe(true);
}

const NO_VALIDATION = {
	closed_through: null,
	inactive_vendors: [],
	inactive_accounts: [],
	duplicate_doc_check: false
};

/** The `invoice.erp_failed` row's details: the coded refusal and the count. */
async function erpFailedRow(page: Page, invoiceId: string) {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	const rows = (await resp.json()) as Array<{
		action: string;
		details: {
			error?: string;
			retries?: number;
			refusal?: { code: string; params: Record<string, unknown> };
		} | null;
	}>;
	const failed = rows.find((r) => r.action === 'invoice.erp_failed');
	expect(failed, 'audit trail has an invoice.erp_failed row').toBeTruthy();
	return failed!.details ?? {};
}

/** Post a bill straight into the fake, as someone working in QuickBooks would. */
async function billInFake(page: Page, docNumber: string, vendorId: string): Promise<void> {
	const qbo = `${FAKE_ERP_BASE}/qbo`;
	const authorize = await page.request.get(`${qbo}/oauth2/authorize`, {
		params: { redirect_uri: ERP_OAUTH_CALLBACK, state: 'spec', client_id: 'fake-qbo-client' },
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
	const created = await page.request.post(`${qbo}/v3/company/${REALM}/bill`, {
		headers: { Authorization: `Bearer ${(await token.json()).access_token}` },
		data: {
			VendorRef: { value: vendorId },
			DocNumber: docNumber,
			Line: [
				{
					DetailType: 'AccountBasedExpenseLineDetail',
					Amount: 5,
					AccountBasedExpenseLineDetail: { AccountRef: { value: '7' } }
				}
			]
		}
	});
	expect(created.ok()).toBe(true);
}

test.describe('/erp quickbooks_online: what QuickBooks refuses, and the panel test', () => {
	test.beforeAll(async () => {
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await connectQbo(page);
		await qboValidation(NO_VALIDATION);
	});

	test.afterAll(async ({ browser }) => {
		await qboValidation(NO_VALIDATION).catch(() => undefined);
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

	test('the connected panel tests the saved connection and names the ERP', async ({ page }) => {
		await page.goto('/organization?section=erp');
		await expect(page.getByTestId('erp-oauth-state')).toContainText('Connected to QuickBooks Online');
		const status = page.getByTestId('erp-status');
		await page.getByTestId('erp-oauth-test').click();
		await expect(status).toHaveText('Connected to QuickBooks Online. The connection test succeeded.');
		await expect(status).toHaveClass(/success/);

		await page.evaluate(() => localStorage.setItem('feoh_locale', 'de'));
		await page.goto('/organization?section=erp');
		await page.getByTestId('erp-oauth-test').click();
		await expect(page.getByTestId('erp-status')).toHaveText(
			'Mit QuickBooks Online verbunden. Der Verbindungstest war erfolgreich.'
		);
		await page.evaluate(() => localStorage.removeItem('feoh_locale'));
	});

	const cases: Array<{
		name: string;
		rules: Record<string, unknown>;
		vendor?: string;
		glAccount?: string;
		code: string;
		params: Record<string, unknown>;
		text: string;
		/** Arrange anything else in QuickBooks before the send. */
		before?: (page: Page, invoiceNumber: string) => Promise<void>;
	}> = [
		{
			name: 'a bill dated into closed books',
			rules: { closed_through: '2026-09-30' },
			code: 'period_closed',
			params: { intuit_code: '6210', provider: 'QuickBooks Online' },
			text: 'QuickBooks Online refused this bill because its date is in a closed period (the books are closed). Change the invoice date or reopen the period in QuickBooks Online, then retry.'
		},
		{
			name: 'a bill for a vendor made inactive in QuickBooks',
			rules: { inactive_vendors: ['57'] },
			vendor: 'Fake ERP Vendor B',
			code: 'vendor_inactive',
			params: { intuit_code: '2500', provider: 'QuickBooks Online' },
			text: 'QuickBooks Online refused this bill because its vendor is inactive or deleted there. Make the vendor active in QuickBooks Online, then retry.'
		},
		{
			name: 'a bill coded to an account made inactive in QuickBooks',
			rules: { inactive_accounts: ['63'] },
			glAccount: 'Landscaping Services:Job Materials',
			code: 'account_inactive',
			params: {
				intuit_code: '2500',
				accounts: ['Landscaping Services:Job Materials'],
				provider: 'QuickBooks Online'
			},
			text: 'QuickBooks Online refused this bill because Landscaping Services:Job Materials is inactive or deleted there. Make the account active in QuickBooks Online, or recode the invoice to an active account, then retry.'
		},
		{
			name: 'a bill whose number another QuickBooks bill already holds',
			rules: { duplicate_doc_check: true },
			code: 'duplicate_document_number',
			params: { intuit_code: '6140', provider: 'QuickBooks Online' },
			text: 'QuickBooks Online already has a bill with this invoice number. Check QuickBooks Online for a duplicate before you retry.',
			before: (page, invoiceNumber) => billInFake(page, invoiceNumber, '56')
		}
	];

	for (const c of cases) {
		test(`${c.name} is refused once, coded, and explained`, async ({ page }) => {
			await syncErpVendors(page);
			await syncErpGlAccounts(page);
			await qboValidation(c.rules);
			const invoiceNumber = `R${Date.now() % 1e9}-${Math.floor(Math.random() * 1e4)}`;
			await c.before?.(page, invoiceNumber);
			const inv = await createApprovedInvoice(page, {
				prefix: 'E2E-QBO-REFUSED',
				invoiceNumber,
				amount: '75.00',
				vendor: c.vendor ?? 'Fake ERP Vendor A',
				glAccount: c.glAccount ?? '6100',
				invoiceDate: '2026-09-15',
				dueDate: '2026-10-15'
			});
			try {
				expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
				const row = await erpFailedRow(page, inv.id);
				// Deterministic: one call, never the three-attempt backoff.
				expect(row.retries).toBe(1);
				expect(row.refusal).toEqual({ code: c.code, params: c.params });
				expect(row.error).toBe(
					`QuickBooks Online post refused: ${c.code} (Intuit fault ${c.params.intuit_code})`
				);

				await page.goto(`/invoices?id=${inv.id}`);
				const error = page.getByTestId('erp-error');
				await expect(error).toHaveText(c.text);
				// Intuit's Detail quotes the bill back; none of it is shown.
				await expect(error).not.toContainText('TxnDate');
				await expect(error).not.toContainText('TxnType');
			} finally {
				await qboValidation(NO_VALIDATION);
				await deleteInvoice(page, inv.id);
			}
		});
	}
});
