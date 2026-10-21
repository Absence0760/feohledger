import { createHmac, randomUUID } from 'node:crypto';

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
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	syncErpGlAccounts,
	syncErpVendors
} from './helpers';

/**
 * QuickBooks Online status sync-back and the connection-expiry notice, end to
 * end against the fake ERP (decisions §295):
 *
 *   1. A bill paid IN QUICKBOOKS (a person, not FeohLedger) reaches FeohLedger
 *      through Intuit's signed CloudEvents webhook, and the invoice — left
 *      payable at `posted_in_erp` by the push — moves to `paid`, so it can
 *      never be paid a second time from our payment runs.
 *   2. A bill DELETED in QuickBooks opens the payment-blocking
 *      `payment_reconciliation` exception instead of being ignored.
 *   3. A forged webhook (wrong signature) changes nothing.
 *   4. A connection whose refresh token expires soon says so on the ERP
 *      settings panel and offers Reconnect.
 *
 * Needs the backend started with `FEOH_ERP_STATUS_SYNC_ENABLED=true` (CI's
 * `erp-e2e` job sets it; locally put it in backend/.env) and the committed
 * dev verifier token; the spec skips, saying so, when status sync is off.
 */

const PROVIDER = 'quickbooks_online';
const REALM = 'fake-realm-1';
/** backend/.env.development's FEOH_ERP_QBO_WEBHOOK_VERIFIER_TOKEN. */
const VERIFIER_TOKEN = process.env.E2E_QBO_WEBHOOK_VERIFIER_TOKEN ?? 'dev-qbo-webhook-verifier';

async function connectQbo(page: Page): Promise<void> {
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

/** Deliver a CloudEvents webhook the way Intuit does: the raw body signed
 *  with the app's verifier token, base64 HMAC-SHA256 in `intuit-signature`. */
async function deliverWebhook(
	page: Page,
	billId: string,
	opts: { type?: string; token?: string } = {}
): Promise<void> {
	const body = JSON.stringify([
		{
			specversion: '1.0',
			id: randomUUID(),
			source: 'intuit.e2e',
			type: opts.type ?? 'qbo.bill.updated.v1',
			time: new Date().toISOString(),
			intuitentityid: billId,
			intuitaccountid: REALM,
			data: {}
		}
	]);
	const signature = createHmac('sha256', opts.token ?? VERIFIER_TOKEN)
		.update(body)
		.digest('base64');
	const resp = await page.request.post(`${API_BASE}/api/erp/webhook/quickbooks`, {
		headers: { 'Content-Type': 'application/json', 'intuit-signature': signature },
		data: body
	});
	// Every outcome answers 204: the response never says what happened.
	expect(resp.status()).toBe(204);
}

async function fakeHook(page: Page, path: string, data: Record<string, unknown>): Promise<void> {
	const resp = await page.request.post(`${FAKE_ERP_BASE}${path}`, { data });
	expect(resp.ok(), `${path}: ${resp.status()}`).toBe(true);
}

async function postedInvoice(page: Page, prefix: string) {
	await syncErpVendors(page);
	await syncErpGlAccounts(page);
	const inv = await createApprovedInvoice(page, {
		prefix,
		invoiceNumber: `${prefix[0]}${Date.now() % 1e9}-${Math.floor(Math.random() * 1e3)}`,
		amount: '321.09',
		vendor: 'Fake ERP Vendor A',
		glAccount: '6100'
	});
	// With status sync on, a confirmed push leaves the bill payable here.
	expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('posted_in_erp');
	return { inv, billId: await erpReferenceFromAudit(page, inv.id) };
}

test.describe('/erp quickbooks_online status sync-back against fake-erp', () => {
	test.beforeAll(async () => {
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		test.skip(
			!(await qboStatusSyncEnabled(page)),
			'QuickBooks status sync is off — start the backend with FEOH_ERP_STATUS_SYNC_ENABLED=true'
		);
		await connectQbo(page);
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

	test('a bill paid in QuickBooks moves the invoice to paid', async ({ page }) => {
		const { inv, billId } = await postedInvoice(page, 'PAIDQ');
		try {
			await fakeHook(page, '/__qbo/pay-bill', { id: billId, pay_type: 'CreditCard' });
			await deliverWebhook(page, billId);
			await expect
				.poll(async () => (await getInvoice(page, inv.id)).status, {
					message: 'the QuickBooks payment never reached the invoice'
				})
				.toBe('paid');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a forged webhook changes nothing', async ({ page }) => {
		const { inv, billId } = await postedInvoice(page, 'FORGE');
		try {
			await fakeHook(page, '/__qbo/pay-bill', { id: billId });
			await deliverWebhook(page, billId, { token: 'not-the-verifier-token' });
			// Positive control: a correctly signed delivery for the same bill
			// does apply, so the forged one was refused rather than slow.
			expect((await getInvoice(page, inv.id)).status).toBe('posted_in_erp');
			await deliverWebhook(page, billId);
			await expect.poll(async () => (await getInvoice(page, inv.id)).status).toBe('paid');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill deleted in QuickBooks blocks paying the invoice', async ({ page }) => {
		const { inv, billId } = await postedInvoice(page, 'DELQ');
		try {
			await fakeHook(page, '/__qbo/delete-bill', { id: billId });
			await deliverWebhook(page, billId, { type: 'qbo.bill.deleted.v1' });
			const headers = await authedTenantHeaders(page);
			await expect
				.poll(async () => {
					const resp = await page.request.get(`${API_BASE}/api/exceptions`, {
						headers,
						params: { type: 'payment_reconciliation', search: inv.invoice_number }
					});
					const body = (await resp.json()) as { items?: { invoice_id: string | null }[] };
					return (body.items ?? []).some((e) => e.invoice_id === inv.id);
				})
				.toBe(true);
			expect((await getInvoice(page, inv.id)).status).toBe('posted_in_erp');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a connection about to expire says so and offers Reconnect', async ({ page }) => {
		// Ten days of refresh-token lifetime from the next token response on.
		await fakeHook(page, '/__qbo/token-lifetime', { seconds: 10 * 86400 });
		try {
			await connectQbo(page);
			const status = await (
				await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
					headers: await authedTenantHeaders(page)
				})
			).json();
			expect(status.expires_soon).toBe(true);

			await page.goto('/organization?section=erp');
			await expect(page.getByTestId('erp-oauth-expiring')).toBeVisible();
			await expect(page.getByTestId('erp-oauth-reconnect')).toBeEnabled();
		} finally {
			await fakeHook(page, '/__qbo/token-lifetime', { seconds: 8726400 });
		}
	});
});
