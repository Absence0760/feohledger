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
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	markSeededWorkflowVersions,
	purgeSeededWorkflowVersions
} from '../workflows/seededWorkflowVersions';
import {
	FAKE_ERP_BASE,
	type Inv,
	createApprovedInvoice,
	deleteInvoice,
	erpFailureFromAudit,
	erpReferenceFromAudit,
	getInvoice,
	loginAsSecondActor,
	sendToErpAndAwaitTerminal,
	syncErpGlAccounts,
	syncErpVendors,
	testErpConnection
} from './helpers';

/**
 * xero adapter e2e — the REAL Xero adapter
 * (backend/app/services/erp_adapters/xero.py) connected through the REAL
 * OAuth flow (backend/app/api/erp_oauth.py) against fake-erp's stub Xero
 * identity server and accounting API (tools/fake-erp, `pnpm erp:up`). The
 * committed backend/.env.development points FEOH_ERP_XERO_AUTHORIZE_URL /
 * _TOKEN_URL / _API_BASE at the fake and supplies its stub app credentials.
 *
 * Xero's connect has a step QuickBooks' does not: the callback carries no
 * company id, so the backend calls `GET /connections` with the fresh token
 * and stores the one organisation that consent authorised. A connected
 * status naming the fake tenant proves that lookup ran.
 *
 * Coverage:
 *   1. Connect — authorize → consent 302 → callback → code exchange →
 *      /connections; the org is connected to `fake-xero-tenant-1` and the
 *      connection test passes on the stored token.
 *   2. Full send — after the contact + account syncs, an approved invoice
 *      posts as an ACCPAY bill; the bill read back FROM THE FAKE carries the
 *      exact gross, the synced contact, our invoice number and currency.
 *   3. Fail closed — an unlinked vendor, and a bill with no invoice / due
 *      date, are refused before anything is posted (Xero needs both dates;
 *      the adapter never invents them).
 *   4. Disconnect — clears the connection.
 */

const PROVIDER = 'xero';
const TENANT = 'fake-xero-tenant-1';
const SUPPLIER = 'Fake Xero Supplier Co';

async function connectXero(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	expect(authorize_url).toContain('/xero/identity/connect/authorize?');

	const consent = await page.request.get(authorize_url, { maxRedirects: 0 });
	expect(consent.status(), await consent.text()).toBe(302);
	const callback = consent.headers()['location'];
	expect(new URL(callback).pathname).toBe('/api/erp/oauth/callback');

	const landed = await page.request.get(callback, { maxRedirects: 0 });
	expect(landed.status()).toBe(302);
	const home = new URL(landed.headers()['location']);
	expect(home.pathname).toBe('/organization');
	expect(home.searchParams.get('erp_error')).toBeNull();
}

async function oauthStatus(page: Page) {
	const resp = await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	return resp.json();
}

/** A bill straight from the fake, as Xero holds it. */
async function xeroBill(request: APIRequestContext, id: string) {
	const resp = await request.get(`${FAKE_ERP_BASE}/xero/api.xro/2.0/Invoices/${id}`, {
		headers: { Authorization: 'Bearer spec', 'Xero-Tenant-Id': TENANT }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
	return (await resp.json()).Invoices[0];
}

/** Every bill the fake holds under an invoice number (its idempotency filter). */
async function xeroBillsNumbered(request: APIRequestContext, invoiceNumber: string) {
	const resp = await request.get(
		`${FAKE_ERP_BASE}/xero/api.xro/2.0/Invoices?InvoiceNumbers=${encodeURIComponent(invoiceNumber)}`,
		{ headers: { Authorization: 'Bearer spec', 'Xero-Tenant-Id': TENANT } }
	);
	expect(resp.ok(), await resp.text()).toBe(true);
	return (await resp.json()).Invoices as unknown[];
}

type WorkflowStepShape = { type: string; config?: Record<string, unknown> };

/** Run `body` with the tenant's default workflow's ERP step set to auto-send
 *  on approval (or not), through the same PATCH the builder saves with, and
 *  put the steps back afterwards. An invoice freezes the workflow when it is
 *  created (decisions §302 reads that snapshot), so `body` creates its own. */
async function withAutoSend(page: Page, on: boolean, body: () => Promise<void>): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const list = await page.request.get(`${API_BASE}/api/workflows?page_size=100`, { headers });
	expect(list.ok(), await list.text()).toBe(true);
	const workflows = (await list.json()).items as Array<{
		id: string;
		is_default: boolean;
		entity_id: string | null;
		steps_config: { steps: WorkflowStepShape[] };
	}>;
	const wf = workflows.find((w) => w.is_default && w.entity_id === null) ?? workflows.find((w) => w.is_default);
	expect(wf, 'the tenant has a default workflow').toBeTruthy();
	const before = wf!.steps_config.steps;
	// Each PATCH of the seeded default's steps snapshots a `workflow_versions`
	// row no name sweep can reach (decisions §127); remove exactly ours after.
	const seededVersions = markSeededWorkflowVersions();
	const steps = before.map((s) =>
		s.type === 'erp_export' ? { ...s, config: { ...s.config, auto_send_on_approval: on } } : s
	);
	const patched = await page.request.patch(`${API_BASE}/api/workflows/${wf!.id}`, {
		headers,
		data: { steps }
	});
	expect(patched.ok(), await patched.text()).toBe(true);
	try {
		await body();
	} finally {
		await page.request.patch(`${API_BASE}/api/workflows/${wf!.id}`, {
			headers,
			data: { steps: before }
		});
		purgeSeededWorkflowVersions(seededVersions);
	}
}

/** An invoice awaiting review, ready for the synced Xero supplier and account.
 *  Created as `page`'s admin; `approveAsSecondActor` approves it as the `cfo`
 *  (segregation of duties). Submitted through `/complete`, the way the modal
 *  submits, so it reaches review with its workflow snapshot. */
async function createInvoiceForApproval(page: Page, prefix: string): Promise<Inv> {
	const headers = await authedTenantHeaders(page);
	const created = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: {
			invoice_number: `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
			vendor: SUPPLIER,
			amount: '1150.00',
			tax_amount: '150.00',
			currency: 'ZAR',
			invoice_date: '2026-10-01',
			due_date: '2026-10-31',
			gl_account: '6100',
			status: 'new'
		}
	});
	expect(created.status(), await created.text()).toBe(201);
	const inv = (await created.json()) as Inv;
	const submitted = await page.request.post(`${API_BASE}/api/invoices/${inv.id}/complete`, {
		headers
	});
	expect(submitted.status(), await submitted.text()).toBe(200);
	expect((await getInvoice(page, inv.id)).status).toBe('ready_for_review');
	return inv;
}

async function approveAsSecondActor(page: Page, invoiceId: string): Promise<void> {
	const approved = await page.request.post(`${API_BASE}/api/invoices/${invoiceId}/approve`, {
		headers: await loginAsSecondActor(page),
		data: {}
	});
	expect(approved.status(), await approved.text()).toBe(200);
	expect((await approved.json()).status).toBe('approved');
}

async function auditActions(page: Page, invoiceId: string): Promise<string[]> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${invoiceId}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return ((await resp.json()) as Array<{ action: string }>).map((r) => r.action);
}

test.describe('/erp xero adapter against fake-erp', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
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

	test('OAuth connect resolves the organisation through /connections', async ({ page }) => {
		const status = await oauthStatus(page);
		expect(status.provider).toBe(PROVIDER);
		expect(status.connected).toBe(true);
		expect(status.external_tenant_id).toBe(TENANT);

		const result = await testErpConnection(page);
		expect(result.success, result.message).toBe(true);
	});

	test('full send: the bill in Xero carries the exact approved gross', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO',
			invoiceDate: '2026-10-01',
			dueDate: '2026-10-31',
			amount: '1150.00',
			taxAmount: '150.00',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
			const bill = await xeroBill(request, await erpReferenceFromAudit(page, inv.id));
			expect(bill.Type).toBe('ACCPAY');
			expect(bill.Contact.ContactID).toBe('xero-contact-1');
			expect(bill.InvoiceNumber).toBe(inv.invoice_number);
			expect(bill.CurrencyCode).toBe('ZAR');
			expect(Number(bill.Total)).toBe(1150);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('an invoice whose vendor never synced is refused, not posted', async ({ page }) => {
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO-UNLINKED',
			currency: 'ZAR',
			vendor: `Unlinked Vendor ${Date.now()}`,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Xero post refused: vendor_not_linked');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('a bill without its dates is refused, never dated by us', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-XERO-NODATE',
			currency: 'ZAR',
			vendor: SUPPLIER,
			glAccount: '6100'
		});
		try {
			expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('failed');
			expect(await erpFailureFromAudit(page, inv.id)).toBe('Xero post refused: missing_dates');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('auto-send on approval: approving reaches Xero with no "Send to ERP" click', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		await withAutoSend(page, true, async () => {
			const inv = await createInvoiceForApproval(page, 'E2E-XERO-AUTO');
			try {
				await approveAsSecondActor(page, inv.id);
				// Polled on the invoice's real status: approval answers first,
				// then its own post-commit transaction starts the push.
				await expect
					.poll(async () => (await getInvoice(page, inv.id)).status, {
						// The same budget `sendToErpAndAwaitTerminal` gives a clicked send.
						timeout: 20_000,
						message: 'the approved invoice never finished its automatic ERP send'
					})
					.toMatch(/^(done|failed)$/);
				expect((await getInvoice(page, inv.id)).status).toBe('done');

				const trail = await auditActions(page, inv.id);
				expect(trail.indexOf('invoice.erp_submitted')).toBeGreaterThan(
					trail.indexOf('invoice.approved')
				);
				const bill = await xeroBill(request, await erpReferenceFromAudit(page, inv.id));
				expect(bill.Type).toBe('ACCPAY');
				expect(bill.InvoiceNumber).toBe(inv.invoice_number);
				expect(bill.Contact.ContactID).toBe('xero-contact-1');
				expect(Number(bill.Total)).toBe(1150);
			} finally {
				await deleteInvoice(page, inv.id);
			}
		});
	});

	test('auto-send off: approval leaves the invoice approved and posts nothing', async ({
		page,
		request
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		await withAutoSend(page, false, async () => {
			const inv = await createInvoiceForApproval(page, 'E2E-XERO-MANUAL');
			try {
				await approveAsSecondActor(page, inv.id);
				// With the flag off approval queues no push at all, so there is
				// nothing to wait for: the state right after the approval is final.
				expect((await getInvoice(page, inv.id)).status).toBe('approved');
				expect(await auditActions(page, inv.id)).not.toContain('invoice.erp_submitted');
				expect(await xeroBillsNumbered(request, inv.invoice_number)).toHaveLength(0);

				// The person's click is still what sends it, and it posts once.
				expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');
				expect(await xeroBillsNumbered(request, inv.invoice_number)).toHaveLength(1);
			} finally {
				await deleteInvoice(page, inv.id);
			}
		});
	});

	test('disconnect clears the connection', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const resp = await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, {
			headers
		});
		expect((await resp.json()).disconnected).toBe(true);
		const status = await oauthStatus(page);
		expect(status.connected).toBe(false);
	});
});
