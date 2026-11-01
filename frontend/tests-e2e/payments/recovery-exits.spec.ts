import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';
import type { ActionRefusal } from '#lib/api/actionVerdicts.ts';

/**
 * /payments — the two money-path RECOVERY exits.
 *
 * Both existed in the backend, are documented as *the* sanctioned recovery in
 * `backend/docs/payments.md`, and had no UI at all. In both cases the money has
 * already moved, so the only control that WAS reachable — void — is the wrong
 * action: it returns the invoice to `approved` and invites a second payment for
 * money that already left.
 *
 *  1. **`POST /api/payments/runs/{id}/sync-erp`** — re-runs the ERP sync-back
 *     for a run whose settled payments never landed, releasing invoices
 *     stranded at `payment_scheduled`. Moves no money; idempotent by
 *     construction. Its response carries `synced` / `transitioned` / `skipped`
 *     / `held` / `failed`, and **`transitioned` is the only one that answers
 *     "did this recover anything"** — `synced` counts legs that RAN and stays
 *     true on a repeat, so a UI leading with `synced` would claim a recovery on
 *     every retry of an already-clean run.
 *
 *  2. **`POST /api/payments/{id}/settlement/accept`** — declares a short /
 *     unverifiable settlement final and releases the invoice to `paid`. The
 *     hold itself was invisible too (`settled_amount` / `settled_currency` are
 *     on the read surface and nothing rendered them), and an exit to an
 *     invisible state is not an exit.
 *
 * Response payloads are driven through `page.route()` — a real response the
 * page parses, not a stub of the page's own state — because manufacturing a
 * genuinely under-settled payment (or a run with a failed ERP leg) in the
 * tenant DB would pin these assertions to whichever rows the shared e2e tenant
 * happens to hold. The permission gates are asserted against the REAL backend
 * at the bottom of the file, where a mock would prove nothing.
 */

const HELD_PAYMENT_ID = '11111111-1111-1111-1111-111111111111';
const HELD_INVOICE_ID = '22222222-2222-2222-2222-222222222222';
const RUN_ID = '33333333-3333-3333-3333-333333333333';

/**
 * A `completed` payment whose rail reported LESS than AP authorized.
 *
 * Denominated in **EUR on purpose**, not the tenant's USD default. This dialog
 * is the one screen whose whole job is to put "Authorized" beside "Settled",
 * and it exists because a rail can settle in a currency AP never authorized —
 * so a fixture in the org's own currency makes every rendering of it look
 * correct, including the wrong one. `PaymentResponse` carries no currency at
 * all before this change, so the authorized figure fell back to the org default
 * and rendered `$500.00` directly above the real `€450.00`.
 */
function heldPayment(acceptRefusal: ActionRefusal = null) {
	return {
		// The server's verdict on Accept for this reader
		// (`payment_controls.settlement_accept_refusal`): `null` offers it.
		accept_settlement_refusal: acceptRefusal,
		id: HELD_PAYMENT_ID,
		correlation_id: null,
		invoice_id: HELD_INVOICE_ID,
		payment_run_id: RUN_ID,
		amount: '500.00',
		method: 'ach',
		status: 'completed',
		reference: 'E2E-SETTLE-REF',
		created_at: '2026-06-01T00:00:00Z',
		updated_at: null,
		settled_amount: '450.00',
		settled_currency: 'EUR',
		// What `amount` is denominated in — the invoice's own currency.
		currency: 'EUR',
		vendor_name: 'E2E Settlement Vendor',
		invoice_number: 'E2E-SETTLE-001',
		card_last_four: null,
		card_provider: null,
		card_id: null
	};
}

function runRow(status = 'completed', syncRefusal: ActionRefusal = null) {
	return {
		// The server's verdict on Retry ERP sync for this reader
		// (`payment_controls.run_sync_erp_refusal`): `null` offers it.
		actions: { sync_erp: syncRefusal },
		id: RUN_ID,
		status,
		total_amount: 500,
		currency: 'EUR',
		initiated_by: null,
		executed_at: '2026-06-01T00:00:00Z',
		created_at: '2026-06-01T00:00:00Z',
		payment_count: 1
	};
}

/** Pin the History tab to one under-settled payment, carrying the server's
 *  verdict on accepting it (`null` = offered). */
async function mockHistory(
	page: import('@playwright/test').Page,
	acceptRefusal: ActionRefusal = null
) {
	await page.route(/\/api\/payments\?/, (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: [heldPayment(acceptRefusal)],
				total: 1,
				page: 1,
				page_size: 20
			})
		})
	);
	await page.route('**/api/payments/counts**', (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ total: 1, by_status: { completed: 1 } })
		})
	);
}

async function mockRuns(
	page: import('@playwright/test').Page,
	status = 'completed',
	syncRefusal: ActionRefusal = null
) {
	await page.route('**/api/payments/runs/**', async (route) => {
		// Only the LIST call; the sync-erp POST is routed separately per test.
		if (route.request().method() !== 'GET') return route.fallback();
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: [runRow(status, syncRefusal)],
				total: 1,
				page: 1,
				page_size: 100
			})
		});
	});
}

async function openHistory(page: import('@playwright/test').Page) {
	await page.goto('/payments');
	await page.getByRole('tab', { name: 'History', exact: true }).click();
}

async function openRuns(page: import('@playwright/test').Page) {
	await page.goto('/payments');
	await page.getByRole('tab', { name: 'Runs', exact: true }).click();
}

// Located by ACCESSIBLE NAME, which is each row action's `aria-label` — a
// per-row name so a screen reader hears which invoice / run the button acts on.
// WCAG 2.5.3 (Label in Name) is why the visible label is a prefix of it, and it
// is why matching on the visible text still works here.
const acceptAction = (page: import('@playwright/test').Page) =>
	page.getByRole('button', { name: 'Accept settlement' });

const syncAction = (page: import('@playwright/test').Page) =>
	page.getByRole('button', { name: 'Retry ERP sync' });

test.describe('/payments — settlement acceptance (the under-settlement hold exit)', () => {
	test('the settled figure renders beside the authorized one, and the exit is offered', async ({
		page
	}) => {
		await mockHistory(page);
		await openHistory(page);

		// The hold was invisible before this: both figures now render, and
		// neither is derived from the other (the backend owns any delta).
		await expect(page.getByTestId('payment-settled-amount')).toBeVisible();
		await expect(page.getByTestId('payment-settled-amount')).toContainText('450');
		await expect(acceptAction(page)).toBeVisible();
	});

	test('the confirm names the payment, states the consequence, and refuses an empty reason', async ({
		page
	}) => {
		await mockHistory(page);
		await openHistory(page);
		await acceptAction(page).click();

		// Confirm-then-act: the dialog names the specific payment and both
		// figures, and says plainly what accepting does.
		await expect(page.getByTestId('settlement-figures')).toContainText('500');
		await expect(page.getByTestId('settlement-settled-figure')).toContainText('450');
		const warning = page.getByTestId('settlement-warning');
		await expect(warning).toContainText('final');
		await expect(warning).toContainText('cannot be undone');

		// Reason is enforced in the FORM, not only server-side — and the form
		// SAYS so: Submit stays enabled, and a blank reason marks the field
		// (§ Field validation). A disabled Submit gave no reason at all.
		let acceptCalls = 0;
		await page.route(`**/api/payments/${HELD_PAYMENT_ID}/settlement/accept`, (r) => {
			acceptCalls += 1;
			return r.fulfill({ status: 500, body: '' });
		});
		const confirm = page.getByTestId('settlement-confirm');
		const reason = page.getByTestId('settlement-reason');
		await expect(confirm).toBeEnabled();
		await reason.fill('   ');
		await confirm.click();
		await expect(page.getByTestId('settlement-reason-error')).toHaveText(
			'Reason is required to accept a settlement as final'
		);
		await expect(reason).toHaveAttribute('aria-invalid', 'true');
		await expect(reason).toHaveAttribute('aria-describedby', 'settlement-reason-error');
		await expect(reason).toBeFocused();
		await expect(page.getByTestId('toast')).toHaveCount(0);
		expect(acceptCalls).toBe(0);

		await reason.fill('e2e: shortfall agreed with the vendor');
		await expect(page.getByTestId('settlement-reason-error')).toBeEmpty();
		await expect(reason).not.toHaveAttribute('aria-invalid', 'true');
	});

	test('an acceptance whose list refresh fails still reads as accepted', async ({ page }) => {
		// `refreshAfterCompliance` used to run inside the acceptance's `try`, so
		// a History reload that failed reached its `catch` and the dialog
		// reported the acceptance as refused — about an invoice already released
		// to paid. The same refresh follows a compliance release / dismiss.
		const pageErrors: string[] = [];
		page.on('pageerror', (e) => pageErrors.push(e.message));
		let accepted = false;
		// After `mockHistory`: the most recently registered route wins, so this
		// list handler overrides its always-200 one.
		await mockHistory(page);
		await page.route(/\/api\/payments\?/, (r) =>
			accepted
				? r.fulfill({
						status: 500,
						contentType: 'application/json',
						body: JSON.stringify({ detail: 'Internal Server Error' })
					})
				: r.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({ items: [heldPayment()], total: 1, page: 1, page_size: 20 })
					})
		);
		await page.route(`**/api/payments/${HELD_PAYMENT_ID}/settlement/accept`, (r) => {
			accepted = true;
			return r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ...heldPayment(), status: 'completed' })
			});
		});

		await openHistory(page);
		await acceptAction(page).click();
		await page.getByTestId('settlement-reason').fill('e2e: shortfall agreed with the vendor');
		await page.getByTestId('settlement-confirm').click();

		const toasts = page.getByTestId('toast');
		await expect(toasts.filter({ hasText: 'Settlement accepted' })).toBeVisible();
		await expect(toasts.filter({ hasText: 'could not be refreshed' })).toBeVisible();
		// Positive first: the refresh ran and failed. Only then is the absence
		// of a refusal meaningful.
		await expect(page.getByTestId('settlement-error')).toHaveCount(0);
		await expect(page.getByTestId('settlement-figures')).toHaveCount(0);
		expect(pageErrors).toEqual([]);
	});

	test('both figures render under the currency the SERVER stated, not the org default', async ({
		page
	}) => {
		// The regression this file could not previously see. Every assertion
		// above matches on DIGITS only, so "Authorized $500.00" and
		// "Authorized €500.00" are indistinguishable to them — and a fabricated
		// symbol above a real one is precisely the failure on a screen built to
		// catch a `currency_mismatch`.
		//
		// Before the fix `PaymentResponse` carried no `currency`, the authorized
		// half fell back to `orgCurrency` (USD on the e2e tenant), and this test
		// fails on the `$` exclusion below while the settled half already passed.
		await mockHistory(page);
		await openHistory(page);
		await acceptAction(page).click();

		const authorized = page.getByTestId('settlement-authorized-figure');
		const settled = page.getByTestId('settlement-settled-figure');

		await expect(authorized).toContainText('€');
		await expect(settled).toContainText('€');
		// The two figures this dialog exists to compare must not be denominated
		// differently by the UI — a dollar sign here is invented, not reported.
		await expect(authorized).not.toContainText('$');
		await expect(page.getByTestId('settlement-figures')).not.toContainText('$');
	});

	test('the History row states the authorized currency too', async ({ page }) => {
		// The same fabrication one screen earlier: the row's authorized amount
		// used to borrow the org default while the settled sub-line beneath it
		// rendered `settled_currency`.
		await mockHistory(page);
		await openHistory(page);

		const row = page.locator('table tbody tr', { hasText: 'E2E-SETTLE-001' });
		await expect(row).toContainText('€500.00');
		await expect(row).not.toContainText('$');
	});

	test('confirming posts the reason to the accept endpoint', async ({ page }) => {
		await mockHistory(page);
		let posted: { reason?: string } | null = null;
		await page.route(`**/api/payments/${HELD_PAYMENT_ID}/settlement/accept`, async (route) => {
			posted = route.request().postDataJSON() as { reason?: string };
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ...heldPayment(), status: 'completed' })
			});
		});

		await openHistory(page);
		await acceptAction(page).click();
		await page.getByTestId('settlement-reason').fill('e2e: shortfall agreed with the vendor');
		await page.getByTestId('settlement-confirm').click();

		await expect.poll(() => posted?.reason).toBe('e2e: shortfall agreed with the vendor');
	});

	test('a backend refusal stays on screen instead of fading in a toast', async ({ page }) => {
		await mockHistory(page);
		await page.route(`**/api/payments/${HELD_PAYMENT_ID}/settlement/accept`, (r) =>
			r.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify({
					detail:
						"This payment's settlement already covers the invoice; there is nothing to accept."
				})
			})
		);

		await openHistory(page);
		await acceptAction(page).click();
		await page.getByTestId('settlement-reason').fill('e2e: reason');
		await page.getByTestId('settlement-confirm').click();

		await expect(page.getByTestId('settlement-error')).toContainText('already covers');
	});

	for (const [code, what] of [
		['payment_settlement_not_held', 'an invoice no longer held'],
		['payment_settlement_covered', 'a settlement that already covers the invoice']
	]) {
		test(`the server refusing ${code} (${what}) offers no exit at all`, async ({ page }) => {
			// The row used to offer Accept wherever any figure was reported, and the
			// dialog then fetched the invoice to find out there was nothing to
			// accept. The server's verdict now decides before the click.
			await mockHistory(page, { code, message: 'server English' });
			await openHistory(page);

			// Positive first: the row (and its settled figure) rendered.
			await expect(page.getByTestId('payment-settled-amount')).toBeVisible();
			await expect(acceptAction(page)).toHaveCount(0);
		});
	}

	test('a role without payment.execute sees the figures but not the exit', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		// The verdict the server computes for a reader without the permission.
		await mockHistory(page, { code: 'role_not_permitted', message: 'server English' });
		await openHistory(page);

		// Reading what the rail settled is not a privilege; accepting it is.
		await expect(page.getByTestId('payment-settled-amount')).toBeVisible();
		await expect(acceptAction(page)).toHaveCount(0);
	});
});

test.describe('/payments — run-level ERP sync retry (the stranded-invoice exit)', () => {
	test('the result leads with `transitioned`, and reports every count the API returned', async ({
		page
	}) => {
		await mockRuns(page);
		await page.route(`**/api/payments/runs/${RUN_ID}/sync-erp`, (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					id: RUN_ID,
					synced: 3,
					transitioned: 2,
					skipped: 1,
					held: 1,
					failed: 0
				})
			})
		);

		await openRuns(page);
		await syncAction(page).click();

		// Confirm-then-act: the dialog names the run and says it moves no money.
		await expect(page.getByTestId('erp-sync-warning')).toContainText('no money');
		await page.getByTestId('erp-sync-confirm').click();

		// `transitioned`, not `synced`, is the success statement.
		await expect(page.getByTestId('erp-sync-outcome')).toContainText('2 invoices released');
		await expect(page.getByTestId('erp-sync-transitioned')).toHaveText('2');
		await expect(page.getByTestId('erp-sync-synced')).toHaveText('3');
		await expect(page.getByTestId('erp-sync-skipped')).toHaveText('1');
		await expect(page.getByTestId('erp-sync-held')).toHaveText('1');
		await expect(page.getByTestId('erp-sync-failed')).toHaveText('0');
	});

	test('a run with nothing to recover says so — a non-zero `synced` is not success', async ({
		page
	}) => {
		await mockRuns(page);
		await page.route(`**/api/payments/runs/${RUN_ID}/sync-erp`, (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					id: RUN_ID,
					// The repeat-call shape: the legs ran again, nothing moved.
					synced: 2,
					transitioned: 0,
					skipped: 2,
					held: 0,
					failed: 0
				})
			})
		);

		await openRuns(page);
		await syncAction(page).click();
		await page.getByTestId('erp-sync-confirm').click();

		await expect(page.getByTestId('erp-sync-outcome')).toContainText('Nothing to recover');
		await expect(page.getByTestId('erp-sync-transitioned')).toHaveText('0');
		await expect(page.getByTestId('erp-sync-synced')).toHaveText('2');
	});

	for (const status of ['draft', 'failed', 'executing']) {
		test(`a ${status} run with no settled payment is not offered the action at all`, async ({
			page
		}) => {
			// The server's verdict (`payment_run_nothing_settled`), not a status
			// list restated in the page: it used to offer this on a failed or
			// executing run, a button that could only 409.
			await mockRuns(page, status, {
				code: 'payment_run_nothing_settled',
				message: 'server English'
			});
			await openRuns(page);
			await expect(page.getByText(RUN_ID.slice(0, 8)).first()).toBeVisible();
			await expect(syncAction(page)).toHaveCount(0);
		});
	}

	test('a role without payment.execute does not get the action', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await mockRuns(page, 'completed', { code: 'role_not_permitted', message: 'server English' });
		await openRuns(page);

		// Wait for the mocked run row before asserting an absence.
		await expect(page.getByText(RUN_ID.slice(0, 8)).first()).toBeVisible();
		await expect(syncAction(page)).toHaveCount(0);
	});
});

test.describe('/payments — dismissing a compliance hold', () => {
	/** A payment the sanctions gate parked; this reader may dismiss it. */
	function heldForCompliance() {
		return {
			...heldPayment(),
			status: 'pending_compliance',
			settled_amount: null,
			settled_currency: null,
			release_refusal: { code: 'e2e_not_offered' },
			dismiss_refusal: null,
			accept_settlement_refusal: {
				code: 'payment_settlement_not_completed',
				message: 'server English'
			}
		};
	}

	async function mockComplianceHistory(page: import('@playwright/test').Page) {
		let dismissed = false;
		await page.route(/\/api\/payments\?/, (r) =>
			dismissed
				? r.fulfill({
						status: 500,
						contentType: 'application/json',
						body: JSON.stringify({ detail: 'Internal Server Error' })
					})
				: r.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({
							items: [heldForCompliance()],
							total: 1,
							page: 1,
							page_size: 20
						})
					})
		);
		await page.route('**/api/payments/counts**', (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ total: 1, by_status: { pending_compliance: 1 } })
			})
		);
		let dismissCalls = 0;
		await page.route(`**/api/payments/${HELD_PAYMENT_ID}/compliance/dismiss`, (r) => {
			dismissCalls += 1;
			dismissed = true;
			return r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ...heldForCompliance(), status: 'failed' })
			});
		});
		return { dismissCalls: () => dismissCalls };
	}

	test('a blank reason is a field error, and a dismiss whose refresh fails still reads as done', async ({
		page
	}) => {
		const pageErrors: string[] = [];
		page.on('pageerror', (e) => pageErrors.push(e.message));
		const calls = await mockComplianceHistory(page);
		await openHistory(page);
		await page.getByRole('button', { name: 'Dismiss', exact: true }).click();

		// Submit stays enabled; a blank reason marks the field and sends nothing.
		const reason = page.getByTestId('compliance-reason');
		await reason.fill('  ');
		await page.getByRole('button', { name: 'Dismiss payment' }).click();
		await expect(page.getByTestId('compliance-reason-error')).toHaveText(
			'Reason is required to dismiss a held payment'
		);
		await expect(reason).toHaveAttribute('aria-invalid', 'true');
		await expect(reason).toBeFocused();
		await expect(page.getByTestId('toast')).toHaveCount(0);
		expect(calls.dismissCalls()).toBe(0);

		// The dismiss lands; the History reload after it fails. That is a stale
		// list — never "Dismiss failed" about a payment already flipped.
		await reason.fill('e2e: vendor confirmed sanctioned');
		await page.getByRole('button', { name: 'Dismiss payment' }).click();
		const toasts = page.getByTestId('toast');
		await expect(toasts.filter({ hasText: 'Payment dismissed' })).toBeVisible();
		await expect(toasts.filter({ hasText: 'could not be refreshed' })).toBeVisible();
		await expect(toasts.filter({ hasText: 'Dismiss failed' })).toHaveCount(0);
		expect(calls.dismissCalls()).toBe(1);
		expect(pageErrors).toEqual([]);
	});
});

test.describe('/payments — recovery exits are gated server-side too', () => {
	// The UI gate is a courtesy; these assert the real backend refuses a role
	// without `payment.execute`, so hiding the control is never the only lock.
	test('clerk gets 403 on the run ERP-sync retry', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const resp = await page.request.post(
			`${API_BASE}/api/payments/runs/00000000-0000-0000-0000-000000000000/sync-erp`,
			{ headers: await authedTenantHeaders(page), data: {} }
		);
		expect(resp.status()).toBe(403);
	});

	test('clerk gets 403 on settlement accept', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const resp = await page.request.post(
			`${API_BASE}/api/payments/00000000-0000-0000-0000-000000000000/settlement/accept`,
			{ headers: await authedTenantHeaders(page), data: { reason: 'should-403' } }
		);
		expect(resp.status()).toBe(403);
	});
});
