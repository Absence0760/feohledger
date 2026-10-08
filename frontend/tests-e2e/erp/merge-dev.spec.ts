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
	erpReferenceFromAudit,
	resetFakeErp,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * merge_dev adapter e2e — the REAL Merge.dev unified-accounting adapter
 * (backend/app/services/erp_adapters/merge_dev.py) against the local fake ERP
 * container (tools/fake-erp, `pnpm erp:up`, host port 12112). The committed
 * backend/.env.development points FEOH_ERP_MERGE_API_BASE at the fake, so the
 * adapter's real httpx calls (auth headers, cursor pagination, payload
 * mapping) run end-to-end with no Merge.dev account.
 *
 * Coverage:
 *   1. test_connection — GET /account-details with the Bearer api_key +
 *      X-Account-Token headers the adapter sends.
 *   2. PO sync — the fake serves its three fixed POs across TWO cursor pages
 *      (page 1 = 2 results + next cursor), so importing all three asserts the
 *      adapter's cursor-following end-to-end; a second sync inserts 0.
 *   3. GL-account sync — imports the fake chart (6100/6200/6300), also
 *      cursor-paginated.
 *   4. Vendor sync — the real `list_vendors()` (issue #256) against the
 *      fake's three fixed vendors, also cursor-paginated 2 + 1; asserts the
 *      full `_merge_vendor_to_payload` field mapping (name, email, phone,
 *      address, tax id, payment terms — including the bare-string
 *      `payment_term` shape one fixture exercises), not just that the sync
 *      call didn't error.
 *   5. Full send — after the vendor sync stores the vendor's Merge id, an
 *      approved invoice posts through the async ERP dispatch with that id as
 *      its `contact` (the fake 400s an unknown one) and lands `done` with a
 *      Merge-shaped erp_document_id (merge-inv-N).
 */

// FIXED fixtures served by tools/fake-erp/app.py — the fake's README marks
// them "do not change"; these literals are the contract.
const FAKE_POS = [
	{ number: 'PO-FAKE-301', total: 1250.0 },
	{ number: 'PO-FAKE-302', total: 980.5 },
	{ number: 'PO-FAKE-303', total: 4400.0 }
];

// Mirrors backend/app/services/vendor_consolidation.py::mask_tax_id — the
// vendor list API never returns a raw tax_id (PII invariant), only a
// display-safe `***<last4>`. Vendor sync itself stores the raw value; this
// is purely for asserting what the *response* is supposed to look like.
function maskTaxId(taxId: string): string {
	const normalized = taxId.replace(/[^a-zA-Z0-9]/g, '').toUpperCase();
	return normalized.length < 4 ? '***' : `***${normalized.slice(-4)}`;
}

const FAKE_GL_ACCOUNTS = [
	{ code: '6100', name: 'Fake Office Supplies' },
	{ code: '6200', name: 'Fake Software' },
	{ code: '6300', name: 'Fake Consulting' }
];

const FAKE_VENDORS = [
	{
		name: 'Fake Merge Vendor Co',
		email: 'ap@fakemergevendor.example',
		phone: '+1-555-0170',
		taxId: '71-1234567',
		paymentTerms: 'Net 30',
		addressContains: '701 Fake Merge Ave'
	},
	{
		name: 'Fake Merge Supply Co',
		email: 'billing@fakemergesupply.example',
		phone: '+1-555-0172',
		taxId: '72-2345678',
		paymentTerms: 'Net 45',
		addressContains: '702 Fake Supply Rd'
	},
	{
		name: 'Fake Merge Services Co',
		email: 'invoices@fakemergeservices.example',
		phone: '+1-555-0173',
		taxId: '73-3456789',
		// This fixture's payment_term is a bare string on the fake, not an
		// {"name": ...} object — proves _merge_vendor_to_payload's `elif
		// isinstance(pt, str)` branch, not just the common dict shape.
		paymentTerms: 'Net 60',
		addressContains: '703 Fake Services Blvd'
	}
];

// The exact settings.erp shape the adapter reads: get_erp_adapter passes the
// whole dict to the adapter, which reads api_key / account_token flat (the
// same flat shape the /organization ERP panel saves).
const MERGE_ERP_CONFIG = {
	type: 'merge_dev',
	integration_method: 'merge_dev',
	api_key: 'fake-merge-key',
	account_token: 'fake-account-token'
};

test.describe('/erp merge_dev adapter against fake-erp', () => {
	test.beforeAll(async () => {
		// Deterministic document ids for this run. Best-effort — when the
		// fake is down the beforeEach gate skips every test with the hint.
		await resetFakeErp();
	});

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await setErpSettings(page, MERGE_ERP_CONFIG);
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

	test('connection test succeeds against the fake Merge.dev', async ({ page }) => {
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
		expect(result.message).toContain('merge_dev');
	});

	test('PO sync imports the fake catalogue across cursor pages, idempotent on re-run', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);

		// First sync: all three fixture POs end up present. POs already in
		// the DB from a prior local run count as "skipped" — either way the
		// fake's catalogue is fully imported, and because the fake serves it
		// as 2 + 1 across two cursor pages, a count of 3 proves the adapter
		// followed the `next` cursor.
		const first = await page.request.post(`${API_BASE}/api/purchase-orders/sync-erp`, {
			headers
		});
		expect(first.status()).toBe(200);
		const firstBody = (await first.json()) as {
			success: boolean;
			created: number;
			skipped: number;
			adapter: string;
		};
		expect(firstBody.success).toBe(true);
		expect(firstBody.adapter).toBe('merge_dev');
		expect(firstBody.created + firstBody.skipped).toBe(FAKE_POS.length);

		// Second sync: everything exists → created=0 / skipped=3 (the dedupe
		// guarantee — without it every sync click multiplies the catalogue).
		const second = await page.request.post(`${API_BASE}/api/purchase-orders/sync-erp`, {
			headers
		});
		expect(second.status()).toBe(200);
		const secondBody = (await second.json()) as { created: number; skipped: number };
		expect(secondBody.created).toBe(0);
		expect(secondBody.skipped).toBe(FAKE_POS.length);

		// The synced POs are listable with their headers AND totals intact.
		const list = await page.request.get(`${API_BASE}/api/purchase-orders?page_size=100`, {
			headers
		});
		expect(list.status()).toBe(200);
		const listBody = (await list.json()) as {
			items: Array<{ po_number: string; total: number; line_items: unknown[] }>;
		};
		for (const fixture of FAKE_POS) {
			const po = listBody.items.find((p) => p.po_number === fixture.number);
			expect(po, `synced PO ${fixture.number} is listable`).toBeTruthy();
			expect(po!.total).toBeCloseTo(fixture.total, 2);
		}

		// Line items persisted, not just headers — PO-FAKE-301 carries two
		// lines (1000.00 widgets + 250.00 installation).
		const target = listBody.items.find((p) => p.po_number === 'PO-FAKE-301')!;
		expect(target.line_items.length).toBe(2);
	});

	test('GL sync imports the fake chart of accounts (6100/6200/6300)', async ({ page }) => {
		const headers = await authedTenantHeaders(page);

		const sync = await page.request.post(`${API_BASE}/api/gl-accounts/sync-erp`, {
			headers
		});
		expect(sync.status()).toBe(200);
		const syncBody = (await sync.json()) as {
			success: boolean;
			created: number;
			updated: number;
			adapter: string;
		};
		expect(syncBody.success).toBe(true);
		expect(syncBody.adapter).toBe('merge_dev');

		// The three fixture accounts are on the tenant chart with the fake's
		// names — code AND name, so a mis-mapped payload can't pass. (created
		// vs updated depends on prior runs; presence is the invariant.)
		const list = await page.request.get(`${API_BASE}/api/gl-accounts`, { headers });
		expect(list.status()).toBe(200);
		const accounts = (await list.json()) as Array<{ code: string; name: string }>;
		for (const fixture of FAKE_GL_ACCOUNTS) {
			const match = accounts.find((a) => a.code === fixture.code);
			expect(match, `GL account ${fixture.code} synced`).toBeTruthy();
			expect(match!.name).toBe(fixture.name);
		}
	});

	test('vendor sync imports the fake vendors across cursor pages with full field mapping, idempotent on re-run', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);

		// First sync: all three fixture vendors end up present. Same
		// created+skipped-equals-catalogue-size invariant as the PO sync test
		// above — the fake serves them 2 + 1 across two cursor pages, so a
		// total of 3 proves the REAL merge_dev adapter's list_vendors()
		// followed the `next` cursor rather than stopping at page 1.
		const first = await page.request.post(`${API_BASE}/api/vendors/sync-erp`, {
			headers
		});
		expect(first.status()).toBe(200);
		const firstBody = (await first.json()) as {
			success: boolean;
			created: number;
			updated: number;
			unchanged: number;
		};
		expect(firstBody.success).toBe(true);
		expect(firstBody.created + firstBody.updated + firstBody.unchanged).toBe(
			FAKE_VENDORS.length
		);

		// Second sync: everything already matches by erp_vendor_id → 0
		// created, all three unchanged (the dedupe guarantee).
		const second = await page.request.post(`${API_BASE}/api/vendors/sync-erp`, {
			headers
		});
		expect(second.status()).toBe(200);
		const secondBody = (await second.json()) as { created: number; unchanged: number };
		expect(secondBody.created).toBe(0);
		expect(secondBody.unchanged).toBe(FAKE_VENDORS.length);

		// The synced vendors are listable with the REAL adapter's parsed
		// fields intact — proves _merge_vendor_to_payload's mapping of
		// Merge's unified Vendor shape (name / email_address / phone_number /
		// addresses[0] / tax_number / payment_term), not just that the sync
		// call didn't error.
		type VendorRow = {
			name: string;
			email: string | null;
			phone: string | null;
			address: string | null;
			tax_id: string | null;
			payment_terms: string | null;
			erp_vendor_id: string | null;
		};
		const list = await page.request.get(
			`${API_BASE}/api/vendors?search=Fake%20Merge&page_size=50`,
			{ headers }
		);
		expect(list.status()).toBe(200);
		const listBody = (await list.json()) as { items: VendorRow[] };
		for (const fixture of FAKE_VENDORS) {
			const vendor = listBody.items.find((v) => v.name === fixture.name);
			expect(vendor, `synced vendor ${fixture.name} is listable`).toBeTruthy();
			expect(vendor!.email).toBe(fixture.email);
			expect(vendor!.phone).toBe(fixture.phone);
			// The vendor list API masks tax_id to `***<last4>` (PII invariant,
			// see backend/app/schemas/vendor.py::VendorResponse.from_db) — this
			// still proves the real value round-tripped through the sync
			// correctly, since a wrong/missing tax_id would mask differently.
			expect(vendor!.tax_id).toBe(maskTaxId(fixture.taxId));
			expect(vendor!.payment_terms).toBe(fixture.paymentTerms);
			expect(vendor!.address).toContain(fixture.addressContains);
			expect(vendor!.erp_vendor_id).toBeTruthy();
		}
	});

	test('full send: approved invoice posts to the fake Merge.dev and completes', async ({
		page
	}) => {
		// Merge references the vendor (`contact`) by its Merge id, which only
		// the vendor sync stores; the fake 400s an unknown contact.
		await syncErpVendors(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-MERGE',
			amount: '1985.25',
			vendor: 'Fake Merge Vendor Co'
		});
		try {
			const terminal = await sendToErpAndAwaitTerminal(page, inv.id);
			expect(terminal).toBe('done');

			// The adapter returned the fake's Merge-shaped document id — proof
			// the REAL merge_dev adapter (not the mock) performed the post.
			const erpRef = await erpReferenceFromAudit(page, inv.id);
			expect(erpRef).toMatch(/^merge-inv-\d+$/);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
