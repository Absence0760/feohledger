import { createHmac } from 'node:crypto';

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
import { createApprovedInvoice, deleteInvoice, setErpSettings } from './helpers';

/**
 * Inbound ERP webhook e2e — `POST /api/erp/webhook/{erp_type}`
 * (backend/app/api/erp_webhook.py), the ONLY path by which an ERP's status
 * reaches FeohLedger (the polling job is designed, not built — see
 * backend/docs/erp-integration.md). It is public and unauthenticated, so its
 * whole defence is the HMAC over the raw body with the tenant's sealed
 * `webhook_signing_secret`, plus event-id dedup. Real HTTP, real signing, real
 * tenant DB — no fake ERP needed, which is why there is no reachability gate.
 *
 * Coverage:
 *   1. Forged — an unsigned event, a wrongly signed one and one naming an
 *      unknown tenant all get the same bodyless 204 and change nothing.
 *   2. Paid — a correctly signed `paid` for an approved invoice FeohLedger
 *      never paid records exactly one completed external payment (reference =
 *      the ERP document id) and moves the invoice to `paid`.
 *   3. Replay — the same event id delivered again is deduplicated: still one
 *      payment.
 */

const SECRET = 'e2e-erp-webhook-secret';
const ERP_TYPE = 'netsuite';

// Any live ERP config carries the secret; NetSuite's needs no fake to save.
const ERP_CONFIG = {
	type: 'netsuite',
	integration_method: 'direct',
	account_id: 'FAKE123',
	consumer_key: 'fake-consumer-key',
	consumer_secret: 'fake-consumer-secret',
	token_id: 'fake-token-id',
	token_secret: 'fake-token-secret',
	webhook_signing_secret: SECRET
};

type Event = Record<string, unknown>;

async function deliver(
	request: APIRequestContext,
	event: Event,
	signature: 'valid' | 'wrong' | 'none' = 'valid'
) {
	const raw = JSON.stringify(event);
	const headers: Record<string, string> = { 'Content-Type': 'application/json' };
	if (signature !== 'none') {
		const key = signature === 'valid' ? SECRET : 'not-the-secret';
		headers['X-Webhook-Signature'] = createHmac('sha256', key).update(raw).digest('hex');
	}
	return request.post(`${API_BASE}/api/erp/webhook/${ERP_TYPE}`, { headers, data: raw });
}

async function invoiceState(page: Page, id: string) {
	const headers = await authedTenantHeaders(page);
	const inv = await (await page.request.get(`${API_BASE}/api/invoices/${id}`, { headers })).json();
	const payments = await (
		await page.request.get(`${API_BASE}/api/payments`, { headers, params: { invoice_id: id } })
	).json();
	return { status: inv.status as string, correlation: inv.correlation_id as string, payments };
}

test.describe('/erp inbound webhook', () => {
	test.beforeEach(async ({ page }) => {
		await setErpSettings(page, ERP_CONFIG);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErpSettings(page, null);
		await context.close();
	});

	test('forged, unsigned and unknown-tenant events change nothing', async ({
		page,
		request
	}) => {
		const inv = await createApprovedInvoice(page, { prefix: 'E2E-WH-FORGED' });
		try {
			const { correlation } = await invoiceState(page, inv.id);
			const event = (overrides: Event = {}): Event => ({
				tenant_slug: currentTenantSlug(),
				correlation_id: correlation,
				erp_document_id: `WH-${Date.now()}`,
				event_id: `evt-${Date.now()}-${Math.random()}`,
				status: 'paid',
				...overrides
			});
			const responses = [
				await deliver(request, event(), 'none'),
				await deliver(request, event(), 'wrong'),
				await deliver(request, event({ tenant_slug: 'no-such-tenant' }))
			];
			// Identical, bodyless answers: nothing to enumerate tenants or state by.
			for (const resp of responses) {
				expect(resp.status()).toBe(204);
				expect(await resp.text()).toBe('');
			}
			const after = await invoiceState(page, inv.id);
			expect(after.status).toBe('approved');
			expect(after.payments.total).toBe(0);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a signed paid event records one external payment; a replay records none', async ({
		page,
		request
	}) => {
		const inv = await createApprovedInvoice(page, { prefix: 'E2E-WH-PAID', amount: '412.37' });
		const documentId = `WH-DOC-${Date.now()}`;
		const event: Event = {
			tenant_slug: currentTenantSlug(),
			correlation_id: (await invoiceState(page, inv.id)).correlation,
			erp_document_id: documentId,
			event_id: `evt-paid-${Date.now()}`,
			status: 'paid'
		};
		try {
			expect((await deliver(request, event)).status()).toBe(204);
			const paid = await invoiceState(page, inv.id);
			expect(paid.status).toBe('paid');
			expect(paid.payments.total).toBe(1);
			const [payment] = paid.payments.items;
			expect(payment.status).toBe('completed');
			expect(payment.provider).toBe('external');
			expect(Number(payment.amount)).toBe(412.37);
			expect(JSON.stringify(payment)).toContain(documentId);

			// The ERP retries the same delivery: deduplicated by event id.
			expect((await deliver(request, event)).status()).toBe(204);
			expect((await invoiceState(page, inv.id)).payments.total).toBe(1);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
