import {
	API_BASE,
	authedTenantHeaders,
	expect,
	firstPayableQueueInvoice,
	loadMoreUntilRow,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

interface QueueItem {
	id: string;
	invoice_number: string;
	amount: number;
}

async function getQueue(page: import('@playwright/test').Page): Promise<QueueItem[]> {
	const resp = await page.request.get(`${API_BASE}/api/payments/queue`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status(), 'GET /api/payments/queue').toBe(200);
	const body = (await resp.json()) as { items: QueueItem[] };
	return body.items;
}

/**
 * Create a draft run over one invoice via the API, and prove it was created.
 *
 * The status is asserted with the server's own refusal in the message, so a
 * 409 (a blocked invoice, a live payment) fails HERE, by name — not three
 * steps later as an undefined run id.
 */
async function createRun(
	page: import('@playwright/test').Page,
	invoiceId: string,
	method: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {
		headers: await authedTenantHeaders(page),
		data: { items: [{ invoice_id: invoiceId, method }] }
	});
	expect(resp.status(), `POST /api/payments/runs: ${await resp.text()}`).toBe(201);
	const runId = ((await resp.json()) as { id: string }).id;
	expect(runId).toMatch(UUID_RE);
	return runId;
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

/** Disable / re-enable the maker-checker run-segregation gate. */
async function patchOrg(
	page: import('@playwright/test').Page,
	partial: object
): Promise<void> {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: partial }
	});
}

/**
 * Force-revert an invoice's status. PATCH /invoices rejects updates to
 * IMMUTABLE_STATUSES (payment_scheduled, paid, posted_in_erp, ...) by
 * design — execute moves the invoice into one of those, and there is
 * no UI path back. Direct SQL is the only revertible option for tests.
 */
function resetInvoiceStatus(id: string, status: string): void {
	tenantPsql(`UPDATE invoices SET status='${status}' WHERE id='${id}'`);
}

/**
 * Hard-purge a payment run + its payments. The product API has no
 * "void run" endpoint by design (executed runs are append-only for
 * audit), so direct SQL is the only revertible path. psql lives on dev
 * workstations and in the CI backend container.
 */
function deletePaymentRun(runId: string): void {
	// Refuse rather than run `WHERE payment_run_id='undefined'`, which matches
	// nothing, succeeds, and hides whatever left the id unset.
	if (!UUID_RE.test(runId)) {
		throw new Error(`deletePaymentRun: not a run id: ${String(runId)}`);
	}
	tenantPsql(`DELETE FROM payments WHERE payment_run_id='${runId}'`);
	tenantPsql(`DELETE FROM payment_runs WHERE id='${runId}'`);
}

/**
 * /payments — full Create-Draft → Execute round-trip via the mock
 * adapter. The mock returns `completed` synchronously, so by the time
 * the execute response lands:
 *   - run.status        flips draft → completed
 *   - payment.status    flips pending → completed
 *   - invoice.status    flips approved → payment_scheduled
 *
 * Each test reverts via direct DB cleanup + invoice PATCH back to the
 * source status.
 *
 * SoD note: `execute_payment_run` enforces maker-checker (the user who
 * creates a run cannot also execute it). Tests that use the same admin
 * session for both operations disable SoD via `require_run_segregation:
 * false` — a legitimate per-org single-operator configuration — and reset
 * it afterward. Tests that explicitly cover SoD live in run-cfo-signoff.spec.ts.
 *
 * Arming note: Execute is a two-click armed commit (`docs/followups.md` item 7),
 * matching the `Cancel run` sibling in the same footer. The first click ARMS —
 * the button relabels from "Execute · <amount>" to "Confirm execute · <amount>"
 * — and only the second click moves money. Every UI execute below clicks twice,
 * and the label change is a real DOM signal to wait on, never a timer.
 */

/** Click the footer's Execute, then its armed confirmation. */
async function armAndExecute(modal: import('@playwright/test').Locator): Promise<void> {
	await modal.getByRole('button', { name: /^Execute/ }).click();
	// The armed control is a DISTINCT accessible name, so this wait is a real
	// signal that the arm landed — no sleep, no retry.
	const confirm = modal.getByRole('button', { name: /^Confirm execute/ });
	await expect(confirm).toBeVisible();
	await confirm.click();
}

test.describe('/payments execute', () => {
	test.beforeEach(async ({ page }) => {
		// Disable maker-checker SoD so a single admin can both create and
		// execute a run within the same UI session. This is a valid per-org
		// configuration (single-operator accounts). SoD enforcement is tested
		// in run-cfo-signoff.spec.ts.
		await patchOrg(page, { payments: { require_run_segregation: false } });
		// KEPT deliberately. Wait for the page to settle before navigating —
		// avoids ERR_ABORTED when the pre-navigated tenant root still has
		// in-flight HMR/redirect requests outstanding from the storageState
		// fixture's initial goto. Unlike every other `networkidle` in this
		// directory this one is about the OUTGOING page, not about content
		// arriving, so there is no single request to name in its place.
		//
		// The one that used to sit after the navigation is gone: every test in
		// this describe opens with a `page.request` call, whose only
		// precondition is the same-origin document `goto` already guarantees.
		await page.waitForLoadState('networkidle');
		await page.goto('/payments');
	});

	test.afterEach(async ({ page, tenantAdmin }) => {
		// Restore the admin session (a test may have switched to a different role)
		// and re-enable SoD so the org setting is hermetic across the suite.
		await signInAndWait(page, tenantAdmin);
		await patchOrg(page, { payments: { require_run_segregation: true } });
	});

	test('Create Draft → Execute flips run + payment + invoice statuses', async ({
		page
	}) => {
		const target = await firstPayableQueueInvoice(page);
		const sourceStatus = target.status;

		let runId: string | null = null;
		try {
			// Select the payable row and Review & Pay. The queue pages, and the
			// row need not be on the first page, so page to it.
			const row = page.locator('table tbody tr', { hasText: target.invoice_number }).first();
			await loadMoreUntilRow(page, row);
			const checkbox = row.locator('input[type="checkbox"]');
			await expect(checkbox).toBeEnabled();
			await checkbox.check();
			await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();

			// Create the draft via the Review panel button. The frontend
			// then opens the RunDetailModal automatically. Match the POST
			// whatever its status, and assert the status: filtering on 201
			// here turned a refusal into a 30s wait for a response that had
			// already arrived.
			const runCreated = page.waitForResponse(
				(r) => r.url().endsWith('/api/payments/runs') && r.request().method() === 'POST'
			);
			await page.locator('.review-panel .btn-execute').click();
			const createdResp = await runCreated;
			expect(createdResp.status(), `POST /api/payments/runs: ${await createdResp.text()}`).toBe(
				201
			);
			runId = ((await createdResp.json()) as { id: string }).id;
			expect(runId).toMatch(UUID_RE);

			const modal = page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
			await expect(modal).toBeVisible();
			await expect(modal.locator('.status-badge')).toHaveText('Draft');

			// Execute.
			const executed = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/payments/runs/${runId}/execute`) &&
					r.request().method() === 'POST' &&
					r.status() === 200
			);
			await armAndExecute(modal);
			const execResp = await executed;
			const execBody = (await execResp.json()) as {
				status: string;
				payments_completed: number;
			};
			expect(execBody.status).toBe('completed');
			expect(execBody.payments_completed).toBeGreaterThan(0);

			// Modal reloads; the status badge now reads "Completed".
			await expect(modal.locator('.status-badge')).toHaveText('Completed', {
				timeout: 5_000
			});

			// Invoice status flipped per the execute logic in payments.py.
			const after = await getInvoiceStatus(page, target.id);
			expect(after).toBe('payment_scheduled');
		} finally {
			if (runId) deletePaymentRun(runId);
			resetInvoiceStatus(target.id, sourceStatus);
		}
	});

	test('a single Execute click ARMS and moves no money', async ({ page }) => {
		// Regression guard for `docs/followups.md` item 7: Execute used to be a
		// single unarmed click on the one irreversible money-moving control in
		// the app. The first click must only arm.
		const target = await firstPayableQueueInvoice(page);
		const sourceStatus = target.status;
		const headers = await authedTenantHeaders(page);

		const runId = await createRun(page, target.id, 'ach');

		// Count every execute POST the page fires. A request is initiated
		// synchronously inside the click handler, so once the armed control has
		// rendered we know whether the click called the API — no sleep needed.
		let executeCalls = 0;
		page.on('request', (req) => {
			if (req.method() === 'POST' && req.url().includes(`/runs/${runId}/execute`)) {
				executeCalls += 1;
			}
		});

		try {
			await page.reload();
			await page.locator('.tab', { hasText: 'Runs' }).click();
			await page
				.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` })
				.click();

			const modal = page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
			await expect(modal).toBeVisible();
			await expect(modal.locator('.status-badge')).toHaveText('Draft');

			// ONE click: arms only.
			await modal.getByRole('button', { name: /^Execute/ }).click();
			await expect(modal.getByRole('button', { name: /^Confirm execute/ })).toBeVisible();
			await expect(modal.getByTestId('execute-armed-note')).toBeVisible();
			expect(executeCalls).toBe(0);

			// Server-side proof: the run is untouched.
			const stillDraft = await page.request.get(`${API_BASE}/api/payments/runs/${runId}`, {
				headers
			});
			expect(((await stillDraft.json()) as { status: string }).status).toBe('draft');
		} finally {
			deletePaymentRun(runId);
			resetInvoiceStatus(target.id, sourceStatus);
		}
	});

	test('arming Cancel run disarms Execute (only one commit is ever armed)', async ({
		page
	}) => {
		const target = await firstPayableQueueInvoice(page);
		const sourceStatus = target.status;

		const runId = await createRun(page, target.id, 'ach');

		try {
			await page.reload();
			await page.locator('.tab', { hasText: 'Runs' }).click();
			await page
				.getByRole('button', { name: `View payment run ${runId.slice(0, 8)}` })
				.click();

			const modal = page.locator('div.modal[role="dialog"][aria-label="Payment run"]');
			await expect(modal).toBeVisible();

			await modal.getByRole('button', { name: /^Execute/ }).click();
			await expect(modal.getByRole('button', { name: /^Confirm execute/ })).toBeVisible();

			// Arming the sibling retracts the money button — two armed red
			// controls side by side is how a mis-click becomes the wrong
			// irreversible action.
			await modal.getByRole('button', { name: 'Cancel run' }).click();
			await expect(modal.getByRole('button', { name: 'Confirm cancel' })).toBeVisible();
			await expect(modal.getByRole('button', { name: /^Confirm execute/ })).toBeHidden();
			await expect(modal.getByRole('button', { name: /^Execute/ })).toBeVisible();
			await expect(modal.getByTestId('execute-armed-note')).toBeHidden();
		} finally {
			deletePaymentRun(runId);
			resetInvoiceStatus(target.id, sourceStatus);
		}
	});

	test('Cannot execute the same run twice — second call returns 409', async ({ page }) => {
		const target = await firstPayableQueueInvoice(page);
		const sourceStatus = target.status;
		const headers = await authedTenantHeaders(page);

		// Create the run via the API directly (UI path is covered above).
		const runId = await createRun(page, target.id, 'ach');

		try {
			// First execute: 200 (SoD disabled in beforeEach so admin can both
			// create and execute).
			const first = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(first.status()).toBe(200);

			// Second execute: 409 because run.status is no longer 'draft'.
			const second = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(second.status()).toBe(409);
		} finally {
			deletePaymentRun(runId);
			resetInvoiceStatus(target.id, sourceStatus);
		}
	});

	test('After execute, the queue no longer contains the paid invoice', async ({ page }) => {
		const target = await firstPayableQueueInvoice(page);
		const sourceStatus = target.status;
		const headers = await authedTenantHeaders(page);

		const runId = await createRun(page, target.id, 'wire');

		try {
			// Without this, a refused execute would leave the invoice in the
			// queue and fail the assertion below for the wrong reason.
			const executed = await page.request.post(
				`${API_BASE}/api/payments/runs/${runId}/execute`,
				{ headers }
			);
			expect(executed.status(), `execute: ${await executed.text()}`).toBe(200);

			const queueAfter = await getQueue(page);
			expect(queueAfter.find((q) => q.id === target.id)).toBeUndefined();
		} finally {
			deletePaymentRun(runId);
			resetInvoiceStatus(target.id, sourceStatus);
		}
	});
});
