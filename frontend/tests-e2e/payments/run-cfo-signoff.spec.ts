import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, deletePaymentRunsWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * Payment-run money-out path — the load-bearing invariants the existing
 * payments/ specs stop short of, exercised deeper:
 *
 *   - Idempotency under a TRUE concurrent double-submit of /execute
 *     (Promise.all, not serial). Exactly one 200 + one 409, and exactly
 *     ONE completed payment row — no double-pay. `execute.spec.ts` only
 *     fires the two executes serially (first→200, second→409), which a
 *     non-atomic guard could still pass while double-charging under real
 *     concurrency. The `with_for_update` claim is the thing under test.
 *   - Every payment status transition writes an append-only audit row.
 *     The project invariant ("a status change that touches a regulated
 *     timestamp like completed_at without an audit row is Critical")
 *     was unverified for the execute path — and was in fact missing
 *     (fixed in app/api/payments.py: payment_run.executed +
 *     payment.<status> rows). These tests lock it.
 *   - The CFO sign-off threshold BOUNDARY: total == threshold does NOT
 *     require approval (`total > cfo_threshold`); total == threshold + a
 *     cent does. `cfo-approval.spec.ts` uses 500 vs 5000 against a 1000
 *     threshold — never the exact edge.
 *   - Approval composes with idempotency: an approved over-threshold run
 *     still 409s on a second execute (the approval gate doesn't reopen
 *     the draft window).
 *   - Concurrent void double-submit: exactly one 200 + one 409, exactly
 *     one payment.voided audit row (no double-void).
 *
 * Cleanup mirrors the sibling specs: executed runs are append-only by
 * design (no void-run endpoint) and payment_scheduled is an immutable
 * invoice status the PATCH endpoint refuses, so revert is direct SQL.
 *
 * SoD note: `check_run_segregation` in `execute_payment_run` enforces
 * maker-checker — the user who created (initiated) the run cannot also
 * execute it. Every test that calls /execute after /runs (admin created)
 * must sign in as the CFO or another user to satisfy this gate. The
 * existing token cache (`_tokenCache`) amortises the extra logins.
 */

type Page = import('@playwright/test').Page;

// This spec is almost entirely API-driven (it asserts the money-path
// invariants over `page.request` + direct SQL), so it has no reason to pay
// for — or depend on the timing of — the login UI. We opt out of the
// worker's shared pre-signed-in storage-state (its JWT is minted once per
// worker and can outlive the backend's 30-min token expiry on a long serial
// run → a 401 on the first API call) AND avoid the login-UI
// waitForURL-on-the-SPA (which flakes when the dev server is under load).
// Instead each role is authenticated by minting a JWT straight from
// `POST /api/auth/login` and writing it into localStorage the same way the
// frontend does — fast, deterministic, render-independent. MFA is off in
// dev, so login returns the access token directly.
test.use({ storageState: { cookies: [], origins: [] } });

// Token cache keyed by email. `POST /api/auth/login` is rate-limited to 10
// attempts/min per IP; this spec authenticates in nearly every test (admin
// baseline + a few CFO switches), which would blow that budget and 429. The
// JWT is valid for 30 min and the whole file runs in seconds, so we mint
// each role's token at most once and replay it into localStorage thereafter.
const _tokenCache = new Map<string, string>();

async function _mintToken(
	page: Page,
	creds: { email: string; password: string }
): Promise<string> {
	const cached = _tokenCache.get(creds.email);
	if (cached) return cached;
	const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
		data: { email: creds.email, password: creds.password }
	});
	expect(resp.status()).toBe(200);
	const token = ((await resp.json()) as { access_token: string }).access_token;
	expect(token).toBeTruthy();
	_tokenCache.set(creds.email, token);
	return token;
}

async function apiSignIn(page: Page, creds: { email: string; password: string }): Promise<void> {
	const token = await _mintToken(page, creds);
	// localStorage is per-origin; the page fixture has already navigated to
	// the worker's tenant root, so this writes under the right origin and
	// authToken(page) reads it back on the next authedTenantHeaders call.
	await page.evaluate((t) => localStorage.setItem('auth_token', t), token);
}

test.beforeEach(async ({ page, tenantAdmin }) => {
	await apiSignIn(page, tenantAdmin);
});

async function patchOrg(page: Page, partial: object): Promise<void> {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: partial }
	});
	expect(resp.status()).toBe(200);
}

async function createApprovedInvoice(page: Page, suffix: string, amount: number): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Run Signoff Vendor',
			invoice_number: `E2E-RUN-${suffix}`,
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
	// 'E2E Run Signoff Vendor' is fresh + these amounts can be large (CFO
	// threshold boundary tests), so create-time fraud detection (now run at
	// manual-entry creation, before vendor_id is reassigned above) can raise
	// an open exception (unverified_vendor, or fraud_flag for "new vendor +
	// large amount" — the latter is a PAYMENT_BLOCKING_EXCEPTION_TYPES
	// member that would 409 the payment-run creation these tests are
	// actually about). Resolve it so the run/CFO-gate logic under test is
	// exercised in isolation from the fraud signal.
	tenantPsql(`UPDATE exceptions SET status='resolved' WHERE invoice_id='${body.id}'`);
	return body.id;
}

async function createRun(
	page: Page,
	invoiceId: string,
	method = 'ach'
): Promise<{ id: string; requires_cfo_approval: boolean }> {
	const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {
		headers: await authedTenantHeaders(page),
		data: { items: [{ invoice_id: invoiceId, method }] }
	});
	expect(resp.status()).toBe(201);
	return (await resp.json()) as { id: string; requires_cfo_approval: boolean };
}

/**
 * Execute a payment run as the CFO (a different user from the admin who
 * created the run) to satisfy the maker-checker SoD gate in
 * `execute_payment_run`. Restores the admin session afterward so
 * subsequent calls in the same test use admin scope.
 */
async function executeRunAsCfo(
	page: Page,
	runId: string,
	tenantCfo: { email: string; password: string },
	tenantAdmin: { email: string; password: string }
): Promise<void> {
	await apiSignIn(page, tenantCfo);
	const resp = await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	await apiSignIn(page, tenantAdmin);
}

async function getRunPayment(page: Page, runId: string): Promise<{ id: string; status: string }> {
	const resp = await page.request.get(`${API_BASE}/api/payments/runs/${runId}`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { payments: Array<{ id: string; status: string }> };
	return body.payments[0];
}

async function getInvoiceStatus(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	return ((await resp.json()) as { status: string }).status;
}

/** Count audit rows for an action against a given entity id. */
function auditCount(action: string, entityId: string): number {
	const out = tenantPsql(
		`SELECT count(*) FROM audit_log WHERE action='${action}' AND entity_id='${entityId}'`
	);
	return parseInt(out.trim(), 10);
}

/** Count completed payments belonging to a run — the double-pay tripwire. */
function completedPaymentCount(runId: string): number {
	const out = tenantPsql(
		`SELECT count(*) FROM payments WHERE payment_run_id='${runId}' AND status='completed'`
	);
	return parseInt(out.trim(), 10);
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
	// Do NOT delete audit_log rows — the table is append-only (a BEFORE
	// DELETE trigger raises), and once an invoice runs through execute/void
	// it carries invoice.payment_scheduled / invoice.voided_return_to_approved
	// rows. Leaving them orphaned is harmless (audit_log has no FK to
	// invoices) and is the only DB-honest cleanup.
	deleteInvoicesWhere(`id='${id}'`);
}

function deletePaymentRun(runId: string): void {
	// NB: the run/payment audit rows (payment_run.executed, payment.<status>,
	// payment_run.cfo_approved/cancelled, payment.voided) are intentionally
	// NOT deleted — audit_log is append-only at the DB level (a trigger
	// rejects every DELETE), and that immutability is itself a money-path
	// invariant. The orphaned rows reference removed run/payment ids, which
	// is harmless (audit_log carries no FK) and each test counts rows for a
	// freshly-created run id, so prior residue never inflates a count.
	deletePaymentRunsWhere(`id='${runId}'`);
}

// ── Idempotency of execute under concurrent double-submit ──────────────

test.describe('/payments — execute idempotency (no double-pay)', () => {
	test('two concurrent /execute calls → exactly one 200 + one 409, exactly one completed payment', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		const invoiceId = await createApprovedInvoice(page, `idem-${Date.now()}`, 1234.56);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			// Execute as CFO (different user from admin who created the run) to
			// satisfy the maker-checker SoD gate. Both concurrent requests use
			// the same CFO token — the FOR UPDATE row lock serialises them.
			await apiSignIn(page, tenantCfo);
			const headers = await authedTenantHeaders(page);

			// Fire both executes truly concurrently. The FOR UPDATE row lock
			// in execute_payment_run is what must serialize them — without
			// it both read status='draft', both pass the guard, and the mock
			// adapter is charged twice.
			const [a, b] = await Promise.all([
				page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, { headers }),
				page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, { headers })
			]);
			const statuses = [a.status(), b.status()].sort();
			expect(statuses).toEqual([200, 409]);

			// The tripwire: exactly ONE completed payment for the run. Two
			// would mean the adapter ran twice — money moved twice.
			expect(completedPaymentCount(runId)).toBe(1);

			// And exactly one settlement audit row for that single payment.
			await apiSignIn(page, tenantAdmin);
			const payment = await getRunPayment(page, runId);
			expect(payment.status).toBe('completed');
			expect(auditCount('payment.completed', payment.id)).toBe(1);
		} finally {
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('a third execute after settlement is still 409 and writes no new audit row', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		const invoiceId = await createApprovedInvoice(page, `idem3-${Date.now()}`, 42.0);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;

			// Execute as CFO (SoD: admin created the run, CFO executes).
			await apiSignIn(page, tenantCfo);
			const cfoHeaders = await authedTenantHeaders(page);

			const first = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: cfoHeaders }
			);
			expect(first.status()).toBe(200);
			expect(auditCount('payment_run.executed', runId)).toBe(1);

			const again = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: cfoHeaders }
			);
			expect(again.status()).toBe(409);
			// No second run-executed row — the 409 short-circuits before any
			// state change or audit write.
			expect(auditCount('payment_run.executed', runId)).toBe(1);
		} finally {
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});

// ── Audit rows on each money-movement transition ───────────────────────

test.describe('/payments — append-only audit on every transition', () => {
	test('execute writes a payment_run.executed row + a payment.completed row', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		const invoiceId = await createApprovedInvoice(page, `audit-exec-${Date.now()}`, 777.0);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;

			// No execute audit rows exist before execution.
			expect(auditCount('payment_run.executed', runId)).toBe(0);

			// Execute as CFO (SoD: admin created the run, CFO executes).
			await apiSignIn(page, tenantCfo);
			const cfoHeaders = await authedTenantHeaders(page);

			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: cfoHeaders }
			);
			expect(resp.status()).toBe(200);

			// Run-level execution row.
			expect(auditCount('payment_run.executed', runId)).toBe(1);
			// Per-payment settlement row, recording the regulated transition
			// that set completed_at.
			await apiSignIn(page, tenantAdmin);
			const payment = await getRunPayment(page, runId);
			expect(auditCount('payment.completed', payment.id)).toBe(1);

			// PII guard: no bank/account fields leaked into the audit details.
			const detailsJson = tenantPsql(
				`SELECT coalesce(details::text,'') FROM audit_log WHERE action='payment.completed' AND entity_id='${payment.id}'`
			).toLowerCase();
			expect(detailsJson).not.toContain('account_number');
			expect(detailsJson).not.toContain('routing');
			expect(detailsJson).not.toContain('iban');
		} finally {
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('CFO approve writes payment_run.cfo_approved; void writes payment.voided', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		await patchOrg(page, { payments: { cfo_approval_above: 1000 } });
		const invoiceId = await createApprovedInvoice(page, `audit-cfo-${Date.now()}`, 6000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			expect(run.requires_cfo_approval).toBe(true);

			// CFO approves → cfo_approved audit row.
			// CFO is also a different user from admin (SoD for execute satisfied).
			await apiSignIn(page, tenantCfo);
			let headers = await authedTenantHeaders(page);
			const approveResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/approve`,
				{ headers }
			);
			expect(approveResp.status()).toBe(200);
			expect(auditCount('payment_run.cfo_approved', runId)).toBe(1);

			// Execute (CFO is in the payments role set), then void the
			// settled payment → payment.voided audit row.
			const execResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(execResp.status()).toBe(200);
			const payment = await getRunPayment(page, runId);
			expect(payment.status).toBe('completed');

			const voidResp = await page.request.post(
				`${API_BASE}/api/payments/${payment.id}/void`,
				{ headers, data: { reason: 'e2e audit check' } }
			);
			expect(voidResp.status()).toBe(200);
			expect(auditCount('payment.voided', payment.id)).toBe(1);
			// Void returns the invoice to approved (re-queue invariant).
			await apiSignIn(page, tenantAdmin);
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
			await apiSignIn(page, tenantAdmin);
			await patchOrg(page, { payments: { cfo_approval_above: null } });
		}
	});

	test('cancel writes a payment_run.cancelled row and re-queues the invoice', async ({
		page
	}) => {
		const invoiceId = await createApprovedInvoice(page, `audit-cancel-${Date.now()}`, 310.0);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			const headers = await authedTenantHeaders(page);

			// Cancel does not move money — no SoD check on cancel.
			const cancelResp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/cancel`,
				{ headers }
			);
			expect(cancelResp.status()).toBe(200);
			expect(auditCount('payment_run.cancelled', runId)).toBe(1);
			expect(await getInvoiceStatus(page, invoiceId)).toBe('approved');
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});

// ── CFO threshold boundary ─────────────────────────────────────────────

test.describe('/payments — CFO threshold boundary', () => {
	test.afterEach(async ({ page, tenantAdmin }) => {
		await apiSignIn(page, tenantAdmin);
		await patchOrg(page, { payments: { cfo_approval_above: null } });
	});

	test('total exactly equal to the threshold does NOT require CFO approval', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		await patchOrg(page, { payments: { cfo_approval_above: 1000 } });
		const invoiceId = await createApprovedInvoice(page, `eq-${Date.now()}`, 1000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			// Gate is `total > cfo_threshold` — equality is below the gate.
			expect(run.requires_cfo_approval).toBe(false);

			// Execute as CFO (SoD: admin created the run, CFO executes).
			await apiSignIn(page, tenantCfo);
			const resp = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(resp.status()).toBe(200);
		} finally {
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});

	test('one cent over the threshold requires CFO approval and 403s execute until signed off', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		await patchOrg(page, { payments: { cfo_approval_above: 1000 } });
		const invoiceId = await createApprovedInvoice(page, `over1c-${Date.now()}`, 1000.01);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			expect(run.requires_cfo_approval).toBe(true);

			// Execute as CFO (SoD: admin created run, CFO executes) — but the
			// run requires CFO APPROVAL before execution (different from who
			// executes). The CFO here is also the one who would approve, but
			// approval hasn't been granted yet, so this 403 is from the
			// CFO-approval gate, not the SoD gate.
			await apiSignIn(page, tenantCfo);
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
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});

// ── Approval composes with idempotency ─────────────────────────────────

test.describe('/payments — approval + idempotency compose', () => {
	test.afterEach(async ({ page, tenantAdmin }) => {
		await apiSignIn(page, tenantAdmin);
		await patchOrg(page, { payments: { cfo_approval_above: null } });
	});

	test('an approved over-threshold run still 409s on a second execute', async ({
		page,
		tenantCfo
	}) => {
		await patchOrg(page, { payments: { cfo_approval_above: 1000 } });
		const invoiceId = await createApprovedInvoice(page, `appidem-${Date.now()}`, 9000);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;
			expect(run.requires_cfo_approval).toBe(true);

			// CFO approves and then executes (satisfies both the approval gate
			// and SoD — admin created the run, CFO is a different user).
			await apiSignIn(page, tenantCfo);
			const headers = await authedTenantHeaders(page);
			expect(
				(await page.request.post(`${API_BASE}/api/payments/runs/${runId}/approve`, { headers })).status()
			).toBe(200);

			const first = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(first.status()).toBe(200);

			// Approval gate already passed once; the draft→executing claim is
			// terminal, so a replay is 409, not a second money movement.
			const second = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(second.status()).toBe(409);
			expect(completedPaymentCount(runId)).toBe(1);
		} finally {
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});

// ── Void idempotency under concurrent double-submit ────────────────────

test.describe('/payments — void idempotency (no double-void)', () => {
	test('two concurrent voids → exactly one 200 + one 409, exactly one payment.voided audit row', async ({
		page,
		tenantCfo,
		tenantAdmin
	}) => {
		const invoiceId = await createApprovedInvoice(page, `void-idem-${Date.now()}`, 480.0);
		let runId: string | null = null;
		try {
			const run = await createRun(page, invoiceId);
			runId = run.id;

			// Execute as CFO (SoD: admin created the run, CFO executes).
			await apiSignIn(page, tenantCfo);
			const cfoHeaders = await authedTenantHeaders(page);
			await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
				headers: cfoHeaders
			});
			// Restore admin session to read the payment status.
			await apiSignIn(page, tenantAdmin);
			const payment = await getRunPayment(page, runId);
			expect(payment.status).toBe('completed');

			// Void requires admin or CFO; use CFO headers still cached above
			// (both void concurrent requests use CFO — no SoD check on void,
			// only a role/permission check). Actually void needs PERM_PAYMENT_VOID
			// which maps to admin/cfo, not ap_manager. Use admin (already signed in).
			const adminHeaders = await authedTenantHeaders(page);
			const [a, b] = await Promise.all([
				page.request.post(`${API_BASE}/api/payments/${payment.id}/void`, {
					headers: adminHeaders,
					data: { reason: 'concurrent void A' }
				}),
				page.request.post(`${API_BASE}/api/payments/${payment.id}/void`, {
					headers: adminHeaders,
					data: { reason: 'concurrent void B' }
				})
			]);
			const statuses = [a.status(), b.status()].sort();
			expect(statuses).toEqual([200, 409]);

			// The FOR UPDATE lock in void_payment must serialize the two —
			// exactly one void audit row, never two.
			expect(auditCount('payment.voided', payment.id)).toBe(1);
		} finally {
			await apiSignIn(page, tenantAdmin);
			if (runId) deletePaymentRun(runId);
			hardDeleteInvoice(invoiceId);
		}
	});
});
