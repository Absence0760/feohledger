import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	tenantHeaders
} from '../fixtures/helpers';

/**
 * Shared plumbing for the erp/ suite — the specs that exercise the three
 * REAL ERP adapters (merge_dev / netsuite / dynamics_365_bc) against the
 * local fake ERP container (tools/fake-erp, compose profile `erp`, host
 * port 12112; `pnpm erp:up`).
 *
 * The backend's committed .env.development points the adapters' base URLs
 * at the fake via FEOH_ERP_MERGE_API_BASE / FEOH_ERP_NETSUITE_API_BASE /
 * FEOH_ERP_D365_API_BASE / FEOH_ERP_D365_TOKEN_URL, so a normally-started dev
 * or CI backend talks to it with no per-run config. The specs only have to
 * flip the org's `settings.erp` to the adapter under test (and reset it to
 * null in afterAll — other suites assume no ERP configured).
 */

export const FAKE_ERP_BASE = 'http://localhost:12112';

/** Wipe the fake ERP's in-memory state (stored invoices + id counters) so
 *  document-id assertions aren't coupled to what previous runs created.
 *  Best-effort: when the fake isn't running the specs skip anyway via
 *  `skipUnlessReachable(SERVICES.fakeErp)`, so a refused connection here
 *  must not fail the hook. */
export async function resetFakeErp(): Promise<void> {
	try {
		await fetch(`${FAKE_ERP_BASE}/__reset`, {
			method: 'POST',
			signal: AbortSignal.timeout(2_500)
		});
	} catch {
		/* fake-erp down — the beforeEach skip gate reports it actionably */
	}
}

/** PATCH the org's `settings.erp`. Pass `null` to unconfigure (the afterAll
 *  cleanup contract every erp spec honours). */
// The ERP block's secret fields — mirror of the backend's
// `provider_credentials.SECRET_FIELDS["erp"]`. They are stored sealed through
// `PUT /api/organization/credentials/erp`; `PATCH /api/organization` refuses them.
const ERP_SECRET_FIELDS = new Set([
	'api_key',
	'account_token',
	'client_secret',
	'consumer_secret',
	'token_secret',
	'password',
	'subscription_key',
	'operator_password',
	'company_password',
	'webhook_signing_secret',
	'webhook_secret'
]);

export async function setErpSettings(
	page: Page,
	erp: Record<string, unknown> | null
): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const entries = Object.entries(erp ?? {});
	const config = erp
		? Object.fromEntries(entries.filter(([key]) => !ERP_SECRET_FIELDS.has(key)))
		: null;
	const secrets = Object.fromEntries(
		entries.filter(([key, value]) => ERP_SECRET_FIELDS.has(key) && typeof value === 'string')
	) as Record<string, string>;
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: { ...headers, 'Content-Type': 'application/json' },
		data: { settings: { erp: config } }
	});
	expect(resp.ok(), `PATCH /api/organization settings.erp failed (${resp.status()})`).toBe(true);
	// Clearing the ERP clears its stored secrets too, so the next spec starts
	// from nothing rather than inheriting another adapter's credentials.
	const creds = await page.request.put(`${API_BASE}/api/organization/credentials/erp`, {
		headers: { ...headers, 'Content-Type': 'application/json' },
		data: erp ? { set: secrets } : { clear: [...ERP_SECRET_FIELDS] }
	});
	expect(creds.ok(), `PUT /api/organization/credentials/erp failed (${creds.status()})`).toBe(
		true
	);
}

/** POST /api/organization/test-erp with an empty body so the endpoint tests
 *  the SAVED `settings.erp` — the same config the send/sync flows read. */
export async function testErpConnection(
	page: Page
): Promise<{ success: boolean; message: string }> {
	const headers = await authedTenantHeaders(page);
	const resp = await page.request.post(`${API_BASE}/api/organization/test-erp`, {
		headers: { ...headers, 'Content-Type': 'application/json' },
		data: {}
	});
	expect(resp.status()).toBe(200);
	return (await resp.json()) as { success: boolean; message: string };
}

export type Inv = { id: string; invoice_number: string; status: string };

async function getInvoice(page: Page, id: string): Promise<Inv> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	if (resp.status() !== 200) throw new Error(`get invoice ${id} failed (${resp.status()})`);
	return (await resp.json()) as Inv;
}

/** Mint a token for the tenant's seeded `cfo` account via a raw login POST —
 *  decoupled from `page`'s own session/localStorage. Used so
 *  `createApprovedInvoice` can approve as a genuinely different actor than
 *  the one who created the invoice (segregation of duties: the uploader
 *  cannot also approve — see `services/approval_chain.py`). `page` is
 *  always signed in as the tenant admin (`signInAndWait(page)`), so `cfo`
 *  is guaranteed distinct. */
async function loginAsSecondActor(page: Page): Promise<Record<string, string>> {
	const slug = currentTenantSlug();
	const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug, 'Content-Type': 'application/json' },
		data: { email: `demo+cfo@${slug}.localhost`, password: 'demo' }
	});
	expect(resp.ok(), `second-actor login failed (${resp.status()})`).toBe(true);
	const { access_token } = (await resp.json()) as { access_token: string };
	return tenantHeaders(access_token, slug);
}

/** Pull the configured ERP's vendors (`POST /api/vendors/sync-erp`). A direct
 *  adapter posts a bill against the vendor's ERP id, which only this sync
 *  stores — an invoice whose vendor never synced is refused
 *  (`vendor_not_linked`), never posted by name. Idempotent, so every spec that
 *  sends can call it without caring what a previous spec synced. */
export async function syncErpVendors(page: Page): Promise<void> {
	const resp = await page.request.post(`${API_BASE}/api/vendors/sync-erp`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status(), 'vendor sync').toBe(200);
}

/** Pull the configured ERP's chart (`POST /api/gl-accounts/sync-erp`) — the
 *  only writer of `gl_accounts.erp_account_id`, which NetSuite's expense lines
 *  are posted against. Idempotent. */
export async function syncErpGlAccounts(page: Page): Promise<void> {
	const resp = await page.request.post(`${API_BASE}/api/gl-accounts/sync-erp`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status(), 'GL account sync').toBe(200);
}

/** A PO as `GET /api/purchase-orders` lists it. */
export type SyncedPo = {
	po_number: string;
	total: number | string;
	currency: string | null;
	status: string;
};

/** Pull the configured ERP's purchase orders (`POST /api/purchase-orders/sync-erp`)
 *  and return the sync's adapter plus the tenant's PO list afterwards. Callers
 *  assert presence by number, since earlier runs may already hold the rows. */
export async function syncErpPurchaseOrders(
	page: Page
): Promise<{ adapter: string; pos: SyncedPo[] }> {
	const headers = await authedTenantHeaders(page);
	const sync = await page.request.post(`${API_BASE}/api/purchase-orders/sync-erp`, { headers });
	expect(sync.status(), 'PO sync').toBe(200);
	const { adapter } = (await sync.json()) as { adapter: string };
	const list = await page.request.get(`${API_BASE}/api/purchase-orders?page_size=100`, {
		headers
	});
	expect(list.status(), 'PO list').toBe(200);
	const { items } = (await list.json()) as { items: SyncedPo[] };
	return { adapter, pos: items };
}

/** Pull the configured ERP's chart and return the tenant's GL accounts. */
export async function syncAndListGlAccounts(
	page: Page
): Promise<Array<{ code: string; name: string }>> {
	await syncErpGlAccounts(page);
	const list = await page.request.get(`${API_BASE}/api/gl-accounts`, {
		headers: await authedTenantHeaders(page)
	});
	expect(list.status(), 'GL account list').toBe(200);
	return (await list.json()) as Array<{ code: string; name: string }>;
}

/** The `details.error` the push recorded on the append-only
 *  `invoice.erp_failed` audit row — the PII-free reason a failed post gives. */
export async function erpFailureFromAudit(page: Page, invoiceId: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	const rows = (await resp.json()) as Array<{
		action: string;
		details: { error?: string } | null;
	}>;
	const failed = rows.find((r) => r.action === 'invoice.erp_failed');
	expect(failed, 'audit trail has an invoice.erp_failed row').toBeTruthy();
	return failed?.details?.error ?? '';
}

/** Create a fresh invoice via the API and approve it directly (`new →
 *  approved` is a legal edge in VALID_TRANSITIONS — the manual-entry
 *  fast path). Deliberately does NOT go through `/complete`: complete's
 *  outcome depends on the tenant's ambient workflow definition (approval
 *  disabled → `new → done`, auto_approve_below → already `approved` —
 *  both then 409 the explicit approve), and this suite's subject is the
 *  ERP adapters, not the review flow (lifecycle-money-path.spec.ts owns
 *  that). Direct approve keeps these specs deterministic on any
 *  workflow-definition state a prior suite left behind.
 *
 *  Creates as `page`'s own user (the tenant admin) and approves as the
 *  seeded `cfo` account — segregation of duties refuses a same-user
 *  create-then-approve on a manually-entered invoice, same as the real
 *  review flow. */
export async function createApprovedInvoice(
	page: Page,
	opts: { prefix: string; amount?: string; vendor?: string; glAccount?: string }
): Promise<Inv> {
	const unique = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const headers = await authedTenantHeaders(page);

	const created = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: {
			invoice_number: `${opts.prefix}-${unique}`,
			vendor: opts.vendor ?? 'Fake ERP Vendor A',
			amount: opts.amount ?? '1234.56',
			currency: 'USD',
			status: 'new',
			...(opts.glAccount ? { gl_account: opts.glAccount } : {})
		}
	});
	if (created.status() !== 201) {
		throw new Error(`create invoice failed (${created.status()}): ${await created.text()}`);
	}
	const inv = (await created.json()) as Inv;

	const approverHeaders = await loginAsSecondActor(page);
	const approved = await page.request.post(`${API_BASE}/api/invoices/${inv.id}/approve`, {
		headers: approverHeaders,
		data: {}
	});
	expect(
		approved.status(),
		`approve (${approved.status()}): ${approved.ok() ? '' : await approved.text()}`
	).toBe(200);
	expect((await getInvoice(page, inv.id)).status).toBe('approved');

	return inv;
}

/** POST send-to-erp (202) and poll the invoice's real status field — never a
 *  fixed sleep — until the async ERP dispatch settles on a terminal outcome.
 *  Success is `done` (sending_to_erp → sent_to_erp → done); a failed post
 *  lands on `failed` after the adapter's bounded retries, so polling for the
 *  terminal pair keeps a failure diagnosable instead of a bare poll timeout.
 *  Returns the terminal status for the caller to assert. */
export async function sendToErpAndAwaitTerminal(page: Page, invoiceId: string): Promise<string> {
	const sent = await page.request.post(`${API_BASE}/api/invoices/${invoiceId}/send-to-erp`, {
		headers: await authedTenantHeaders(page),
		data: {}
	});
	expect(sent.status(), 'send-to-erp accepted').toBe(202);

	await expect
		.poll(async () => (await getInvoice(page, invoiceId)).status, {
			timeout: 20_000,
			message: 'invoice never reached a terminal ERP status (done/failed)'
		})
		.toMatch(/^(done|failed)$/);

	return (await getInvoice(page, invoiceId)).status;
}

/** Read the ERP document id the adapter returned for a sent invoice, off the
 *  append-only audit trail: `invoice.erp_confirmed` carries
 *  `details.erp_reference` (the adapter's `erp_document_id`). This is the
 *  provider-shaped id the fake mints (merge-inv-N / numeric NetSuite id /
 *  d365-inv-N), so asserting its shape proves the REAL adapter — not the
 *  mock — performed the post. */
export async function erpReferenceFromAudit(page: Page, invoiceId: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	const rows = (await resp.json()) as Array<{
		action: string;
		details: { erp_reference?: string } | null;
	}>;
	const confirmed = rows.find((r) => r.action === 'invoice.erp_confirmed');
	expect(confirmed, 'audit trail has an invoice.erp_confirmed row').toBeTruthy();
	const ref = confirmed?.details?.erp_reference;
	expect(ref, 'invoice.erp_confirmed carries details.erp_reference').toBeTruthy();
	return ref as string;
}

/** Best-effort cleanup — a `done` invoice is immutable and DELETE 409s;
 *  accept that (the worker's tenant resets between sessions). */
export async function deleteInvoice(page: Page, id: string): Promise<void> {
	await page.request.delete(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}
