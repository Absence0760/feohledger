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
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpReferenceFromAudit,
	getInvoice,
	qboStatusSyncEnabled,
	sendToErpAndAwaitTerminal,
	syncErpGlAccounts,
	syncErpVendors
} from './helpers';

/**
 * Xero's channels back into FeohLedger, end to end against the fake ERP
 * (decisions §386, §392):
 *
 *   1. A bill paid IN XERO reaches FeohLedger through Xero's signed invoice
 *      webhook, and the invoice, left payable at `posted_in_erp` by the push,
 *      moves to `paid`. The delivery is fake-erp's own (`POST
 *      /xero/__webhook`: Xero's envelope, signed with the committed dev key).
 *   2. A forged delivery (another key) answers 401 and changes nothing.
 *   3. Xero's intent-to-receive check: the signed, empty delivery answers 200
 *      and the forged one 401, both bodyless — what Xero needs before it
 *      enables the webhook.
 *   4. The ERP settings panel's "Check payments now" runs the on-request pass
 *      and says so.
 *
 * Needs the backend started with `FEOH_ERP_STATUS_SYNC_ENABLED=true` (CI's
 * `erp-e2e` job sets it; locally put it in backend/.env) and the committed dev
 * webhook key; the spec skips, saying so, when status sync is off.
 */

const PROVIDER = 'xero';
const SUPPLIER = 'Fake Xero Supplier Co';

async function connectXero(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	const consent = await page.request.get(authorize_url, { maxRedirects: 0 });
	const landed = await page.request.get(consent.headers()['location'], { maxRedirects: 0 });
	expect(new URL(landed.headers()['location']).searchParams.get('erp_error')).toBeNull();
}

/** A delivery fake-erp built and signed the way Xero does. */
async function fakeDelivery(
	page: Page,
	data: Record<string, unknown>
): Promise<{ body: string; signature: string }> {
	const resp = await page.request.post(`${FAKE_ERP_BASE}/xero/api.xro/2.0/__webhook`, { data });
	expect(resp.ok(), await resp.text()).toBe(true);
	return resp.json();
}

/** POST it to the backend verbatim (re-serialising would change the signed
 *  bytes) and return the status, after checking the answer has no body. */
async function deliver(page: Page, delivery: { body: string; signature: string }): Promise<number> {
	const resp = await page.request.post(`${API_BASE}/api/erp/webhook/xero`, {
		headers: { 'Content-Type': 'application/json', 'x-xero-signature': delivery.signature },
		data: delivery.body
	});
	expect(await resp.text()).toBe('');
	return resp.status();
}

async function fakeHook(page: Page, path: string, data: Record<string, unknown>): Promise<void> {
	const resp = await page.request.post(`${FAKE_ERP_BASE}/xero/api.xro/2.0${path}`, { data });
	expect(resp.ok(), `${path}: ${resp.status()}`).toBe(true);
}

async function postedInvoice(page: Page, prefix: string) {
	await syncErpVendors(page);
	await syncErpGlAccounts(page);
	// The Xero spec's own figures, so this vendor's payment history stays one
	// shape across the two specs (the stat-anomaly fraud rule reads it).
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
	// With status sync on, a confirmed push leaves the bill payable here.
	expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('posted_in_erp');
	return { inv, billId: await erpReferenceFromAudit(page, inv.id) };
}

test.describe('/erp xero status sync-back against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		test.skip(
			!(await qboStatusSyncEnabled(page)),
			'Status sync is off — start the backend with FEOH_ERP_STATUS_SYNC_ENABLED=true'
		);
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

	test('a bill paid in Xero moves the invoice to paid through the webhook', async ({ page }) => {
		const { inv, billId } = await postedInvoice(page, 'E2E-XWH');
		try {
			await fakeHook(page, '/__pay', { id: billId });
			expect(await deliver(page, await fakeDelivery(page, { id: billId }))).toBe(200);
			await expect
				.poll(async () => (await getInvoice(page, inv.id)).status, {
					message: 'the Xero payment never reached the invoice'
				})
				.toBe('paid');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a forged delivery answers 401 and changes nothing', async ({ page }) => {
		const { inv, billId } = await postedInvoice(page, 'E2E-XFG');
		try {
			await fakeHook(page, '/__pay', { id: billId });
			const forged = await fakeDelivery(page, { id: billId, key: 'not-the-webhook-key' });
			expect(await deliver(page, forged)).toBe(401);
			expect((await getInvoice(page, inv.id)).status).toBe('posted_in_erp');
			// Positive control: the correctly signed delivery for the same bill
			// applies, so the forged one was refused rather than slow.
			expect(await deliver(page, await fakeDelivery(page, { id: billId }))).toBe(200);
			await expect.poll(async () => (await getInvoice(page, inv.id)).status).toBe('paid');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test("Xero's intent-to-receive check gets 200 signed and 401 forged", async ({ page }) => {
		expect(await deliver(page, await fakeDelivery(page, { intent_to_receive: true }))).toBe(200);
		const forged = await fakeDelivery(page, { intent_to_receive: true, key: 'not-the-key' });
		expect(await deliver(page, forged)).toBe(401);
	});

	test('the ERP panel runs a payment check on request and says so', async ({ page }) => {
		const status = await (
			await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
				headers: await authedTenantHeaders(page)
			})
		).json();
		expect(status.status_sync_available).toBe(true);

		await page.goto('/organization?section=erp');
		const button = page.getByTestId('erp-check-payments');
		await expect(button).toBeEnabled();
		const answered = page.waitForResponse(
			(r) => r.url().endsWith('/api/organization/erp/oauth/status-sync') && r.request().method() === 'POST'
		);
		await button.click();
		const resp = await answered;
		const result = page.getByTestId('erp-status');
		// The server's answer decides the sentence: 202 started (or already
		// running), or the limiter's 429 once this tenant has run four this
		// hour (a re-run of the spec within the hour) — each stated exactly.
		if (resp.status() === 202) {
			const { started } = (await resp.json()) as { started: boolean };
			await expect(result).toContainText(
				started ? 'Checking Xero for payments made there.' : 'A check of Xero is already running.'
			);
		} else {
			expect(resp.status()).toBe(429);
			await expect(result).toContainText('Too many attempts.');
		}
	});
});
