import type { APIRequestContext } from '@playwright/test';

import { currentTenantSlug, expect, signInAndWait, tenantBase, test } from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpPurchaseOrders,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * sage_intacct adapter e2e — the REAL Sage Intacct REST adapter
 * (backend/app/services/erp_adapters/sage_intacct.py) against fake-erp's
 * `/intacct` surface (tools/fake-erp, `pnpm erp:up`). The committed
 * backend/.env.development points FEOH_ERP_INTACCT_API_BASE at the fake.
 *
 * Intacct authenticates with an OAuth client-credentials grant for
 * `user_id@company_id` on every send, so a passing connection test proves the
 * adapter built that username and the fake accepted the client.
 *
 * Coverage:
 *   1. Connection — the client-credentials token is issued and a query runs.
 *   2. Full send — after the vendor + GL syncs an approved invoice posts as an
 *      AP bill; the bill read back FROM THE FAKE carries the exact total as a
 *      decimal STRING (the fake refuses a numeric `txnAmount`, as Intacct
 *      does), the synced vendor id, our bill number, an idempotency reference,
 *      and state `posted` (the fake, like Intacct, makes a bill created
 *      without `state` a draft that books nothing).
 *   3. Fail closed — an unlinked vendor is refused before anything is posted.
 *   4. A create Intacct refuses (a closed period) is the coded `bill_refused`.
 *   5. PO sync — purchasing documents import with exact totals, mapped states
 *      and currency (the fake 400s a query naming a field the object lacks,
 *      such as the `txnTotal` the adapter once asked for).
 */

const INTACCT_ERP_CONFIG = {
	type: 'sage_intacct',
	integration_method: 'direct',
	client_id: 'e2e-intacct-client',
	client_secret: 'e2e-intacct-secret',
	company_id: 'E2E-CO',
	user_id: 'e2e-ws-user'
};

const BASE = `${FAKE_ERP_BASE}/intacct/ia/api/v1`;

/** A bill straight from the fake, with a client-credentials token of our own. */
async function intacctBill(request: APIRequestContext, key: string) {
	const token = await request.post(`${BASE}/oauth2/token`, {
		form: {
			grant_type: 'client_credentials',
			client_id: 'spec',
			client_secret: 'spec',
			username: 'spec@spec'
		}
	});
	expect(token.ok(), await token.text()).toBe(true);
	const resp = await request.get(`${BASE}/objects/accounts-payable/bill/${key}`, {
		headers: { Authorization: `Bearer ${(await token.json()).access_token}` }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return (await resp.json())['ia::result'];
}

test.describe('/erp sage_intacct adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, INTACCT_ERP_CONFIG);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErpSettings(page, null);
		await context.close();
	});

	test('connection test obtains a client-credentials token', async ({ page }) => {
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the bill in Intacct carries the exact total', async ({ page, request }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-INTACCT',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '2450.75',
			vendor: 'Fake Intacct Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			const bill = await intacctBill(request, await erpReferenceFromAudit(page, inv.id));
			expect(bill.totalTxnAmount).toBe('2450.75');
			expect(bill.vendor.id).toBe('V-ACME');
			expect(bill.billNumber).toBe(inv.invoice_number);
			// The idempotency key the adapter looks a retry up by.
			expect(bill.referenceNumber).toBeTruthy();
			expect(bill.state).toBe('posted');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-INTACCT-UNLINKED',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'Sage Intacct post refused: vendor_not_linked'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill Intacct refuses (closed period) is a coded refusal, not "possibly posted"', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		// fake-erp answers a bill dated before 2020-01-01 with Intacct's 422
		// business-validation refusal, as a closed period does.
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-INTACCT-CLOSED',
			invoiceDate: '2019-06-01',
			dueDate: '2019-06-30',
			amount: '75.00',
			vendor: 'Fake Intacct Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'Sage Intacct post refused: bill_refused'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('PO sync imports purchasing documents with exact totals', async ({ page }) => {
		const { adapter, pos } = await syncErpPurchaseOrders(page);
		expect(adapter).toBe('sage_intacct');
		const open = pos.find((p) => p.po_number === 'PO-INTACCT-401');
		const closed = pos.find((p) => p.po_number === 'PO-INTACCT-402');
		expect(open, 'PO-INTACCT-401 synced').toBeTruthy();
		expect(closed, 'PO-INTACCT-402 synced').toBeTruthy();
		expect(String(open!.total)).toMatch(/^1250(\.0+)?$/);
		expect(String(closed!.total)).toMatch(/^980\.50*$/);
		expect(open!.currency).toBe('USD');
		expect(open!.status).toBe('open');
		expect(closed!.status).toBe('closed');
	});
});
