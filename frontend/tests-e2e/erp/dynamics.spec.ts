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
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncAndListGlAccounts,
	syncErpGlAccounts,
	syncErpPurchaseOrders,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * dynamics_365_bc adapter e2e — the REAL Dynamics 365 Business Central
 * adapter (backend/app/services/erp_adapters/dynamics_365_bc.py) against the
 * local fake ERP container (tools/fake-erp, `pnpm erp:up`, host port 12112).
 * The committed backend/.env.development points FEOH_ERP_D365_API_BASE and
 * FEOH_ERP_D365_TOKEN_URL at the fake — which is why this config deliberately
 * OMITS `base_url`: the operator env override supplies the API base, so the
 * admin-config field (and its SSRF guard) is never consulted.
 *
 * This suite exercises the OAuth2 client-credentials TOKEN FLOW end-to-end:
 * every adapter call first POSTs the token endpoint (the fake 400s unless
 * grant_type=client_credentials with a non-empty client_id/client_secret),
 * then presents the minted Bearer token (the fake 401s anything else).
 *
 * Coverage:
 *   1. test_connection — token exchange + GET companies(fake-co)/vendors.
 *   2. Chart sync — `accounts` paged by `Prefer: odata.maxpagesize`; the
 *      three Posting accounts land on the chart (the heading and the blocked
 *      account are skipped).
 *   3. PO sync — `purchaseOrders?$expand=purchaseOrderLines`; a blank
 *      `currencyCode` (local currency) stays null, a stated one is kept.
 *   4. Full send — after the vendor and chart syncs store the BC ids, an
 *      approved invoice posts as a purchaseInvoice through the async ERP
 *      dispatch (create 201 → Microsoft.NAV.post finalize) and lands `done`
 *      with a BC-shaped document id (d365-inv-N). The fake 400s a
 *      `vendorId` naming no vendor and a line whose `accountId` is not a
 *      posting account, as BC does, so `done` proves the adapter posted by
 *      the synced ids rather than the name or the account No.
 */

// The exact settings.erp shape the adapter reads: get_erp_adapter passes the
// whole dict to the adapter, which reads tenant_id / client_id /
// client_secret / environment / company_id flat (the same flat shape the
// /organization ERP panel saves). No base_url — see the header comment.
const D365_ERP_CONFIG = {
	type: 'dynamics_365_bc',
	integration_method: 'direct',
	tenant_id: 'fake-tenant',
	client_id: 'fake-client',
	client_secret: 'fake-secret',
	environment: 'sandbox',
	company_id: 'fake-co'
};

test.describe('/erp dynamics_365_bc adapter against fake-erp', () => {
	test.beforeAll(async () => {
		// Deterministic document ids for this run (fake BC ids count up from
		// "d365-inv-1"). Best-effort — when the fake is down the beforeEach
		// gate skips every test with the hint.
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, D365_ERP_CONFIG);
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

	test('connection test succeeds against the fake BC (OAuth2 token flow)', async ({ page }) => {
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
		expect(result.message).toContain('dynamics_365_bc');
	});

	test('chart sync imports the posting accounts', async ({ page }) => {
		const accounts = await syncAndListGlAccounts(page);
		for (const [code, name] of [
			['6100', 'Fake BC Office Supplies'],
			['6200', 'Fake BC Software'],
			['6300', 'Fake BC Consulting']
		]) {
			const match = accounts.find((a) => a.code === code);
			expect(match, `GL account ${code} synced`).toBeTruthy();
			expect(match!.name).toBe(name);
		}
	});

	test('PO sync imports purchase orders, leaving a local-currency total unlabelled', async ({
		page
	}) => {
		const { adapter, pos } = await syncErpPurchaseOrders(page);
		expect(adapter).toBe('dynamics_365_bc');
		const local = pos.find((p) => p.po_number === 'PO-FAKE-BC-401');
		const euro = pos.find((p) => p.po_number === 'PO-FAKE-BC-402');
		expect(local, 'PO-FAKE-BC-401 synced').toBeTruthy();
		expect(euro, 'PO-FAKE-BC-402 synced').toBeTruthy();
		expect(Number(local!.total)).toBeCloseTo(1500.25, 2);
		expect(local!.currency).toBeNull();
		expect(Number(euro!.total)).toBeCloseTo(820, 2);
		expect(euro!.currency).toBe('EUR');
		expect(euro!.status).toBe('open');
	});

	test('full send: approved invoice posts as a purchaseInvoice and completes', async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-D365',
			amount: '3120.40',
			vendor: 'Fake BC Vendor A',
			glAccount: '6100'
		});
		try {
			const terminal = await sendToErpAndAwaitTerminal(page, inv.id);
			expect(terminal).toBe('done');

			// The adapter returned the fake's BC-shaped document id — proof the
			// REAL dynamics_365_bc adapter (token exchange included) performed
			// the post, not the mock.
			const erpRef = await erpReferenceFromAudit(page, inv.id);
			expect(erpRef).toMatch(/^d365-inv-\d+$/);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a VAT company that would book more than was approved is refused, not posted', async ({
		page
	}) => {
		// fake-erp's `fake-vat-co` adds 20% VAT on top of the lines, as a real
		// BC VAT company does: 1,200 approved would become a 1,440 bill. The
		// adapter reads the draft's total, deletes the draft and refuses.
		await setErpSettings(page, { ...D365_ERP_CONFIG, company_id: 'fake-vat-co' });
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-D365-VAT',
			amount: '1200.00',
			vendor: 'Fake BC Vendor A',
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'Business Central post refused: posted_total_mismatch'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
