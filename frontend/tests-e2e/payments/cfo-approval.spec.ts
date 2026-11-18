import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deletePaymentRunsWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

async function patchOrg(page: import('@playwright/test').Page, partial: object) {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: partial }
	});
}

async function createApprovedInvoice(
	page: import('@playwright/test').Page,
	suffix: string,
	amount: number
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E CFO Vendor',
			invoice_number: `E2E-CFO-${suffix}`,
			amount,
			currency: 'USD'
		}
	});
	expect(resp.status()).toBe(201);
	const body = (await resp.json()) as { id: string };
	// POST /api/invoices intentionally ignores a client-supplied status (the
	// status-injection fix — InvoiceCreate has no `status` field). Force the
	// row to `approved` and bind a real vendor_id (required by the compliance
	// gate in execute_payment_run — NULL vendor → pending_compliance) via SQL.
	const vendorId = tenantPsql(
		`SELECT id FROM vendors WHERE status='active' LIMIT 1`
	).trim();
	const sets = `status='approved'${vendorId ? `, vendor_id='${vendorId}'` : ''}`;
	tenantPsql(`UPDATE invoices SET ${sets} WHERE id='${body.id}'`);
	// 'E2E CFO Vendor' is fresh + these amounts are large, so create-time fraud
	// detection (now run at manual-entry creation, before vendor_id is
	// reassigned above) can raise an open fraud_flag ("new vendor + large
	// amount") — a PAYMENT_BLOCKING_EXCEPTION_TYPES member that would 409 the
	// payment-run creation these tests are actually about. Resolve it so the
	// CFO-threshold gate is tested in isolation from the fraud signal.
	tenantPsql(`UPDATE exceptions SET status='resolved' WHERE invoice_id='${body.id}'`);
	return body.id;
}

async function createRun(
	page: import('@playwright/test').Page,
	invoiceId: string
): Promise<{ id: string; requires_cfo_approval: boolean }> {
	const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {
		headers: await authedTenantHeaders(page),
		data: { items: [{ invoice_id: invoiceId, method: 'ach' }] }
	});
	const body = (await resp.json()) as { id: string; requires_cfo_approval: boolean };
	return body;
}

function hardDeleteInvoice(id: string): void {
	// The invoice's payments, and any run holding only them, go with it in
	// `deleteInvoicesWhere` below. (The two statements this replaced deleted the
	// payments first, which emptied the subquery the run delete selected by, so
	// every such run leaked as a draft.)
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	// 'E2E CFO Vendor' may auto-mint `unverified` on first use — refresh_warnings
	// (now run at manual-entry creation time) raises an `unverified_vendor`
	// exception against it, which FKs to this invoice and must clear first.
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	// audit_log is append-only (a BEFORE DELETE trigger raises) — an invoice
	// that ran through execute carries an invoice.payment_scheduled row that
	// cannot be deleted. Leave it orphaned (no FK to invoices); deleting the
	// invoice row is enough.
	deleteInvoicesWhere(`id='${id}'`);
}

function deletePaymentRun(runId: string): void {
	deletePaymentRunsWhere(`id='${runId}'`);
}

/**
 * CFO sign-off on high-value payment runs. The threshold lives in the
 * org settings (`payments.cfo_approval_above`). Each test sets a known
 * threshold and resets it in `finally` so the suite is hermetic.
 *
 * SoD note: `execute_payment_run` enforces maker-checker (the user who
 * creates a run cannot also execute it). These tests disable SoD via
 * `require_run_segregation: false` so a single admin session can both
 * create and execute, letting the CFO-approval gate fire. The explicit
 * SoD tests live in run-cfo-signoff.spec.ts.
 */
test.describe('/payments — CFO approval gate', () => {
	test.beforeEach(async ({ page }) => {
		await patchOrg(page, {
			payments: { cfo_approval_above: 1000, require_run_segregation: false }
		});
	});

	test.afterEach(async ({ page, tenantAdmin }) => {
		// A test may have signed this page in as CFO; restore the admin
		// session so the patch below goes through with admin scope.
		await signInAndWait(page, tenantAdmin);
		await patchOrg(page, {
			payments: { cfo_approval_above: null, require_run_segregation: true }
		});
	});

	test('runs over the threshold land with requires_cfo_approval=true', async ({ page }) => {
		const invoiceId = await createApprovedInvoice(page, `over-${Date.now()}`, 5000);
		try {
			const run = await createRun(page, invoiceId);
			expect(run.requires_cfo_approval).toBe(true);
		} finally {
			hardDeleteInvoice(invoiceId);
		}
	});

	test('runs at or below the threshold do not require CFO approval', async ({ page }) => {
		const invoiceId = await createApprovedInvoice(page, `under-${Date.now()}`, 500);
		try {
			const run = await createRun(page, invoiceId);
			expect(run.requires_cfo_approval).toBe(false);
		} finally {
			hardDeleteInvoice(invoiceId);
		}
	});

	test('execute is rejected with 403 while CFO approval pending', async ({ page }) => {
		const invoiceId = await createApprovedInvoice(page, `block-${Date.now()}`, 5000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			expect(run.requires_cfo_approval).toBe(true);

			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(resp.status()).toBe(403);
			// A coded refusal (decisions §461): the code is the contract the
			// browser localizes on; the English sentence still names the CFO.
			const body = (await resp.json()) as {
				detail: { code: string; message: string };
			};
			expect(body.detail.code).toBe('payment_run_awaiting_cfo');
			expect(body.detail.message).toContain('CFO');
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('non-CFO actor cannot approve (403)', async ({ page }) => {
		const invoiceId = await createApprovedInvoice(page, `nocfo-${Date.now()}`, 5000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;

			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/approve`,
				{ headers: await authedTenantHeaders(page) }
			);
			// Tenant admin doesn't hold the CFO role — gate is `require_roles(ROLE_CFO)`.
			expect(resp.status()).toBe(403);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('CFO approves → execute then runs end-to-end', async ({ page, tenantCfo }) => {
		const invoiceId = await createApprovedInvoice(page, `flow-${Date.now()}`, 5000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;

			// Sign in as the CFO and approve.
			await signInAndWait(page, tenantCfo);
			const headers = await authedTenantHeaders(page);
			const approveResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/approve`,
				{ headers }
			);
			expect(approveResp.status()).toBe(200);
			const approveBody = (await approveResp.json()) as { cfo_approved_by: string };
			expect(approveBody.cfo_approved_by).toBeTruthy();

			// Now execute (still as the CFO — the standard payments role set
			// includes CFO). Run flips to completed via the mock adapter.
			const execResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(execResp.status()).toBe(200);
			expect(((await execResp.json()) as { status: string }).status).toBe('completed');
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('approving a run that does not require approval is rejected with 409', async ({
		page,
		tenantCfo
	}) => {
		const invoiceId = await createApprovedInvoice(page, `noreq-${Date.now()}`, 100);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			expect(run.requires_cfo_approval).toBe(false);

			await signInAndWait(page, tenantCfo);
			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/approve`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(resp.status()).toBe(409);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});

test.describe('/payments — remittance PDF', () => {
	test.beforeEach(async ({ page }) => {
		// Disable SoD so the admin can both create and execute a run in the
		// same session. The SoD tests live in run-cfo-signoff.spec.ts.
		await patchOrg(page, { payments: { require_run_segregation: false } });
	});

	test.afterEach(async ({ page, tenantAdmin }) => {
		await signInAndWait(page, tenantAdmin);
		await patchOrg(page, { payments: { require_run_segregation: true } });
	});

	test('GET /payments/{id}/remittance returns a PDF for completed payments', async ({
		page
	}) => {
		const invoiceId = await createApprovedInvoice(page, `pdf-${Date.now()}`, 250);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			const headers = await authedTenantHeaders(page);
			await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
				headers
			});

			const detailResp = await page.request.get(
				`${API_BASE}/api/payments/runs/${runId}`,
				{ headers }
			);
			const detail = (await detailResp.json()) as {
				payments: Array<{ id: string; status: string }>;
			};
			const payment = detail.payments[0];
			expect(payment.status).toBe('completed');

			const pdfResp = await page.request.get(
				`${API_BASE}/api/payments/${payment.id}/remittance`,
				{ headers }
			);
			expect(pdfResp.status()).toBe(200);
			expect(pdfResp.headers()['content-type']).toContain('application/pdf');
			expect(pdfResp.headers()['content-disposition']).toContain('attachment');

			const buf = await pdfResp.body();
			// PDF magic byte signature.
			expect(buf.slice(0, 5).toString()).toBe('%PDF-');
			expect(buf.length).toBeGreaterThan(500); // not an empty stub
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});
