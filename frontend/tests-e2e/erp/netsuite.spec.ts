import { generateKeyPairSync } from 'node:crypto';
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
	erpFailureFromAudit,
	erpReferenceFromAudit,
	getInvoice,
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpPurchaseOrders,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * netsuite adapter e2e — the REAL NetSuite SuiteTalk REST adapter
 * (backend/app/services/erp_adapters/netsuite.py) against the local fake ERP
 * container (tools/fake-erp, `pnpm erp:up`, host port 12112). The committed
 * backend/.env.development points FEOH_ERP_NETSUITE_API_BASE at the fake
 * (instead of deriving the per-account suitetalk.api.netsuite.com URL from
 * account_id), so the adapter's real OAuth 1.0 TBA header construction +
 * httpx calls run end-to-end with no NetSuite account.
 *
 * Coverage:
 *   1. test_connection — GET /vendor?limit=1 with the full `Authorization:
 *      OAuth ...` TBA header (consumer key/token/nonce/HMAC signature); the
 *      fake 401s any request missing the OAuth params.
 *   2. Full send — after the vendor + chart syncs (the only writers of the
 *      ERP ids a bill is posted against), an approved invoice posts as a
 *      vendorBill through the async ERP dispatch. The fake 400s a bill whose
 *      `entity` or expense-line `account` is not a known internal id, exactly
 *      as NetSuite does, so `done` proves the adapter posted by id. It answers
 *      204 + a Location header, and the adapter parses the record id out of it.
 *   3. Fail closed — an invoice whose vendor never synced is refused with the
 *      stable `vendor_not_linked` reason before any request reaches NetSuite;
 *      it is never posted by name.
 *   4. PO sync — one SuiteQL query over `transaction` (type PurchOrd); the
 *      status letter maps onto open / closed and the ISO currency is kept.
 *      SuiteQL states the header total in posting sign (negative); it is
 *      stored at the UI's positive figure, and a live PO's lines come from a
 *      second query over `transactionline` (decisions §398).
 *   5. Posted total — on the fake's 10%-taxed account 6400 NetSuite books more
 *      than was approved; the adapter reads the bill's `total` back and fails
 *      `posted_total_mismatch`, deleting the bill only while it is Pending
 *      Approval (vendor "Fake NetSuite Vendor Routed").
 *   6. Posted means approved — an untaxed bill for the routed vendor starts
 *      Pending Approval, so the push is refused `bill_not_posted` and the bill
 *      is left; once approved in NetSuite, a retry finds it by `eid:` and
 *      completes without a second bill.
 *   7. Coded refusals — a bill dated in the fake's closed January 2020 is
 *      refused by NetSuite and coded `period_closed` from the period itself.
 *
 * The vendor sync is SuiteQL too (`SELECT … FROM vendor`): the REST `/vendor`
 * collection carries no names. The full send proves it stored the ids.
 */

// The exact settings.erp shape the adapter reads: get_erp_adapter passes the
// whole dict to the adapter, which reads account_id / consumer_key /
// consumer_secret / token_id / token_secret flat (the same flat shape the
// /organization ERP panel saves). All values are fakes — the fake ERP checks
// OAuth-header shape, not signatures.
const NETSUITE_ERP_CONFIG = {
	type: 'netsuite',
	integration_method: 'direct',
	account_id: 'FAKE123',
	consumer_key: 'fake-consumer-key',
	consumer_secret: 'fake-consumer-secret',
	token_id: 'fake-token-id',
	token_secret: 'fake-token-secret'
};

/** The id of the bill a refused push left in NetSuite, as the failure's
 *  append-only `invoice.erp_failed` row records it (`erp_document_id`). */
async function leftBillId(page: Page, invoiceId: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	const rows = (await resp.json()) as Array<{
		action: string;
		details: { erp_document_id?: string } | null;
	}>;
	const id = rows.find((r) => r.action === 'invoice.erp_failed')?.details?.erp_document_id;
	expect(id, 'the refused push names the bill it left').toMatch(/^\d+$/);
	return id!;
}

test.describe('/erp netsuite adapter against fake-erp', () => {
	test.beforeAll(async () => {
		// Deterministic document ids for this run (fake NetSuite ids count up
		// from "1001"). Best-effort — when the fake is down the beforeEach
		// gate skips every test with the hint.
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, NETSUITE_ERP_CONFIG);
	});

	// Other suites (e.g. payments/execute) assume the seeded org has NO ERP
	// configured — `dispatch_payment_sync` only runs the invoice → paid
	// auto-bump when erp_config is present. Same contract as
	// purchase-orders/sync.spec.ts.
	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		const headers = await authedTenantHeaders(page);
		await page.request.patch(`${API_BASE}/api/organization`, {
			headers: { ...headers, 'Content-Type': 'application/json' },
			data: { settings: { erp: null } }
		});
		await context.close();
	});

	test('connection test succeeds against the fake NetSuite (TBA header accepted)', async ({
		page
	}) => {
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
		expect(result.message).toContain('netsuite');
	});

	test('full send: approved invoice posts as a vendorBill and completes', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS',
			amount: '2450.75',
			vendor: 'Fake NetSuite Vendor A',
			glAccount: '6100'
		});
		try {
			const terminal = await sendToErpAndAwaitTerminal(page, inv.id);
			expect(terminal).toBe('done');

			// NetSuite returns 204 + the record URL in the Location header; the
			// adapter extracts the trailing id — the fake mints numeric-string
			// ids ("1001", "1002", ...). A numeric id proves the REAL netsuite
			// adapter (not the mock) parsed the Location contract.
			const erpRef = await erpReferenceFromAudit(page, inv.id);
			expect(erpRef).toMatch(/^\d+$/);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted by name', async ({
		page
	}) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-UNLINKED',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'NetSuite post refused: vendor_not_linked'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	// The fake's account 6400 carries a 10% tax code (NETSUITE_ACCOUNT_TAX_RATES),
	// so NetSuite books 2695.83 for an approved 2450.75. The adapter reads the
	// created bill's `total` back and never reports that as posted.
	test('a bill NetSuite totals differently is not posted (approved bill left alone)', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-TAXED',
			amount: '2450.75',
			vendor: 'Fake NetSuite Vendor A',
			glAccount: '6400'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			// Vendor A's bills are created Open (approved): never deleted.
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'NetSuite post failed: posted_total_mismatch (the bill could not be deleted in ' +
					'NetSuite: it is no longer pending approval or NetSuite refused)'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill NetSuite totals differently is deleted while pending approval', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-TAXED-ROUTED',
			amount: '2450.75',
			// Under approval routing in the fake: its bills start Pending Approval.
			vendor: 'Fake NetSuite Vendor Routed',
			glAccount: '6400'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'NetSuite post failed: posted_total_mismatch (the bill was deleted)'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('PO sync imports purchase orders through SuiteQL', async ({ page }) => {
		const { adapter, pos } = await syncErpPurchaseOrders(page);
		expect(adapter).toBe('netsuite');
		const open = pos.find((p) => p.po_number === 'PO-FAKE-NS-501');
		const closed = pos.find((p) => p.po_number === 'PO-FAKE-NS-502');
		expect(open, 'PO-FAKE-NS-501 synced').toBeTruthy();
		expect(closed, 'PO-FAKE-NS-502 synced').toBeTruthy();
		expect(Number(open!.total)).toBeCloseTo(2100.5, 2);
		expect(open!.currency).toBe('USD');
		expect(open!.status).toBe('open');
		expect(closed!.status).toBe('closed');
		expect(closed!.currency).toBe('GBP');
		// The fake states both headers negative, as SuiteQL does.
		expect(Number(closed!.total)).toBeCloseTo(640, 2);

		// The live PO's lines came from `transactionline`: an item line and
		// an expense line, 1,500.50 + 600.00.
		const list = await page.request.get(`${API_BASE}/api/purchase-orders?page_size=100`, {
			headers: await authedTenantHeaders(page)
		});
		expect(list.status()).toBe(200);
		const { items } = (await list.json()) as {
			items: Array<{ po_number: string; line_items: Array<{ total: number | string }> }>;
		};
		const withLines = items.find((p) => p.po_number === 'PO-FAKE-NS-501');
		expect(withLines?.line_items.map((l) => Number(l.total)).sort((a, b) => a - b)).toEqual([
			600, 1500.5
		]);
	});

	test('a bill pending approval is not posted, and completes once approved in NetSuite', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-ROUTED',
			amount: '410.20',
			// Under approval routing in the fake: its bills start Pending Approval.
			vendor: 'Fake NetSuite Vendor Routed',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'NetSuite post refused: bill_not_posted ' +
					'(the bill is in NetSuite and was left there: NetSuite does not show it approved)'
			);

			// The approver acts in NetSuite; the retry finds the same bill.
			const billId = await leftBillId(page, inv.id);
			const approved = await fetch(`${FAKE_ERP_BASE}/__set-status`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ provider: 'netsuite', id: billId, status: 'open' })
			});
			expect(approved.ok, `approve the bill in the fake (${approved.status})`).toBe(true);

			const retried = await page.request.post(`${API_BASE}/api/invoices/${inv.id}/retry-erp`, {
				headers: await authedTenantHeaders(page),
				data: {}
			});
			expect(retried.status(), 'retry-erp accepted').toBe(202);
			await expect
				.poll(async () => (await getInvoice(page, inv.id)).status, { timeout: 20_000 })
				.toBe('done');
			expect(await erpReferenceFromAudit(page, inv.id)).toBe(billId);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill dated in a closed period is coded period_closed', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-CLOSED',
			amount: '75.00',
			vendor: 'Fake NetSuite Vendor A',
			glAccount: '6100',
			// The fake's January 2020 is closed (NETSUITE_SUITEQL_PERIOD_FIXTURES).
			invoiceDate: '2020-01-15',
			dueDate: '2020-02-14'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'NetSuite post refused: period_closed (HTTP 400, invalid_request)'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

/**
 * OAuth 2.0 client credentials (M2M) — the sign-in NetSuite requires of every
 * NEW integration from 2027.1 (issue #653). The fake's token endpoint verifies
 * the adapter's JWT client assertion as NetSuite does, signature included
 * (tools/fake-erp/netsuite_oauth2.py), against a certificate this spec maps the
 * way an administrator would: only the PUBLIC key goes to the fake, and the
 * private key is sealed through `PUT /api/organization/credentials/erp` like
 * any other ERP secret. The key pair is generated per run — no key is
 * committed anywhere.
 */
test.describe('/erp netsuite adapter: OAuth 2.0 client credentials', () => {
	const certificateId = `e2e-ns-cert-${Date.now()}`;
	const clientId = `e2e-ns-client-${Date.now()}`;
	const { publicKey, privateKey } = generateKeyPairSync('ec', {
		namedCurve: 'P-256',
		publicKeyEncoding: { type: 'spki', format: 'pem' },
		privateKeyEncoding: { type: 'pkcs8', format: 'pem' }
	});

	async function mintedTokens(): Promise<number> {
		const resp = await fetch(`${FAKE_ERP_BASE}/__netsuite/oauth2/stats`);
		return ((await resp.json()) as { minted: number }).minted;
	}

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		const mapped = await fetch(`${FAKE_ERP_BASE}/__netsuite/oauth2/certificates`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				certificate_id: certificateId,
				client_id: clientId,
				public_key_pem: publicKey
			})
		});
		expect(mapped.ok, `map the certificate (${mapped.status})`).toBe(true);
		await setErpSettings(page, {
			type: 'netsuite',
			integration_method: 'direct',
			account_id: 'FAKE123',
			auth_method: 'oauth2_m2m',
			client_id: clientId,
			certificate_id: certificateId,
			private_key: privateKey
		});
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErpSettings(page, null);
		await context.close();
	});

	test('connection test signs in with a verified client assertion, and re-mints after a 401', async ({
		page
	}) => {
		const before = await mintedTokens();
		const first = await testErpConnection(page);
		expect(first.success, first.message).toBe(true);
		expect(await mintedTokens()).toBeGreaterThan(before);

		// NetSuite revokes / expires the bearer early: the next call's 401 makes
		// the adapter exchange a fresh assertion rather than fail.
		await fetch(`${FAKE_ERP_BASE}/__netsuite/oauth2/expire-tokens`, { method: 'POST' });
		const afterExpiry = await mintedTokens();
		const second = await testErpConnection(page);
		expect(second.success, second.message).toBe(true);
		expect(await mintedTokens()).toBeGreaterThan(afterExpiry);
	});

	test('full send: an approved invoice posts as a vendorBill under the bearer token', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NS-OAUTH2',
			amount: '310.40',
			vendor: 'Fake NetSuite Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			expect(await erpReferenceFromAudit(page, inv.id)).toMatch(/^\d+$/);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
