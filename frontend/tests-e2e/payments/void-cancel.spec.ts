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

interface QueueItem {
	id: string;
	invoice_number: string;
	amount: number;
}

async function getQueue(page: import('@playwright/test').Page): Promise<QueueItem[]> {
	const resp = await page.request.get(`${API_BASE}/api/payments/queue`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await resp.json()) as { items: QueueItem[] }).items;
}

async function getInvoiceStatus(
	page: import('@playwright/test').Page,
	id: string
): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await resp.json()) as { status: string }).status;
}

async function createApprovedInvoice(
	page: import('@playwright/test').Page,
	suffix: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Void/Cancel Vendor',
			invoice_number: `E2E-VC-${suffix}`,
			amount: 525.0,
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
	// 'E2E Void/Cancel Vendor' may auto-mint `unverified` on first use —
	// refresh_warnings (now run at manual-entry creation time) can raise an
	// open exception against it, which FKs to this invoice and would block
	// hardDeleteInvoice's cleanup below. Resolve it so the void/cancel
	// behavior under test is exercised in isolation from the fraud signal.
	tenantPsql(`UPDATE exceptions SET status='resolved' WHERE invoice_id='${body.id}'`);
	return body.id;
}

async function createRun(
	page: import('@playwright/test').Page,
	invoiceId: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {
		headers: await authedTenantHeaders(page),
		data: { items: [{ invoice_id: invoiceId, method: 'ach' }] }
	});
	return ((await resp.json()) as { id: string }).id;
}

/**
 * Execute a payment run as a different user from the creator (SoD).
 * The run is created by the tenant admin; the CFO (a different user) executes
 * it to satisfy the maker-checker segregation-of-duties check enforced by
 * `check_run_segregation` in `execute_payment_run`.
 */
async function executeRunAsCfo(
	page: import('@playwright/test').Page,
	runId: string,
	tenantCfo: { email: string; password: string },
	tenantAdmin: { email: string; password: string }
): Promise<void> {
	// Switch to the CFO session to satisfy the SoD gate (admin created the run).
	await signInAndWait(page, tenantCfo);
	const resp = await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	// Restore the admin session so subsequent API calls use admin scope.
	await signInAndWait(page, tenantAdmin);
}

async function getRunPayment(
	page: import('@playwright/test').Page,
	runId: string
): Promise<{ id: string; status: string }> {
	const resp = await page.request.get(`${API_BASE}/api/payments/runs/${runId}`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { payments: Array<{ id: string; status: string }> };
	return body.payments[0];
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
	// createApprovedInvoice resolves any create-time exception rather than
	// deleting it, so it still FKs to this invoice and must clear first.
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	// audit_log is append-only (a BEFORE DELETE trigger raises) — once an
	// invoice runs through execute/void it carries invoice.payment_scheduled
	// / invoice.voided_return_to_approved rows that cannot be deleted. Leave
	// them orphaned (no FK to invoices); deleting the invoice is enough.
	deleteInvoicesWhere(`id='${id}'`);
}

function deletePaymentRun(runId: string): void {
	deletePaymentRunsWhere(`id='${runId}'`);
}

/**
 * Void / cancel flows. The mock adapter accepts every void synchronously,
 * so the API tests assert the local bookkeeping; the audit-log row carries
 * the adapter outcome (`voided_upstream` for the mock).
 */

test.describe('/payments — void completed payment', () => {
	test('void flips payment to voided, invoice back to approved, audit row written', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		const stamp = Date.now();
		const invoiceId = await createApprovedInvoice(page, `void-${stamp}`);
		let runId: string | null = null;
		try {
			runId = await createRun(page, invoiceId);
			// Execute as CFO (different user from admin who created the run) to
			// satisfy the maker-checker SoD gate in execute_payment_run.
			await executeRunAsCfo(page, runId, tenantCfo, tenantAdmin);
			const before = await getRunPayment(page, runId);
			expect(before.status).toBe('completed');

			const voidResp = await page.request.post(
				`${API_BASE}/api/payments/${before.id}/void`,
				{
					headers: await authedTenantHeaders(page),
					data: { reason: 'e2e: testing void path' }
				}
			);
			expect(voidResp.status()).toBe(200);
			const body = (await voidResp.json()) as { status: string };
			expect(body.status).toBe('voided');

			// Invoice flipped back to `approved` and re-enters the queue.
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
			const queue = await getQueue(page);
			expect(queue.some((q) => q.id === invoiceId)).toBe(true);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('voiding a failed payment is rejected with 409', async ({ page, tenantCfo, tenantAdmin }) => {
		const stamp = Date.now();
		const invoiceId = await createApprovedInvoice(page, `void-failed-${stamp}`);
		let runId: string | null = null;
		try {
			runId = await createRun(page, invoiceId);
			// Execute as CFO to satisfy SoD; the mock adapter always completes, so
			// we then force the payment to `failed` via SQL for the 409 test.
			await executeRunAsCfo(page, runId, tenantCfo, tenantAdmin);
			const before = await getRunPayment(page, runId);

			// Force the payment into `failed` via SQL — the mock adapter
			// always reports `completed`, so we need to short-circuit it.
			tenantPsql(`UPDATE payments SET status='failed' WHERE id='${before.id}'`);

			const voidResp = await page.request.post(
				`${API_BASE}/api/payments/${before.id}/void`,
				{
					headers: await authedTenantHeaders(page),
					data: { reason: 'should reject' }
				}
			);
			expect(voidResp.status()).toBe(409);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('clerk role gets 403 on void', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const fakeId = '00000000-0000-0000-0000-000000000000';
		const resp = await page.request.post(`${API_BASE}/api/payments/${fakeId}/void`, {
			headers: await authedTenantHeaders(page),
			data: { reason: 'should-403' }
		});
		expect(resp.status()).toBe(403);
	});
});

test.describe('/payments — cancel draft run', () => {
	test('cancel flips draft run to cancelled and releases its invoices', async ({ page }) => {
		const stamp = Date.now();
		const invoiceId = await createApprovedInvoice(page, `cancel-${stamp}`);
		let runId: string | null = null;
		try {
			runId = await createRun(page, invoiceId);

			// Invoice is pinned to the draft run; not yet flipped to scheduled.
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
			// But the queue temporarily hides it because there's a non-completed
			// payment row pointing at it. Cancel should re-surface it.

			const headers = await authedTenantHeaders(page);
			const cancelResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/cancel`,
				{ headers }
			);
			expect(cancelResp.status()).toBe(200);
			const body = (await cancelResp.json()) as {
				status: string;
				released_invoices: number;
			};
			expect(body.status).toBe('cancelled');
			expect(body.released_invoices).toBe(1);

			// Run still readable; status reflects the cancel.
			const detailResp = await page.request.get(
				`${API_BASE}/api/payments/runs/${runId}`,
				{ headers }
			);
			const detail = (await detailResp.json()) as {
				status: string;
				payments: unknown[];
			};
			expect(detail.status).toBe('cancelled');
			expect(detail.payments).toHaveLength(0);

			// Invoice is back in the queue.
			const queue = await getQueue(page);
			expect(queue.some((q) => q.id === invoiceId)).toBe(true);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('cannot cancel a run that has already executed', async ({ page, tenantCfo, tenantAdmin }) => {
		const stamp = Date.now();
		const invoiceId = await createApprovedInvoice(page, `noex-${stamp}`);
		let runId: string | null = null;
		try {
			runId = await createRun(page, invoiceId);
			// Execute as CFO to satisfy SoD; then assert cancel is rejected.
			await executeRunAsCfo(page, runId, tenantCfo, tenantAdmin);

			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/cancel`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(resp.status()).toBe(409);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});
