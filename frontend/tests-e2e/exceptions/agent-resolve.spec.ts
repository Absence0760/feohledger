import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /exceptions → "AI Agents" tab — the RUN action.
 *
 * `POST /api/exceptions/{id}/agent-resolve` had no caller in `frontend/src`:
 * `AgentDashboard.svelte` read `agent-decisions` and `agent-stats` and never
 * the run action, so the dashboard reported on agent activity that could only
 * be triggered outside the product.
 *
 * The property this file exists to pin is the OUTCOME rendering. Per
 * `backend/docs/exception-agents.md` the coordinator applies a resolver's fix
 * only when its confidence clears the org's autonomy threshold, and **escalates
 * to a human otherwise**. An escalation is therefore a normal, recorded outcome
 * of a successful run — rendering it as a failure would teach operators that
 * the safe path is the broken one. Same for `no_action`.
 *
 * Payloads go through `page.route()` — a real response the page parses, not a
 * stub of the page's own state — because which resolver fires, and at what
 * confidence, depends on the invoice/PO rows the shared e2e tenant happens to
 * hold. The role gate is asserted against the REAL backend at the bottom.
 *
 * The mocks send the WIRE status (`resolved` / `escalated`) and the assertions
 * read the rendered LABEL (`Resolved` / `Escalated`) — the outcome line reads
 * `types/exception.ts::EXCEPTION_STATUS_LABEL_KEYS`, the same keys the queue's
 * status chips read, so asserting the raw value here would be asserting the
 * defect. `toContainText` is case-sensitive, which is what makes the pair a
 * real check rather than a coincidence.
 */

const EXC_ID = '55555555-5555-5555-5555-555555555555';
const NO_INVOICE_EXC_ID = '66666666-6666-6666-6666-666666666666';

function candidate(over: Record<string, unknown> = {}) {
	return {
		id: EXC_ID,
		invoice_id: '77777777-7777-7777-7777-777777777777',
		invoice_number: 'E2E-AGENT-001',
		vendor_name: 'E2E Agent Vendor',
		exception_type: 'po_mismatch',
		type_label: 'PO Mismatch',
		severity: 'warning',
		status: 'open',
		created_at: '2026-06-01T00:00:00Z',
		...over
	};
}

function decision(action: 'auto_resolved' | 'escalated' | 'no_action') {
	return {
		id: '88888888-8888-8888-8888-888888888888',
		exception_id: EXC_ID,
		invoice_id: '77777777-7777-7777-7777-777777777777',
		exception_type: 'po_mismatch',
		action_taken: action,
		confidence: action === 'auto_resolved' ? 0.94 : 0.41,
		rationale:
			action === 'escalated'
				? 'Amount variance is above the tolerance this resolver will act on alone.'
				: 'Invoice total matches the PO within tolerance.',
		changes: action === 'auto_resolved' ? { amount: { old: '101.00', new: '100.00' } } : null,
		autonomy_level: 'supervised',
		agent_type: 'amount_mismatch_v1',
		created_at: '2026-06-02T00:00:00Z'
	};
}

/** Pin the runnable queue the panel lists. */
async function mockCandidates(
	page: import('@playwright/test').Page,
	items: Record<string, unknown>[] = [candidate()]
) {
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items, total: items.length, page: 1, page_size: 25 })
			})
	);
}

async function mockRun(
	page: import('@playwright/test').Page,
	body: unknown,
	status = 200
): Promise<void> {
	await page.route(`**/api/exceptions/${EXC_ID}/agent-resolve`, (route) =>
		route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
	);
}

async function openAgentsTab(page: import('@playwright/test').Page) {
	await page.goto('/exceptions');
	await page.getByRole('tab', { name: 'AI Agents' }).click();
	await expect(page.getByTestId('agent-dashboard')).toBeVisible({ timeout: 5_000 });
}

const runAction = (page: import('@playwright/test').Page) =>
	page.getByRole('button', { name: /^Run agent on PO Mismatch exception/ });

test.describe('/exceptions AI Agents — running an agent', () => {
	test('the runnable queue is listed with a per-row run action', async ({ page }) => {
		await mockCandidates(page);
		await openAgentsTab(page);

		const panel = page.getByTestId('agent-run-panel');
		await expect(panel).toBeVisible();
		// What a run DOES is stated before the button, not after it.
		await expect(panel).toContainText(/autonomy threshold/i);
		await expect(panel).toContainText('E2E-AGENT-001');
		await expect(runAction(page)).toBeVisible();
	});

	test('running is confirm-then-act, and an auto-resolution renders its change', async ({
		page
	}) => {
		await mockCandidates(page);
		await mockRun(page, {
			exception: { id: EXC_ID, status: 'resolved' },
			decision: decision('auto_resolved')
		});
		await openAgentsTab(page);
		await runAction(page).click();

		// A run can MUTATE the invoice, so it is never a bare click.
		await expect(page.getByTestId('agent-run-warning')).toContainText(/may change this invoice/i);
		await page.getByTestId('agent-run-confirm').click();

		await expect(page.getByTestId('agent-run-action')).toContainText('Auto-resolved');
		await expect(page.getByTestId('agent-run-status')).toContainText('Resolved');
		await expect(page.getByTestId('agent-run-changes')).toContainText('101.00');
		await expect(page.getByTestId('agent-run-facts')).toContainText('amount_mismatch_v1');
	});

	test('an ESCALATION renders as an outcome, not as a failure', async ({ page }) => {
		await mockCandidates(page);
		await mockRun(page, {
			exception: { id: EXC_ID, status: 'escalated' },
			decision: decision('escalated')
		});
		await openAgentsTab(page);
		await runAction(page).click();
		await page.getByTestId('agent-run-confirm').click();

		// The run SUCCEEDED. The agent's confidence simply didn't clear the org's
		// autonomy threshold, which is what that threshold is for.
		const outcome = page.getByTestId('agent-run-outcome');
		await expect(outcome).toBeVisible();
		await expect(page.getByTestId('agent-run-action')).toContainText('Escalated');
		await expect(page.getByTestId('agent-run-status')).toContainText('Escalated');
		await expect(page.getByTestId('agent-run-escalated-note')).toContainText(
			/autonomy threshold/i
		);
		await expect(page.getByTestId('agent-run-rationale')).toContainText(/above the tolerance/i);

		// The error region is what a FAILURE renders. It must not be on screen.
		await expect(page.getByTestId('agent-run-error')).toHaveCount(0);
	});

	test('a no-action decision is also an outcome, with nothing changed', async ({ page }) => {
		await mockCandidates(page);
		await mockRun(page, {
			exception: { id: EXC_ID, status: 'open' },
			decision: decision('no_action')
		});
		await openAgentsTab(page);
		await runAction(page).click();
		await page.getByTestId('agent-run-confirm').click();

		await expect(page.getByTestId('agent-run-action')).toContainText('No action');
		await expect(page.getByTestId('agent-run-no-action-note')).toContainText(
			/nothing it could safely change/i
		);
		await expect(page.getByTestId('agent-run-error')).toHaveCount(0);
	});

	test('a real refusal (409) stays on screen instead of fading in a toast', async ({ page }) => {
		await mockCandidates(page);
		await mockRun(page, { detail: "Cannot run agent from 'resolved' status" }, 409);
		await openAgentsTab(page);
		await runAction(page).click();
		await page.getByTestId('agent-run-confirm').click();

		// Lost a race with a concurrent run, or the exception moved on. The
		// server's own sentence is the actionable half.
		await expect(page.getByTestId('agent-run-error')).toContainText(/Cannot run agent from/);
		await expect(page.getByTestId('agent-run-outcome')).toHaveCount(0);
	});

	test('an invoice-less exception is listed with a disabled run, not hidden', async ({ page }) => {
		await mockCandidates(page, [
			candidate({
				id: NO_INVOICE_EXC_ID,
				invoice_id: null,
				invoice_number: null,
				exception_type: 'fraud_flag',
				type_label: 'Fraud Flag'
			})
		]);
		await openAgentsTab(page);

		// The backend 422s it (a Positive Pay never-issued cheque has no invoice
		// for an agent to act on). A missing button explains nothing, so the row
		// keeps a disabled one carrying the reason.
		const disabled = page.getByRole('button', {
			name: /Cannot run an agent on this Fraud Flag exception/
		});
		await expect(disabled).toBeVisible();
		await expect(disabled).toBeDisabled();
	});
});

test.describe('/exceptions AI Agents — the real gate', () => {
	test('a clerk cannot reach the AI Agents surface at all', async ({ page, tenantClerk }) => {
		// `agent-stats` / `agent-decisions` / `agent-resolve` are all
		// `require_roles(admin, ap_manager)` — a clerk 403s on the whole queue
		// page, so the run action is unreachable rather than merely hidden.
		await signInAndWait(page, tenantClerk);
		await page.goto('/exceptions');
		const resp = await page.request.post(
			`${API_BASE}/api/exceptions/${EXC_ID}/agent-resolve`,
			{ headers: await authedTenantHeaders(page), data: {} }
		);
		expect(resp.status()).toBe(403);
	});
});
