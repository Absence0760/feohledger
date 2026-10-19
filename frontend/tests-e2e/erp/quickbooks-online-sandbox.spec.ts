import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	FAKE_ERP_BASE,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	sendToErpAndAwaitTerminal,
	testErpConnection
} from './helpers';

/**
 * The REAL QuickBooks Online sandbox company, replayed — the regression
 * harness for what the live test of 2026-10-19 found (#598, decisions §284).
 *
 * CI cannot reach Intuit, so the company is recorded in
 * tools/fake-erp/fixtures/qbo_sandbox_company.json and served by the fake as a
 * second `/qbo` company (tools/fake-erp/qbo_sandbox.py). A spec picks it the way
 * a person picks a company on Intuit's consent screen: `connectSandbox` appends
 * `fake_company=sandbox` to the authorize URL the backend hands out. Its realm,
 * tokens and data are its own, so quickbooks-online.spec.ts (the default
 * company) never sees them; its state survives the fake's global /__reset and
 * is cleared by /qbo/_admin/sandbox/reset before each test here.
 *
 * The backend's in-process replay of the same fixture is
 * backend/tests/test_qbo_sandbox_replay.py; this spec adds the real HTTP, the
 * OAuth chain, the API and the push.
 *
 * Coverage:
 *   1. The pre-§284 chart, repaired — our chart as the old adapter left it
 *      (bare names, same-named accounts merged into one row) is seeded through
 *      the API and linked as it was; one sync then answers EXACTLY what it
 *      answered live, and the chart afterwards has one correctly-linked row per
 *      path.
 *   2. Pushes on the repaired chart — the two "Software" accounts post to
 *      their own ids, and an invoice still coded to a merged row is refused
 *      `account_not_linked` instead of posting on a guess.
 *   3. Vendors — non-ASCII and 90-character names arrive intact, QuickBooks'
 *      vendor AcctNum never becomes our vendor code.
 *   4. Token lifecycle — an expired access token is refreshed and the refresh
 *      token rotated without the sync noticing; a short-lived token is
 *      refreshed before use; a consent revoked at Intuit leaves the connection
 *      `needs_reconnect`, not a 500.
 *   5. Disconnect revokes the refresh token at Intuit.
 */

const PROVIDER = 'quickbooks_online';
const SANDBOX_ADMIN = `${FAKE_ERP_BASE}/qbo/_admin/sandbox`;

type QboAccount = {
	Id: string;
	Name: string;
	FullyQualifiedName: string;
	AcctNum?: string;
};
type QboVendor = { Id: string; DisplayName: string; AcctNum?: string };
type LegacyRow = {
	code: string;
	name: string;
	account_type: string;
	parent_code: string | null;
	erp_account_id: string;
};
type ChartSummary = {
	created: number;
	updated: number;
	unchanged: number;
	linked: number;
	conflict_count: number;
	conflicts: Array<{ code: string; reason: string; erp_account_ids: string[] }>;
	unlinked_count: number;
	unlinked: string[];
	code_drift_count: number;
	code_drift: Array<{ code: string; erp_code: string }>;
	link_changes: Array<{ code: string; from: string | null; to: string | null }>;
};

const COMPANY = JSON.parse(
	readFileSync(
		resolve(
			dirname(fileURLToPath(import.meta.url)),
			'../../../tools/fake-erp/fixtures/qbo_sandbox_company.json'
		),
		'utf8'
	)
) as {
	realm: string;
	accounts: QboAccount[];
	vendors: QboVendor[];
	legacy_chart: LegacyRow[];
	live_sync: { gl_accounts: ChartSummary };
};

const sql = (value: string) => `'${value.replaceAll("'", "''")}'`;
const sqlList = (values: Iterable<string>) => [...new Set(values)].map(sql).join(', ');

/** Every code a row of this company's chart can carry: its path, its bare name,
 *  its number, and each legacy row's code. */
const COMPANY_CODES = [
	...COMPANY.accounts.flatMap((a) => [a.FullyQualifiedName, a.Name, a.AcctNum ?? a.Name]),
	...COMPANY.legacy_chart.map((r) => r.code)
];
const ACCOUNT_IDS = COMPANY.accounts.map((a) => a.Id);
const VENDOR_IDS = COMPANY.vendors.map((v) => v.Id);

/**
 * Leave the worker tenant's chart and vendors holding nothing of this company.
 *
 * Its rows are deleted (nothing references a GL row by key, invoices hold the
 * code as a string). Any other row still linked to one of its ids is unlinked:
 * other ERP specs share this tenant and their fake companies reuse small ids
 * ("7", "56"), so a stale link would be matched by id and turn into code drift.
 * Vendors are only unlinked — every vendor spec re-links by name on its sync.
 */
function clearCompanyFromTenant(): void {
	tenantPsql(`DELETE FROM gl_accounts WHERE code IN (${sqlList(COMPANY_CODES)})`);
	tenantPsql(
		`UPDATE gl_accounts SET erp_account_id = NULL WHERE erp_account_id IN (${sqlList(ACCOUNT_IDS)})`
	);
	tenantPsql(
		`UPDATE vendors SET erp_vendor_id = NULL WHERE erp_vendor_id IN (${sqlList(VENDOR_IDS)})`
	);
}

async function sandbox(path: string, data?: unknown) {
	const resp = await fetch(`${SANDBOX_ADMIN}/${path}`, {
		method: data === undefined ? 'GET' : 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: data === undefined ? undefined : JSON.stringify(data)
	});
	expect(resp.ok, `fake-erp ${path} (${resp.status})`).toBe(true);
	return resp.json();
}

type Stats = { refreshes: number; rejected: number; refresh_tokens: string[]; revocations: string[] };
const stats = () => sandbox('stats') as Promise<Stats>;

/** The OAuth connect chain, picking the sandbox company at consent. */
async function connectSandbox(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const consentUrl = new URL((await start.json()).authorize_url);
	consentUrl.searchParams.set('fake_company', 'sandbox');

	const consent = await page.request.get(consentUrl.toString(), { maxRedirects: 0 });
	expect(consent.status()).toBe(302);
	const callback = new URL(consent.headers()['location']);
	expect(callback.searchParams.get('realmId')).toBe(COMPANY.realm);

	const landed = await page.request.get(callback.toString(), { maxRedirects: 0 });
	expect(landed.status()).toBe(302);
	expect(new URL(landed.headers()['location']).searchParams.get('erp_error')).toBeNull();
}

async function oauthStatus(page: Page) {
	const resp = await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	return resp.json();
}

async function post(page: Page, path: string) {
	return page.request.post(`${API_BASE}${path}`, { headers: await authedTenantHeaders(page) });
}

async function syncChart(page: Page): Promise<ChartSummary & { message: string }> {
	const resp = await post(page, '/api/gl-accounts/sync-erp');
	expect(resp.status(), await resp.text()).toBe(200);
	return resp.json();
}

type GlRow = { code: string; name: string; erp_account_id: string | null; parent_code: string | null };
async function chart(page: Page): Promise<Map<string, GlRow>> {
	const resp = await page.request.get(`${API_BASE}/api/gl-accounts`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return new Map(((await resp.json()) as GlRow[]).map((r) => [r.code, r]));
}

/** Our chart as the pre-§284 adapter left it: created through the API (parents
 *  first), then linked the way that sync linked it. No API writes a link —
 *  that is the sync's job — so the link is set in the tenant DB. */
async function seedLegacyChart(page: Page): Promise<void> {
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
	const depth = (r: LegacyRow) => (r.parent_code === null ? 0 : 1);
	for (const row of [...COMPANY.legacy_chart].sort((a, b) => depth(a) - depth(b))) {
		const resp = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers,
			data: {
				code: row.code,
				name: row.name,
				account_type: row.account_type,
				parent_code: row.parent_code
			}
		});
		expect(resp.status(), `${row.code}: ${await resp.text()}`).toBe(201);
	}
	const links = COMPANY.legacy_chart.map((r) => `(${sql(r.code)}, ${sql(r.erp_account_id)})`);
	tenantPsql(
		`UPDATE gl_accounts g SET erp_account_id = v.id FROM (VALUES ${links.join(', ')}) AS v(code, id) ` +
			'WHERE g.code = v.code AND g.entity_id IS NULL'
	);
}

const byCode = <T extends { code: string }>(items: T[]) =>
	[...items].sort((a, b) => a.code.localeCompare(b.code));

function lineAccounts(bill: { Line: Array<{ AccountBasedExpenseLineDetail?: { AccountRef?: { value: string } } }> }) {
	return bill.Line.map((l) => l.AccountBasedExpenseLineDetail?.AccountRef?.value);
}

/** A number QuickBooks' 21-character DocNumber holds. */
const docNumber = (prefix: string) => `${prefix}${Date.now() % 1e9}-${Math.floor(Math.random() * 1e4)}`;

test.describe('/erp quickbooks_online against the recorded sandbox company', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await sandbox('reset', {});
		clearCompanyFromTenant();
		await connectSandbox(page);
	});

	// Other suites assume the seeded org has NO ERP configured, and the default
	// QuickBooks company's spec must not meet this company's rows.
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
		clearCompanyFromTenant();
	});

	test('connects to the sandbox company', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.external_tenant_id).toBe(COMPANY.realm);
		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('the pre-§284 chart is repaired exactly as the live sync repaired it', async ({ page }) => {
		await seedLegacyChart(page);
		const before = await chart(page);
		// The collapse the live test found: the merged rows, linked to one of the
		// accounts each stood for.
		expect(before.get('Job Materials')?.erp_account_id).toBe('46');
		expect(before.get('Depreciation')?.erp_account_id).toBe('39');
		expect(before.has('Office Expenses:Software')).toBe(false);

		const result = await syncChart(page);
		const live = COMPANY.live_sync.gl_accounts;
		const { message, ...summary } = result as ChartSummary & {
			message: string;
			success: boolean;
			adapter: string;
		};
		// The live answer, plus what the inactive-inclusive query adds (§284):
		// Bank Charges (8), inactive in QuickBooks, is retired here — one more
		// "updated" — where live it was never read. Every pre-0115 link holds
		// its own code in this company, so the realm pass adopts them all and
		// moves none.
		const legacyLinks = COMPANY.legacy_chart.filter((r) => r.erp_account_id).length;
		expect(summary).toEqual({
			...live,
			updated: live.updated + 1,
			deactivated: ['Bank Charges'],
			deactivated_count: 1,
			realm: `${PROVIDER}:${COMPANY.realm}`,
			realm_adopted: legacyLinks,
			link_changes: expect.any(Array),
			success: true,
			adapter: PROVIDER
		});
		expect(byCode(result.link_changes)).toEqual(byCode(live.link_changes));
		expect(message).toBe(
			`Synced ${live.created} new, ${live.updated + 1} updated GL accounts; ` +
				`${live.conflict_count} not linked, ${live.unlinked_count} unlinked`
		);

		const after = await chart(page);
		const accountById = new Map(COMPANY.accounts.map((a) => [a.Id, a]));
		const linked = [...after.values()].filter(
			(r) => r.erp_account_id && accountById.has(r.erp_account_id)
		);
		expect(linked).toHaveLength(89);
		expect(new Set(linked.map((r) => r.erp_account_id)).size).toBe(89);

		// One row per path for every repeated leaf, each on that path's id.
		for (const [path, id] of [
			['Job Expenses:Job Materials', '63'],
			['Landscaping Services:Job Materials', '46'],
			['Truck:Depreciation', '39'],
			['Landscaping Services:Labor:Maintenance and Repair', '53'],
			['Office Expenses:Software', '1150040001'],
			['Software', '1150040000']
		]) {
			expect(after.get(path)?.erp_account_id, path).toBe(id);
		}
		// The merged rows stay, with no link to post on.
		for (const code of live.unlinked) {
			expect(after.get(code)?.erp_account_id, code).toBeNull();
		}
		// Numbered accounts keep code and link; the number is reported as drift.
		expect(after.get('Advertising')?.erp_account_id).toBe('7');
		expect(after.get('Office Expenses')?.erp_account_id).toBe('15');
		expect(result.code_drift).toContainEqual({ code: 'Advertising', erp_code: '6010' });
		expect(after.has('6010')).toBe(false);

		// The repair is stable: a second run moves nothing.
		const again = await syncChart(page);
		expect([again.created, again.updated, again.linked, again.unlinked_count]).toEqual([0, 0, 0, 0]);
		expect(again.conflicts).toEqual(live.conflicts);

		// An invoice still coded to a merged row is refused before anything
		// posts (live push D) — never booked to whichever account it was on.
		await post(page, '/api/vendors/sync-erp');
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBOSB-MERGED',
			invoiceNumber: docNumber('M'),
			amount: '10.00',
			vendor: 'Ærøskøbing Café & Søn ApS',
			glAccount: 'Job Materials'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe(
				'QuickBooks Online post refused: account_not_linked'
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a person settles a locked-out top-level account, and its bills post there', async ({
		page
	}) => {
		// Live, "Depreciation" (40) and "Maintenance and Repair" (72) were refused
		// for good: our bare-name rows could mean them or a sub-account, and no
		// sync may guess. The way out is a confirmed choice among what it offered.
		await seedLegacyChart(page);
		await syncChart(page);
		const rows = (await (
			await page.request.get(`${API_BASE}/api/gl-accounts`, {
				headers: await authedTenantHeaders(page)
			})
		).json()) as Array<
			GlRow & {
				id: string;
				erp_link_candidates: { reason: string; accounts: Array<{ erp_account_id: string }> } | null;
			}
		>;
		const depreciation = rows.find((r) => r.code === 'Depreciation')!;
		expect(depreciation.erp_account_id).toBeNull();
		expect(depreciation.erp_link_candidates?.reason).toBe('ambiguous_legacy');
		expect(depreciation.erp_link_candidates?.accounts.map((a) => a.erp_account_id).sort()).toEqual([
			'39',
			'40'
		]);

		const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
		const notOffered = await page.request.post(
			`${API_BASE}/api/gl-accounts/${depreciation.id}/erp-link`,
			{ headers, data: { erp_account_id: '72' } }
		);
		expect(notOffered.status()).toBe(422);
		const confirmed = await page.request.post(
			`${API_BASE}/api/gl-accounts/${depreciation.id}/erp-link`,
			{ headers, data: { erp_account_id: '40' } }
		);
		expect(confirmed.status(), await confirmed.text()).toBe(200);

		// The next sync keeps the person's choice and stops reporting the code.
		const again = await syncChart(page);
		expect(again.conflicts.map((c) => c.code)).toEqual(['Maintenance and Repair']);
		expect((await chart(page)).get('Depreciation')?.erp_account_id).toBe('40');

		await post(page, '/api/vendors/sync-erp');
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-QBOSB-DEP',
			invoiceNumber: docNumber('D'),
			amount: '45.00',
			vendor: 'Ærøskøbing Café & Søn ApS',
			glAccount: 'Depreciation'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			const { Bill } = await sandbox(`bills/${await erpReferenceFromAudit(page, inv.id)}`);
			expect(lineAccounts(Bill)).toEqual(['40']);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('each Software account receives its own bills', async ({ page }) => {
		expect((await post(page, '/api/vendors/sync-erp')).status()).toBe(200);
		await syncChart(page);
		for (const [glAccount, accountId, vendor, vendorId] of [
			['Office Expenses:Software', '1150040001', 'Ærøskøbing Café & Søn ApS', '59'],
			[
				'Software',
				'1150040000',
				'Northwind Industrial Fasteners and Precision Components International Holdings Corporation',
				'60'
			]
		]) {
			const inv = await createApprovedInvoice(page, {
				prefix: 'E2E-QBOSB-SW',
				invoiceNumber: docNumber('W'),
				amount: '123.45',
				vendor,
				glAccount
			});
			try {
				expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
				const { Bill } = await sandbox(`bills/${await erpReferenceFromAudit(page, inv.id)}`);
				expect(lineAccounts(Bill), glAccount).toEqual([accountId]);
				expect(Bill.VendorRef.value).toBe(vendorId);
				expect(Bill.TotalAmt).toBe(123.45);
			} finally {
				await deleteInvoice(page, inv.id);
			}
		}
	});

	test('vendors arrive with their names intact and no borrowed code', async ({ page }) => {
		const resp = await post(page, '/api/vendors/sync-erp');
		expect(resp.status()).toBe(200);
		expect((await resp.json()).conflicts).toEqual([]);

		const headers = await authedTenantHeaders(page);
		const named = async (name: string) => {
			const list = await page.request.get(`${API_BASE}/api/vendors`, {
				headers,
				params: { search: name }
			});
			expect(list.status()).toBe(200);
			const { items } = (await list.json()) as {
				items: Array<{ name: string; code: string | null; payment_terms: string | null }>;
			};
			return items.filter((v) => v.name === name);
		};

		const aero = await named('Ærøskøbing Café & Søn ApS');
		expect(aero).toHaveLength(1);
		expect(aero[0].payment_terms).toBe('Net 15');

		const northwindName = COMPANY.vendors.find((v) => v.Id === '60')!.DisplayName;
		expect(northwindName).toHaveLength(90);
		const northwind = await named(northwindName);
		expect(northwind).toHaveLength(1);
		expect(northwind[0].payment_terms).toBe('Net 60');
		// QuickBooks' "Account no." is OUR number at Northwind, not its code.
		expect(northwind[0].code).toBeNull();
	});

	test('an expired access token is refreshed and the refresh token rotated', async ({ page }) => {
		const [connected] = (await stats()).refresh_tokens;
		expect(connected).toBeTruthy();
		const issuedUntil = (await oauthStatus(page)).expires_at;

		// Past its hour while our stored expiry says otherwise: Intuit's 401 is
		// answered with a refresh and the call retried, invisibly to the sync.
		await sandbox('expire-access', {});
		expect((await post(page, '/api/vendors/sync-erp')).status()).toBe(200);
		let now = await stats();
		expect([now.rejected, now.refreshes]).toEqual([1, 1]);
		expect(now.refresh_tokens).toHaveLength(1);
		expect(now.refresh_tokens[0]).not.toBe(connected);
		expect((await oauthStatus(page)).expires_at).not.toBe(issuedUntil);

		// A token that expires inside the backend's refresh margin is refreshed
		// BEFORE use — no 401 this time.
		await sandbox('tokens', { access_ttl: 60 });
		await sandbox('expire-access', {});
		expect((await post(page, '/api/gl-accounts/sync-erp')).status()).toBe(200); // 401, refresh (ttl 60)
		expect((await post(page, '/api/gl-accounts/sync-erp')).status()).toBe(200); // refresh first
		now = await stats();
		expect([now.rejected, now.refreshes]).toEqual([2, 3]);
		expect((await oauthStatus(page)).needs_reconnect).toBeFalsy();
	});

	test('a consent revoked at Intuit asks for a reconnect, not a 500', async ({ page }) => {
		await sandbox('revoke-consent', {});
		const vendors = await post(page, '/api/vendors/sync-erp');
		expect(vendors.status()).toBe(502);
		expect((await vendors.json()).detail).toBe('ERP request failed: ErpNotConnectedError');

		const status = await oauthStatus(page);
		expect(status.needs_reconnect).toBe(true);
		expect(status.connected).toBe(false);

		// Marked once; later calls fail the same way without asking Intuit again.
		const glSync = await post(page, '/api/gl-accounts/sync-erp');
		expect(glSync.status()).toBe(502);
		expect((await glSync.json()).detail).toBe('ERP request failed: ErpNotConnectedError');
		expect((await testErpConnection(page)).success).toBe(false);
		expect((await stats()).refreshes).toBe(0);
	});

	test('disconnect revokes the refresh token at Intuit', async ({ page }) => {
		const [refresh] = (await stats()).refresh_tokens;
		const resp = await post(page, '/api/organization/erp/oauth/disconnect');
		expect(resp.ok()).toBe(true);
		expect(await resp.json()).toEqual({ disconnected: true, revoked: true });
		const now = await stats();
		expect(now.revocations).toEqual([refresh]);
		expect(now.refresh_tokens).toEqual([]);
		expect((await oauthStatus(page)).connected).toBe(false);
	});
});
