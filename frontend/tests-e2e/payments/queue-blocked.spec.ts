import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	loadMoreUntilRow,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * /payments Queue tab — rows the backend would hard-refuse.
 *
 * `docs/followups.md` item 12: `GET /api/payments/queue` selects on payable
 * status and "not already paid", and says nothing about exceptions — but
 * `services/payment_runs.create_payment_run_for_invoices` refuses the WHOLE
 * run with a 409 when ANY selected invoice carries an unresolved
 * `duplicate` / `fraud_flag` / `line_total_mismatch` exception
 * (`PAYMENT_BLOCKING_EXCEPTION_TYPES`). Select twenty rows where one is
 * blocked and the draft failed with no advance signal and no indication which
 * row was at fault.
 *
 * Two halves are covered here:
 *
 *  1. **The advance signal.** `GET /api/payments/queue` carries a per-row
 *     `blocked` / `blocked_reason` pair. Most of these tests still inject it
 *     via `page.route()` — a real response the page parses, not a stub of the
 *     page's own state — because that is the only way to drive a specific
 *     `blocked_reason` (including one this build doesn't recognise) without
 *     manufacturing every kind of exception in the tenant DB — and because
 *     which rows a TENANT has blocked is not this file's to control. The
 *     live-payload behaviour (select-all skipping blocked rows) is asserted in
 *     `queue.spec.ts` instead, where it needs no fixed row set.
 *  2. **The failure message.** The 409 detail NAMES the offending invoice
 *     numbers and the type that blocked each one, and the page must keep that
 *     on screen rather than only flashing it through a 5-second toast. Note
 *     this is now only reachable via the STALE-VIEW race — see that test —
 *     precisely because half (1) ships: a row known to be blocked when the
 *     queue loaded can no longer be selected at all.
 */

/** Mint an `approved` invoice that lands in the payment queue. */
async function createApprovedInvoice(
	page: import('@playwright/test').Page,
	invoiceNumber: string,
	amount = 250.0
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Blocked Queue Vendor',
			invoice_number: invoiceNumber,
			amount,
			currency: 'USD'
		}
	});
	expect(resp.status()).toBe(201);
	const body = (await resp.json()) as { id: string };
	// POST /api/invoices ignores a client-supplied status by design, and the
	// compliance gate needs a real vendor_id — same setup queue.spec.ts uses.
	const vendorId = tenantPsql(`SELECT id FROM vendors WHERE status='active' LIMIT 1`).trim();
	const sets = `status='approved'${vendorId ? `, vendor_id='${vendorId}'` : ''}`;
	tenantPsql(`UPDATE invoices SET ${sets} WHERE id='${body.id}'`);
	return body.id;
}

/** Attach an unresolved payment-blocking exception to an invoice. */
function seedBlockingException(invoiceId: string, type = 'duplicate'): void {
	tenantPsql(
		`INSERT INTO exceptions (id, invoice_id, organization_id, exception_type, severity, description, status)
		 SELECT gen_random_uuid(), i.id, i.organization_id, '${type}', 'error', 'e2e payment-blocking exception', 'open'
		 FROM invoices i WHERE i.id='${invoiceId}'`
	);
}

/** Wipe an approved test invoice + everything that FKs to it. */
function hardDeleteInvoice(id: string): void {
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	// audit_log is append-only (DB trigger) — never DELETE; orphan rows are harmless.
	deleteInvoicesWhere(`id='${id}'`);
}

/**
 * Rewrite `GET /api/payments/queue` so the row for `invoiceNumber` carries the
 * proposed `blocked` / `blocked_reason` pair. Everything else passes through
 * untouched, so the page still parses a real backend response.
 */
async function injectBlockedFlag(
	page: import('@playwright/test').Page,
	invoiceNumber: string,
	reason: string
): Promise<void> {
	await page.route('**/api/payments/queue**', async (route) => {
		// Only the GET carries the payload. A CORS preflight (OPTIONS) has no
		// JSON body — forward it untouched rather than trying to parse it.
		if (route.request().method() !== 'GET') {
			await route.continue();
			return;
		}
		const response = await route.fetch();
		const body = (await response.json()) as {
			items?: { invoice_number: string; blocked?: boolean; blocked_reason?: string }[];
			blocked_total?: number;
			selectable_total?: number;
		};
		let newlyBlocked = 0;
		for (const item of body.items ?? []) {
			if (item.invoice_number === invoiceNumber && !item.blocked) {
				item.blocked = true;
				item.blocked_reason = reason;
				newlyBlocked++;
			}
		}
		// The queue payload now carries whole-set `blocked_total` /
		// `selectable_total` (the count banner + select-all rollup read them, not
		// the loaded rows) — keep them consistent with the rows we just flipped.
		if (typeof body.blocked_total === 'number') body.blocked_total += newlyBlocked;
		if (typeof body.selectable_total === 'number')
			body.selectable_total = Math.max(0, body.selectable_total - newlyBlocked);
		await route.fulfill({ response, json: body });
	});
}

/**
 * Rewrite `GET /api/payments/queue` so NO row carries the `blocked` pair at all
 * — the payload a backend predating the field returns.
 *
 * The queue's blocked rows are a property of the TENANT, not of the test: any
 * invoice that has picked up a duplicate / fraud / line-total / reconciliation
 * exception puts a chip and a banner on screen. Asserting "no chip anywhere"
 * against the live payload therefore only holds on a freshly-seeded tenant, and
 * fails the moment the worker lands on one that has a blocked row (it does).
 * Stripping the field makes the assertion mean what its name says, on any
 * tenant — and it is the graceful-degradation contract in its own right: the
 * page must not REQUIRE the field to work.
 */
async function stripBlockedFields(page: import('@playwright/test').Page): Promise<void> {
	await page.route('**/api/payments/queue**', async (route) => {
		if (route.request().method() !== 'GET') {
			await route.continue();
			return;
		}
		const response = await route.fetch();
		const body = (await response.json()) as {
			items?: { blocked?: boolean; blocked_reason?: string }[];
			blocked_total?: number;
			selectable_total?: number;
		};
		for (const item of body.items ?? []) {
			delete item.blocked;
			delete item.blocked_reason;
		}
		// The ENVELOPE carries the feature too — `blocked_total` is what drives
		// the banner (`selectable_total` is derived from it in the same handler,
		// `api/payments.py`: `blocked_total = rollup.total - selectable_total`).
		// Stripping only the per-row pair left the banner rendering from a count
		// no visible row explained, so this helper's own promise — "assert no
		// chip anywhere, on ANY tenant" — held only on a freshly-seeded one and
		// failed on a worker whose tenant had picked up a blocking exception.
		// The sibling `markSomeBlocked` above keeps both in step for exactly
		// this reason; this one has to remove both for the same reason.
		delete body.blocked_total;
		delete body.selectable_total;
		await route.fulfill({ response, json: body });
	});
}

test.describe('/payments queue — payment-blocking exceptions', () => {
	test('with no `blocked` field the queue behaves exactly as before', async ({ page }) => {
		// Graceful degradation: against a backend that sends no `blocked` key
		// (`stripBlockedFields` produces exactly that payload), every row must
		// stay selectable with no chip, no banner, no noise. The page must not
		// REQUIRE the field.
		//
		// "No noise" is asserted as: no uncaught page error, and no Svelte
		// effect-loop error — the selection-pruning `$effect` now depends on a
		// `$derived` list AND writes the selection, which is exactly the shape
		// that loops if the identity-preserving guard is ever lost. Scoped to
		// those classes rather than "any console.error" so unrelated dev-server
		// chatter can't make this flaky (same posture as
		// `tests-e2e/reactivity/no-effect-loop.spec.ts`).
		const pageErrors: string[] = [];
		const LOOP_RE = /effect_update_depth_exceeded|Maximum update depth/i;
		page.on('pageerror', (err) => pageErrors.push(err.message));
		page.on('console', (msg) => {
			if (msg.type() === 'error' && LOOP_RE.test(msg.text())) pageErrors.push(msg.text());
		});

		await stripBlockedFields(page);
		await page.goto('/payments');
		await page.locator('.tab', { hasText: 'Queue' }).click();

		// Readiness before counting. `rows.first()` is NOT it: the DataTable's
		// loading placeholder is itself a `tbody tr`, so "the first row is
		// visible" is satisfied while the fetch is still out and the count below
		// reads 1 — the same race that made queue.spec.ts assert "1 selected"
		// against 7 real rows on CI. No empty row means the whole set is on
		// screen; the queue commits in one assignment, so there is no partial fill.
		await expect(page.getByTestId('table-empty')).toHaveCount(0);

		await expect(page.getByTestId('queue-blocked-banner')).toHaveCount(0);
		await expect(page.getByTestId('queue-blocked-chip')).toHaveCount(0);
		await expect(page.getByTestId('queue-blocked-checkbox')).toHaveCount(0);

		// Nothing is blocked here (asserted just above), so every row is
		// selectable and select-all must take all of them.
		const selectable = page.locator('table tbody tr input[type="checkbox"]:not([disabled])');
		const total = await selectable.count();
		expect(total).toBeGreaterThan(0);
		const selectAll = page.locator('thead th.checkbox-col input[type="checkbox"]');
		await expect(selectAll).toBeEnabled();
		await selectAll.check();
		await expect(page.locator('.pay-bar .pay-bar-count')).toContainText(`${total} selected`);

		expect(pageErrors, `page errors on /payments:\n${pageErrors.join('\n')}`).toEqual([]);
		await page.unroute('**/api/payments/queue**');
	});

	test('a blocked row is labelled, unselectable, and excluded from select-all', async ({
		page
	}) => {
		const stamp = Date.now();
		const blockedNumber = `E2E-BLOCK-${stamp}-A`;
		const payableNumber = `E2E-BLOCK-${stamp}-B`;
		const created: string[] = [];
		try {
			created.push(await createApprovedInvoice(page, blockedNumber));
			created.push(await createApprovedInvoice(page, payableNumber));

			await injectBlockedFlag(page, blockedNumber, 'fraud_flag');
			await page.goto('/payments');
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const blockedRow = page.locator('table tbody tr', { hasText: blockedNumber });
			const payableRow = page.locator('table tbody tr', { hasText: payableNumber });
			// Page to them rather than assume page 1 — select-all below only ever
			// covers LOADED rows, so the counts stay self-consistent either way.
			await loadMoreUntilRow(page, blockedRow);
			await loadMoreUntilRow(page, payableRow);
			await expect(blockedRow).toBeVisible();
			await expect(payableRow).toBeVisible();

			// The reason is visible in the row AND is the checkbox's accessible
			// name, so it reaches a screen reader too.
			await expect(blockedRow.getByTestId('queue-blocked-chip')).toContainText('Fraud flag');
			const blockedBox = blockedRow.locator('input[type="checkbox"]');
			await expect(blockedBox).toBeDisabled();
			await expect(blockedBox).toHaveAttribute(
				'aria-label',
				new RegExp(`${blockedNumber}.*Fraud flag`)
			);

			// A count banner gives the advance signal before anything is selected.
			await expect(page.getByTestId('queue-blocked-banner')).toContainText('blocked from payment');

			// Select-all must skip it — and the pay-bar total must not include it.
			await page.locator('thead th.checkbox-col input[type="checkbox"]').check();
			await expect(blockedBox).not.toBeChecked();
			await expect(payableRow.locator('input[type="checkbox"]')).toBeChecked();

			const selectableCount = await page
				.locator('table tbody tr input[type="checkbox"]:not([disabled])')
				.count();
			await expect(page.locator('.pay-bar .pay-bar-count')).toContainText(
				`${selectableCount} selected`
			);
		} finally {
			await page.unroute('**/api/payments/queue**');
			for (const id of created) hardDeleteInvoice(id);
		}
	});

	test('an unknown blocked_reason code still blocks, with a generic reason', async ({
		page
	}) => {
		// Fail-safe: a vocabulary the frontend doesn't recognise must never
		// render a raw identifier, and must never make the row selectable.
		const stamp = Date.now();
		const number = `E2E-BLOCK-${stamp}-U`;
		let id: string | null = null;
		try {
			id = await createApprovedInvoice(page, number);
			await injectBlockedFlag(page, number, 'some_future_exception_type');
			await page.goto('/payments');
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const row = page.locator('table tbody tr', { hasText: number });
			await loadMoreUntilRow(page, row);
			await expect(row).toBeVisible();
			await expect(row.getByTestId('queue-blocked-chip')).toContainText(
				'Unresolved exception blocks payment'
			);
			await expect(row.getByTestId('queue-blocked-chip')).not.toContainText(
				'some_future_exception_type'
			);
			await expect(row.locator('input[type="checkbox"]')).toBeDisabled();
		} finally {
			await page.unroute('**/api/payments/queue**');
			if (id) hardDeleteInvoice(id);
		}
	});

	test('a payment_reconciliation block names its own reason, not the generic one', async ({
		page
	}) => {
		// The fourth member of `PAYMENT_BLOCKING_EXCEPTION_TYPES`. It was added to
		// the backend tuple without a case in the page's reason map, so a row held
		// because an earlier payment's fate at the rail is unknown rendered the
		// catch-all "Unresolved exception blocks payment" — true, but it tells the
		// operator nothing they can act on, and the action here (reconcile the
		// rail) is nothing like the action for a duplicate.
		const stamp = Date.now();
		const number = `E2E-BLOCK-${stamp}-R`;
		let id: string | null = null;
		try {
			id = await createApprovedInvoice(page, number);
			await injectBlockedFlag(page, number, 'payment_reconciliation');
			await page.goto('/payments');
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const row = page.locator('table tbody tr', { hasText: number });
			await loadMoreUntilRow(page, row);
			await expect(row).toBeVisible();
			const chip = row.getByTestId('queue-blocked-chip');
			await expect(chip).toContainText('Earlier payment unreconciled');
			await expect(chip).not.toContainText('Unresolved exception blocks payment');
			await expect(row.locator('input[type="checkbox"]')).toBeDisabled();
		} finally {
			await page.unroute('**/api/payments/queue**');
			if (id) hardDeleteInvoice(id);
		}
	});

	test('the create-draft 409 names the offending invoice and stays on screen', async ({
		page
	}) => {
		// No route interception here — this is the REAL backend refusal.
		//
		// The exception is seeded AFTER the row has been selected, and that
		// ordering is the whole point. `GET /api/payments/queue` now carries the
		// `blocked` pair, so an invoice blocked before the queue loads renders
		// with a DISABLED checkbox and can never be selected through the UI —
		// this test used to seed the exception up front and then sit for 30s
		// waiting to click a control the app is right to have disabled. Forcing
		// that click would have been testing the guard by defeating it.
		//
		// What is left, and what the panel error actually exists for, is the
		// STALE-VIEW race: the `blocked` flag is a snapshot taken when the queue
		// was fetched, so an exception raised afterwards (a concurrent
		// `refresh_warnings` recompute, another operator, the reconciler) leaves
		// a selection the client still believes is payable. The server refusal is
		// the real control; the advance signal is only an advance signal. Seeding
		// mid-flight reproduces exactly that, with no interception.
		const stamp = Date.now();
		const number = `E2E-409-${stamp}`;
		let id: string | null = null;
		try {
			id = await createApprovedInvoice(page, number);

			await page.goto('/payments');
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const row = page.locator('table tbody tr', { hasText: number });
			await loadMoreUntilRow(page, row);
			await expect(row).toBeVisible();
			const box = row.locator('input[type="checkbox"]');
			// It must be selectable right now — that IS the stale view.
			await expect(box).toBeEnabled();
			await box.check();
			await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();

			// The client's view of this row goes stale here. Nothing on the page
			// reloads the queue between opening the review panel and executing, so
			// the selection survives — which is the condition under test.
			seedBlockingException(id, 'duplicate');

			const refused = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/payments/runs') &&
					r.request().method() === 'POST' &&
					r.status() === 409
			);
			await page.locator('.review-panel .btn-execute').click();
			await refused;

			// The panel-level error carries the server's own detail, which names
			// the invoice number — the only thing that makes a 20-row selection
			// actionable. It is stated here, beside the refused button, and not
			// repeated in a toast (one error, one signal).
			const panelError = page.getByTestId('create-run-error');
			await expect(panelError).toBeVisible();
			await expect(panelError).toContainText(number);
			await expect(panelError).toContainText('exception');
			await expect(page.getByTestId('toast').filter({ hasText: number })).toHaveCount(0);

			// Changing the selection clears it — a stale refusal must not linger.
			await page.locator('.pay-bar').getByRole('button', { name: 'Clear' }).click();
			await row.locator('input[type="checkbox"]').check();
			await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();
			await expect(page.getByTestId('create-run-error')).toHaveCount(0);
		} finally {
			if (id) hardDeleteInvoice(id);
		}
	});
});
