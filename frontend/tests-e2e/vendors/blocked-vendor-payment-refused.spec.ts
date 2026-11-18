import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deletePaymentRunsWhere,
	deleteVendorsWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

async function patchOrg(
	page: import('@playwright/test').Page,
	partial: object,
	headers: Record<string, string>
): Promise<void> {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers,
		data: { settings: partial }
	});
}

/**
 * Blocked-vendor payment refusal — the sticky payment-block fraud gate.
 *
 * Invariant: a vendor flagged `payments_blocked` (by a manual AP block, or a
 * sanctions `match` from screening) can NEVER be paid until an AP user
 * explicitly unblocks them. `services/compliance.check_payment_compliance`
 * refuses the payment before any payment-adapter call.
 *
 * REGRESSION GUARD: the compliance gate in `execute_payment_run`
 * (`backend/app/api/payments.py`) was nested inside the international-leg
 * `if` block, so a blocked vendor paid via DOMESTIC ACH (same currency, no
 * IBAN/SWIFT) skipped the gate entirely and the payment completed — a
 * blocked vendor could be paid. These tests drive the real end-to-end
 * execute path with a domestic-ACH run and assert the payment is refused:
 *
 *   1. A manually-blocked vendor: domestic-ACH payment is REFUSED
 *      (payment.status=failed, run completes with 0 paid), invoice stays
 *      `approved` (never advances to payment_scheduled).
 *   2. A vendor whose name matches the sanctions mock blocklist is screened
 *      to `match` + auto-blocked on create; the same domestic-ACH payment is
 *      refused.
 *   3. After /unblock, the same vendor's domestic-ACH payment goes through.
 *
 * Setup uses the real /api/vendors block/unblock endpoints; the approved
 * invoice + payment run are created via API + a direct DB insert (the API
 * doesn't expose linking an invoice to a specific vendor in `approved`
 * status). Each test reverts its DB state in finally.
 */

interface VendorResp {
	id: string;
	name: string;
	screening_status: string;
	payments_blocked: boolean;
}

interface ExecuteResp {
	status: string;
	payments_completed: number;
	payments_failed: number;
	payments_in_flight: number;
}

let H: Record<string, string>;
let SLUG: string;

function slugFromPage(page: import('@playwright/test').Page): string {
	return new URL(page.url()).hostname.split('.')[0];
}

async function createVendor(
	page: import('@playwright/test').Page,
	name: string
): Promise<VendorResp> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: H,
		// USD / US, no IBAN/SWIFT → resolves to the DOMESTIC corridor, the
		// path the compliance gate used to skip.
		data: {
			name,
			bank_details: { account_last4: '4321', bank_name: 'Acme Bank', country: 'US' }
		}
	});
	expect(resp.status(), `create vendor ${name}`).toBe(201);
	return (await resp.json()) as VendorResp;
}

async function blockVendor(page: import('@playwright/test').Page, id: string): Promise<void> {
	const resp = await page.request.post(`${API_BASE}/api/vendors/${id}/block`, {
		headers: H,
		data: { reason: 'e2e fraud-control test block' }
	});
	expect(resp.status()).toBe(200);
	expect(((await resp.json()) as VendorResp).payments_blocked).toBe(true);
}

async function unblockVendor(page: import('@playwright/test').Page, id: string): Promise<void> {
	const resp = await page.request.post(`${API_BASE}/api/vendors/${id}/unblock`, {
		headers: H,
		data: {}
	});
	expect(resp.status()).toBe(200);
	expect(((await resp.json()) as VendorResp).payments_blocked).toBe(false);
}

/** Insert an `approved`, USD invoice linked to `vendorId` straight into the
 *  tenant DB (the create API can't set vendor_id + approved). Returns the
 *  new invoice id. Derives org/entity from the vendor row. */
function insertApprovedInvoice(vendorId: string, invoiceNumber: string): string {
	const out = tenantPsql(
		`INSERT INTO invoices ` +
			`(id, correlation_id, invoice_number, vendor_name, vendor_id, amount, ` +
			`currency, status, organization_id, entity_id, created_at, updated_at) ` +
			`SELECT gen_random_uuid(), gen_random_uuid(), '${invoiceNumber}', v.name, v.id, ` +
			`250.00, 'USD', 'approved', v.organization_id, v.entity_id, now(), now() ` +
			`FROM vendors v WHERE v.id='${vendorId}' RETURNING id`,
		SLUG
	);
	return out
		.split('\n')
		.map((l) => l.trim())
		.filter(Boolean)[0]!;
}

async function createAndExecuteAchRun(
	page: import('@playwright/test').Page,
	invoiceId: string
): Promise<{ runId: string; exec: ExecuteResp }> {
	const create = await page.request.post(`${API_BASE}/api/payments/runs`, {
		headers: H,
		data: { items: [{ invoice_id: invoiceId, method: 'ach' }] }
	});
	expect(create.status(), 'create run').toBe(201);
	const runId = ((await create.json()) as { id: string }).id;

	const exec = await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
		headers: H
	});
	expect(exec.status(), 'execute run').toBe(200);
	return { runId, exec: (await exec.json()) as ExecuteResp };
}

async function getInvoiceStatus(
	page: import('@playwright/test').Page,
	id: string
): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, { headers: H });
	return ((await resp.json()) as { status: string }).status;
}

function cleanup(invoiceId: string | null, runId: string | null, vendorId: string | null): void {
	try {
		if (runId) {
			deletePaymentRunsWhere(`id='${runId}'`, SLUG);
		}
		if (invoiceId) {
			tenantPsql(`DELETE FROM payments WHERE invoice_id='${invoiceId}'`, SLUG);
			tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${invoiceId}'`, SLUG);
			tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${invoiceId}'`, SLUG);
			deleteInvoicesWhere(`id='${invoiceId}'`, SLUG);
		}
		if (vendorId) {
			deleteVendorsWhere(`id='${vendorId}'`, SLUG);
		}
	} catch {
		/* best-effort */
	}
}

test.describe('blocked vendor cannot be paid (domestic ACH)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendors');
		SLUG = slugFromPage(page);
		H = await authedTenantHeaders(page, SLUG);
		// Disable SoD so the admin session can both create and execute a run.
		// The SoD tests live in run-cfo-signoff.spec.ts.
		await patchOrg(page, { payments: { require_run_segregation: false } }, H);
	});

	test.afterEach(async ({ page }) => {
		await patchOrg(page, { payments: { require_run_segregation: true } }, H);
	});

	test('a manually-blocked vendor is refused a domestic-ACH payment', async ({ page }) => {
		let vendorId: string | null = null;
		let invoiceId: string | null = null;
		let runId: string | null = null;
		try {
			const vendor = await createVendor(page, `Blocked Manual Co ${Date.now()}`);
			vendorId = vendor.id;
			await blockVendor(page, vendor.id);

			invoiceId = insertApprovedInvoice(vendor.id, `BLK-MAN-${Date.now()}`);
			expect(invoiceId).toMatch(/[0-9a-f-]{36}/);

			const { runId: rid, exec } = await createAndExecuteAchRun(page, invoiceId);
			runId = rid;

			// The blocked vendor's payment must be REFUSED, not completed.
			expect(exec.payments_completed).toBe(0);
			expect(exec.payments_failed).toBe(1);

			// The Payment row is failed with a compliance refusal reason.
			const payStatus = tenantPsql(
				`SELECT status FROM payments WHERE invoice_id='${invoiceId}'`,
				SLUG
			).trim();
			expect(payStatus).toBe('failed');
			const reason = tenantPsql(
				`SELECT failure_reason FROM payments WHERE invoice_id='${invoiceId}'`,
				SLUG
			).trim();
			expect(reason).toContain('compliance_refusal');

			// The invoice must NOT have advanced to payment_scheduled — money
			// never moved.
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
		} finally {
			cleanup(invoiceId, runId, vendorId);
		}
	});

	test('a sanctions-match vendor (auto-blocked on create) is refused a domestic-ACH payment', async ({
		page
	}) => {
		let vendorId: string | null = null;
		let invoiceId: string | null = null;
		let runId: string | null = null;
		try {
			// The mock sanctions adapter flags this exact name as a list match,
			// so the create-time screen sets payments_blocked=true.
			const vendor = await createVendor(page, 'Sanctioned Test Entity');
			vendorId = vendor.id;
			expect(vendor.screening_status).toBe('match');
			expect(vendor.payments_blocked).toBe(true);

			invoiceId = insertApprovedInvoice(vendor.id, `BLK-SDN-${Date.now()}`);
			const { runId: rid, exec } = await createAndExecuteAchRun(page, invoiceId);
			runId = rid;

			expect(exec.payments_completed).toBe(0);
			expect(exec.payments_failed).toBe(1);
			expect(
				tenantPsql(`SELECT status FROM payments WHERE invoice_id='${invoiceId}'`, SLUG).trim()
			).toBe('failed');
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
		} finally {
			cleanup(invoiceId, runId, vendorId);
		}
	});

	test('after unblock, the same vendor can be paid via domestic ACH', async ({ page }) => {
		let vendorId: string | null = null;
		let invoiceId: string | null = null;
		let runId: string | null = null;
		try {
			const vendor = await createVendor(page, `Unblock Roundtrip Co ${Date.now()}`);
			vendorId = vendor.id;
			await blockVendor(page, vendor.id);
			await unblockVendor(page, vendor.id);

			invoiceId = insertApprovedInvoice(vendor.id, `BLK-RT-${Date.now()}`);
			const { runId: rid, exec } = await createAndExecuteAchRun(page, invoiceId);
			runId = rid;

			// Now the (clear) mock adapter completes the payment synchronously.
			expect(exec.payments_failed).toBe(0);
			expect(exec.payments_completed).toBe(1);
			expect(await getInvoiceStatus(page, invoiceId)).toBe('payment_scheduled');
		} finally {
			cleanup(invoiceId, runId, vendorId);
		}
	});
});
