import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * Invoice lifecycle money path — the end-to-end state machine.
 *
 * Authoritative graph: backend/app/services/workflow_engine.py::VALID_TRANSITIONS
 *
 *   new → (submit) → ready_for_review → (approve) → approved
 *        → (send-to-erp) → sending_to_erp → sent_to_erp → … → done
 *   ready_for_review → (reject) → rejected → (resubmit) → ready_for_review
 *   payment_scheduled / paid → (void) → approved        (back-edge)
 *
 * Invariants this file locks down:
 *   - every status transition writes an APPEND-ONLY audit row;
 *   - approvals are RBAC-gated (ap_clerk cannot approve);
 *   - segregation of duties (the uploader cannot approve their own invoice);
 *   - the state machine REJECTS invalid jumps (409) and ACCEPTS the valid
 *     forward + back edges;
 *   - money survives every transition as exact Numeric (no float drift).
 *
 * Why the invoices are built fresh via the API rather than promoted from seed:
 * a status change is a workflow transition, and `PATCH /api/invoices/{id}`
 * deliberately DROPS the `status` field (a bare setattr would bypass
 * validate_transition + segregation + the immutable approval audit row), so
 * the ONLY way to move an invoice through the graph is the dedicated action
 * endpoints (`/complete`, `/approve`, `/reject`, `/resubmit`, `/send-to-erp`).
 * Each test creates its own invoice and deletes it in a `finally`, so the
 * worker's tenant is left as it was found — no reliance on a drained seed.
 *
 * NOTE on the RBAC / segregation-of-duties / void tests below: those three
 * sign a *second* user in (a clerk / manager) via `POST /api/auth/login` to
 * assert a cross-actor decision. `/api/auth/login` is rate-limited per client
 * IP (10 / 60 s). Under the local 4-worker model every worker hits that
 * endpoint from the same loopback IP, so the bucket can saturate and those
 * three tests get a 429 on the helper login. CI runs the e2e backend with
 * `FEOH_RATE_LIMIT_ENABLED=false` precisely for this reason (see
 * `.github/workflows/ci.yml`), and these tests are green there. The other
 * eight tests touch no login endpoint and pass unconditionally.
 */

type Page = import('@playwright/test').Page;

type Inv = { id: string; invoice_number: string; status: string; amount: number };

/** POST a fresh `new` invoice and return it. */
async function createNewInvoice(
	page: Page,
	overrides: Partial<{ invoice_number: string; vendor: string; amount: string }> = {}
): Promise<Inv> {
	const unique = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			invoice_number: overrides.invoice_number ?? `E2E-LIFE-${unique}`,
			vendor: overrides.vendor ?? 'Lifecycle Vendor',
			amount: overrides.amount ?? '1234.56',
			currency: 'USD',
			status: 'new'
		}
	});
	if (resp.status() !== 201) {
		throw new Error(`create invoice failed (${resp.status()}): ${await resp.text()}`);
	}
	const inv = (await resp.json()) as Inv;

	// Read-after-write race (docs/known-issues.md): the mutating request's
	// transaction commits in the dependency's post-yield code, which runs
	// AFTER the 201 is already sent — so a follow-up action (every test here
	// calls `complete` immediately) can 404 even though creation "succeeded".
	// Poll until the row is actually readable before handing it back, closing
	// the race for every caller at one shared point instead of per-test.
	await expect
		.poll(
			async () => (await page.request.get(`${API_BASE}/api/invoices/${inv.id}`, {
				headers: await authedTenantHeaders(page)
			})).status(),
			{ timeout: 5_000 }
		)
		.toBe(200);
	return inv;
}

async function getInvoice(page: Page, id: string): Promise<Inv> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
	if (resp.status() !== 200) throw new Error(`get invoice ${id} failed (${resp.status()})`);
	return (await resp.json()) as Inv;
}

/** Poll for an invoice to reach `expected`, rather than a single immediate
 *  GET — the backend commits a mutating request's transaction in the
 *  dependency's post-yield code, which runs AFTER the response is already
 *  sent to the client (see docs/known-issues.md § read-after-write race). A
 *  GET issued right after a 200 response can race that commit, especially
 *  once an extra round-trip (a second actor's login) sits between the write
 *  and the read. Same mitigation as the ERP-completion poll further down in
 *  this file — waiting on the real signal, not a fixed sleep. */
async function expectStatus(page: Page, id: string, expected: string): Promise<void> {
	await expect
		.poll(async () => (await getInvoice(page, id)).status, { timeout: 5_000 })
		.toBe(expected);
}

/** Action POST helper — returns the raw response so the caller asserts status. */
async function action(
	page: Page,
	id: string,
	verb: string,
	data?: Record<string, unknown>,
	creds?: { Authorization: string; 'X-Tenant-Slug': string }
) {
	return page.request.post(`${API_BASE}/api/invoices/${id}/${verb}`, {
		headers: creds ?? (await authedTenantHeaders(page)),
		data: data ?? {}
	});
}

/** The invoice's audit-trail action list, oldest→newest. */
async function auditActions(page: Page, id: string): Promise<string[]> {
	const resp = await page.request.get(`${API_BASE}/api/invoices/${id}/audit-log`, {
		headers: await authedTenantHeaders(page)
	});
	if (resp.status() !== 200) throw new Error(`audit-log ${id} failed (${resp.status()})`);
	const rows = (await resp.json()) as Array<{ action: string }>;
	return rows.map((r) => r.action);
}

// Token cache, keyed `slug:role`, so each role logs in AT MOST ONCE per worker
// process. `/api/auth/login` is rate-limited (10 / 60s / IP); a fresh login in
// every test would burst past it and 429. Memoizing both fixes that and keeps
// the cross-actor assertions deterministic.
const _roleTokenCache = new Map<string, string>();

/** Sign a second user in (a role other than the worker admin) and return the
 *  composed API headers for them — cached. Used for RBAC + SoD cross-actor
 *  assertions. */
async function headersForRole(
	page: Page,
	role: 'manager' | 'clerk' | 'cfo'
): Promise<{ Authorization: string; 'X-Tenant-Slug': string }> {
	const slug = (await authedTenantHeaders(page))['X-Tenant-Slug'];
	const cacheKey = `${slug}:${role}`;
	let token = _roleTokenCache.get(cacheKey);
	if (!token) {
		const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: `demo+${role}@${slug}.localhost`, password: 'demo' }
		});
		if (resp.status() !== 200) throw new Error(`login ${role} failed (${resp.status()})`);
		token = ((await resp.json()) as { access_token: string }).access_token;
		_roleTokenCache.set(cacheKey, token);
	}
	return { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': slug };
}

async function deleteInvoice(page: Page, id: string): Promise<void> {
	// Best-effort cleanup. Once an invoice reaches an immutable status
	// (sending_to_erp … done) DELETE 409s — accept that and move on; the
	// worker's tenant resets between sessions.
	await page.request.delete(`${API_BASE}/api/invoices/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}

test.describe('/invoices lifecycle — forward money path', () => {
	test.beforeEach(async ({ page }) => {
		// Land on the queue so localStorage (token) is populated for the API
		// request context, mirroring the other invoice specs.
		await page.goto('/invoices');
	});

	test('new → ready_for_review → approved → ERP → done, each step audited', async ({ page }) => {
		const inv = await createNewInvoice(page, { amount: '4242.42' });
		try {
			// new → ready_for_review (submit for review via the workflow).
			const submitted = await action(page, inv.id, 'complete');
			expect(submitted.status()).toBe(200);
			await expectStatus(page, inv.id, 'ready_for_review');

			// ready_for_review → approved. A manual-entry invoice now carries its
			// creator's uploaded_by_id (segregation of duties), so the approver
			// must be a different actor than the worker admin who created it —
			// same as the file-upload path always required.
			const manager = await headersForRole(page, 'manager');
			const approved = await action(page, inv.id, 'approve', {}, manager);
			expect(approved.status()).toBe(200);
			await expectStatus(page, inv.id, 'approved');
			const afterApprove = await getInvoice(page, inv.id);
			// Money survives the transition byte-for-byte (Numeric, no drift).
			expect(afterApprove.amount).toBe(4242.42);
			expect(tenantPsql(`select amount from invoices where id='${inv.id}'`).trim()).toBe(
				'4242.42'
			);

			// approved → sending_to_erp (then the mock ERP adapter drives it
			// forward to a terminal/near-terminal state asynchronously).
			const sent = await action(page, inv.id, 'send-to-erp');
			expect(sent.status()).toBe(202);

			// Poll the real status field — never a fixed sleep — until the mock
			// ERP chain settles. It always lands on `done` (sent_to_erp → done)
			// or, in the posted variant, posted_in_erp → done.
			await expect
				.poll(async () => (await getInvoice(page, inv.id)).status, { timeout: 15_000 })
				.toBe('done');

			// Append-only audit trail: every transition wrote its own row, in
			// order. `approved`, the ERP submit, and the completion are all present.
			const actions = await auditActions(page, inv.id);
			expect(actions).toContain('invoice.submitted_for_review');
			expect(actions).toContain('invoice.approved');
			expect(actions).toContain('invoice.erp_submitted');
			expect(actions).toContain('invoice.completed');
			// Ordering invariant: submitted precedes approved precedes erp submit.
			expect(actions.indexOf('invoice.submitted_for_review')).toBeLessThan(
				actions.indexOf('invoice.approved')
			);
			expect(actions.indexOf('invoice.approved')).toBeLessThan(
				actions.indexOf('invoice.erp_submitted')
			);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('approval is signed for SOX non-repudiation (immutable signature on the audit row)', async ({
		page
	}) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');
			// A different actor than the creator — segregation of duties.
			await action(page, inv.id, 'approve', {}, await headersForRole(page, 'manager'));
			await expectStatus(page, inv.id, 'approved');

			// The committed .env.development sets FEOH_APPROVAL_SIGNING_KEY to a
			// (non-secret) dev value, so the approval is signed and re-verifies
			// true. A post-approval tamper would flip this to valid:false.
			const verify = await page.request.get(
				`${API_BASE}/api/audit/invoice/${inv.id}/verify-signatures`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(verify.status()).toBe(200);
			const body = (await verify.json()) as {
				signing_configured: boolean;
				approvals: Array<{ signed: boolean; valid: boolean }>;
			};
			expect(body.signing_configured).toBe(true);
			expect(body.approvals.length).toBeGreaterThan(0);
			// Every signed approval verifies; the seeded dev signing key makes
			// at least the one approval above signed + valid.
			expect(body.approvals.every((a) => a.signed && a.valid)).toBe(true);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

test.describe('/invoices lifecycle — reject / rework loop', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
	});

	test('ready_for_review → rejected → resubmit → ready_for_review, with an exception + audit rows', async ({
		page
	}) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');
			await expectStatus(page, inv.id, 'ready_for_review');

			// Reject creates a review_rejected exception and writes an audit row.
			const rejected = await action(page, inv.id, 'reject', { reason: 'e2e rework reason' });
			expect(rejected.status()).toBe(200);
			await expectStatus(page, inv.id, 'rejected');

			const exResp = await page.request.get(`${API_BASE}/api/exceptions`, {
				headers: await authedTenantHeaders(page)
			});
			const exBody = await exResp.json();
			const exItems = (Array.isArray(exBody) ? exBody : (exBody.items ?? [])) as Array<{
				invoice_id: string;
				exception_type: string;
			}>;
			expect(
				exItems.some(
					(e) => e.invoice_id === inv.id && e.exception_type === 'review_rejected'
				)
			).toBe(true);

			// rejected → ready_for_review (the rework back-edge).
			const resubmitted = await action(page, inv.id, 'resubmit');
			expect(resubmitted.status()).toBe(200);
			await expectStatus(page, inv.id, 'ready_for_review');

			const actions = await auditActions(page, inv.id);
			expect(actions).toContain('invoice.rejected');
			expect(actions).toContain('invoice.resubmitted');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('reject reason is required — empty reason is a 422, no transition', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');
			const resp = await action(page, inv.id, 'reject', { reason: '' });
			expect(resp.status()).toBe(422);
			// State unchanged — the failed validation did not move the invoice.
			await expectStatus(page, inv.id, 'ready_for_review');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

test.describe('/invoices lifecycle — invalid-transition guards (409)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
	});

	test('cannot send a NEW (unapproved) invoice to ERP', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			const resp = await action(page, inv.id, 'send-to-erp');
			// new ∉ {sending_to_erp …} → validate_transition raises 409.
			expect(resp.status()).toBe(409);
			await expectStatus(page, inv.id, 'new');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('cannot re-approve an already-approved invoice (one-way edge)', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');
			// Segregation of duties checks against the ORIGINAL creator
			// (uploaded_by_id) regardless of who approves, so both calls here
			// need a different actor than the worker admin who created the
			// invoice — otherwise the second call would 403 on SoD instead of
			// exercising the invalid-transition 409 this test is actually about.
			const manager = await headersForRole(page, 'manager');
			await action(page, inv.id, 'approve', {}, manager);
			await expectStatus(page, inv.id, 'approved');

			const again = await action(page, inv.id, 'approve', {}, manager);
			// approved ∉ {approved} as a target → 409.
			expect(again.status()).toBe(409);
			await expectStatus(page, inv.id, 'approved');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('cannot resubmit an invoice already in ready_for_review', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');
			await expectStatus(page, inv.id, 'ready_for_review');

			const resp = await action(page, inv.id, 'resubmit');
			// ready_for_review → ready_for_review is not a valid edge → 409.
			expect(resp.status()).toBe(409);
			await expectStatus(page, inv.id, 'ready_for_review');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

test.describe('/invoices lifecycle — RBAC + segregation of duties on approval', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
	});

	test('an ap_clerk cannot approve (403); a manager can (200)', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			await action(page, inv.id, 'complete');

			const clerk = await headersForRole(page, 'clerk');
			const clerkApprove = await action(page, inv.id, 'approve', {}, clerk);
			expect(clerkApprove.status()).toBe(403);
			await expectStatus(page, inv.id, 'ready_for_review');

			const manager = await headersForRole(page, 'manager');
			const mgrApprove = await action(page, inv.id, 'approve', {}, manager);
			expect(mgrApprove.status()).toBe(200);
			await expectStatus(page, inv.id, 'approved');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});

	test('segregation of duties: the uploader cannot approve their own invoice', async ({ page }) => {
		const inv = await createNewInvoice(page);
		try {
			// createNewInvoice already stamps the worker admin as uploaded_by_id
			// (manual-entry create sets it, same as the file-upload path always
			// did) — re-asserting it explicitly here documents the precondition
			// this test depends on rather than relying on it implicitly.
			const me = await page.request.get(`${API_BASE}/api/auth/me`, {
				headers: await authedTenantHeaders(page)
			});
			const adminId = ((await me.json()) as { id: string }).id;
			tenantPsql(`update invoices set uploaded_by_id='${adminId}' where id='${inv.id}'`);

			await action(page, inv.id, 'complete');

			// Same user (the uploader) tries to approve → 403 SoD.
			const selfApprove = await action(page, inv.id, 'approve');
			expect(selfApprove.status()).toBe(403);
			// Coded (`api/refusals.coded_refusal`), so a client states it in the
			// reader's language rather than rendering the server's English.
			const refusal = ((await selfApprove.json()) as { detail: { code: string } }).detail;
			expect(refusal.code).toBe('approval_segregation');
			await expectStatus(page, inv.id, 'ready_for_review');

			// A different approver (the manager) is allowed → 200.
			const manager = await headersForRole(page, 'manager');
			const mgrApprove = await action(page, inv.id, 'approve', {}, manager);
			expect(mgrApprove.status()).toBe(200);
			await expectStatus(page, inv.id, 'approved');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

test.describe('/invoices lifecycle — void back-edge to approved', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
	});

	test('voiding a payment returns a payment_scheduled invoice to approved + audits it', async ({
		page
	}) => {
		const inv = await createNewInvoice(page, { amount: '500.00' });
		try {
			await action(page, inv.id, 'complete');
			// A different actor than the creator — segregation of duties.
			await action(page, inv.id, 'approve', {}, await headersForRole(page, 'manager'));
			await expectStatus(page, inv.id, 'approved');

			// Stand up the minimal "scheduled payment" state the void path
			// reverses: a completed Payment row on a payment_scheduled invoice.
			// Driving a full payment-run execution is the payments domain; here
			// we only assert the INVOICE-side back-edge (payment_scheduled →
			// approved) that lives in the workflow state machine. tenantPsql is
			// the documented escape hatch for state the API doesn't expose.
			const corr = tenantPsql(
				`select correlation_id from invoices where id='${inv.id}'`
			).trim();
			const paymentId = crypto.randomUUID();
			tenantPsql(
				`begin; ` +
					`update invoices set status='payment_scheduled' where id='${inv.id}'; ` +
					`insert into payments (id, invoice_id, amount, status, correlation_id, method, created_at, updated_at) ` +
					`values ('${paymentId}','${inv.id}',500.00,'completed','${corr}','ach',now(),now()); ` +
					`commit;`
			);
			await expectStatus(page, inv.id, 'payment_scheduled');

			// Void is admin/CFO-gated; the worker admin qualifies.
			const voided = await page.request.post(`${API_BASE}/api/payments/${paymentId}/void`, {
				headers: await authedTenantHeaders(page),
				data: { reason: 'e2e void back-edge' }
			});
			expect(voided.status()).toBe(200);

			// The back-edge fired: invoice is back in approved, re-queued for payment.
			await expectStatus(page, inv.id, 'approved');

			// And it's on the immutable trail.
			expect(await auditActions(page, inv.id)).toContain('invoice.voided_return_to_approved');

			// Cleanup the synthetic payment so DELETE can cascade the invoice.
			tenantPsql(`delete from payments where id='${paymentId}'`);
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});

test.describe('/invoices lifecycle — approve through the real modal UI', () => {
	// This one drives the SvelteKit UI end to end (the most-walked path),
	// complementing the API-level coverage above. The invoice is created via
	// the API, then approved by the signed-in worker admin straight from the
	// modal — exactly the journey a real approver walks. A UI-driven test
	// can't easily swap to a second browser identity mid-test the way the
	// API-level specs do via headersForRole, so uploaded_by_id (now always
	// stamped on manual-entry create) is nulled back out directly — this
	// test is about the modal wiring the click through to the endpoint, not
	// segregation of duties (that has its own dedicated test above).
	test('clicking Approve in the modal flips the invoice to approved', async ({ page }) => {
		await page.goto('/invoices');
		const inv = await createNewInvoice(page);
		tenantPsql(`update invoices set uploaded_by_id=NULL where id='${inv.id}'`);
		await action(page, inv.id, 'complete');
		await expectStatus(page, inv.id, 'ready_for_review');

		try {
			// Reload the queue so the freshly-promoted row is rendered.
			await page.goto('/invoices');

			await page
				.locator('table tbody tr', { hasText: inv.invoice_number })
				.first()
				.getByRole('button', { name: 'Edit' })
				.click();
			const modal = page.locator('div.modal[role="dialog"]');
			await expect(modal).toBeVisible();
			await expect(modal.locator('.review-section .review-title')).toHaveText('Review');

			const approved = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/invoices/${inv.id}/approve`) &&
					r.request().method() === 'POST' &&
					r.status() === 200
			);
			await modal.getByRole('button', { name: /^Approve$/ }).click();
			await approved;
			await expect(modal).toBeHidden({ timeout: 5_000 });

			await expectStatus(page, inv.id, 'approved');
		} finally {
			await deleteInvoice(page, inv.id);
		}
	});
});
