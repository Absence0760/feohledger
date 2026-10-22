import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	loadMoreUntilRow,
	tenantPsql,
	test
} from '../fixtures/helpers';

async function createApprovedInvoice(
	page: import('@playwright/test').Page,
	invoiceNumber: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: 'E2E Pluralization Vendor',
			invoice_number: invoiceNumber,
			amount: 250.0,
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
	return body.id;
}

/** Wipe an approved test invoice + its workflow rows + audit trail.
 *  The PATCH/DELETE invoice endpoint won't touch approved rows, so
 *  raw SQL is the only revertible path. */
function hardDeleteInvoice(id: string): void {
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	// 'E2E Pluralization Vendor' may auto-mint `unverified` on first use —
	// refresh_warnings (now run at manual-entry creation time) raises an
	// `unverified_vendor` exception against it, which FKs to this invoice and
	// must clear before the delete below.
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	// audit_log is append-only (DB trigger, migration 0022 + seed) — never DELETE;
	// orphan rows for the removed invoice are harmless (no FK back to invoices).
	deleteInvoicesWhere(`id='${id}'`);
}

/**
 * /payments Queue tab — selection, Review & Pay panel, payment-method
 * selector, and Clear. Stops short of clicking "Create Draft Run" so
 * we don't pollute the seed payment_runs table; that path is exercised
 * by the runs detail tests via the API.
 */

/**
 * The first queue row whose checkbox is ENABLED. The queue lists blocked rows
 * (an unverified vendor, an open payment-blocking exception) with a disabled
 * checkbox, and with no due dates their order is the random invoice id — so
 * "the first row" was a blocked one whenever the tenant held any, and
 * `.check()` waited out the timeout on a control that can never be checked.
 */
function firstSelectableRow(page: import('@playwright/test').Page) {
	return page
		.locator('table tbody tr')
		.filter({ has: page.locator('input[type="checkbox"]:not([disabled])') })
		.first();
}

test.describe('/payments queue selection', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/payments');
		// Queue is the default tab, but be defensive in case a prior
		// test left a different tab active in shared state.
		await page.locator('.tab', { hasText: 'Queue' }).click();
	});

	test('queue table renders rows from /api/payments/queue', async ({ page }) => {
		// The seeded `approved` invoices should be ready to pay. If the
		// seed has zero queue items, the rest of the suite is moot, so
		// fail fast with a helpful message.
		const rows = page.locator('table tbody tr');
		await expect(rows.first()).toBeVisible({ timeout: 5_000 });
		expect(await rows.count()).toBeGreaterThan(0);
		// Header includes the checkbox column.
		await expect(page.locator('thead th.checkbox-col input[type="checkbox"]')).toBeVisible();
	});

	test('selecting a row reveals the pay-bar with count + total', async ({ page }) => {
		const firstRow = firstSelectableRow(page);
		await firstRow.locator('input[type="checkbox"]').check();

		const payBar = page.locator('.pay-bar');
		await expect(payBar).toBeVisible();
		await expect(payBar.locator('.pay-bar-count')).toContainText('1 selected');
		// Pay-bar shows a currency-formatted total (USD format includes "$").
		await expect(payBar.locator('.pay-bar-count')).toContainText('$');
	});

	test('selecting all via header checkbox selects every selectable queue row', async ({
		page
	}) => {
		const selectAll = page.locator('thead th.checkbox-col input[type="checkbox"]');

		// Readiness before counting, and a real signal rather than a sleep: while
		// `GET /api/payments/queue` is in flight the DataTable renders its single
		// `table-empty` row, so a bare `tbody tr` count reads 1. That is exactly
		// how this snapshotted `total = 1` on CI and then compared it against the
		// 7 rows that had landed by the time the header checkbox was clicked.
		// The queue commits in ONE assignment (`queue = data.items`), so "no
		// empty row" means the whole set is on screen — there is no partial fill
		// to race. A genuinely empty (or failed) queue leaves that row up and
		// fails here, which is the honest outcome: there is nothing to select.
		await expect(page.getByTestId('table-empty')).toHaveCount(0);
		// Select-all is `disabled` when nothing is selectable, so asserting it is
		// enabled states this test's precondition instead of assuming it.
		await expect(selectAll).toBeEnabled();

		// The denominator is the SELECTABLE rows, not every row. A row carrying a
		// payment-blocking exception renders a disabled checkbox and is left out
		// of the selection on purpose (`selectableQueue` in the page) — pulling it
		// in would build a draft run `POST /api/payments/runs` refuses with a 409.
		const selectable = page.locator('table tbody tr input[type="checkbox"]:not([disabled])');
		const blocked = page.locator('table tbody tr input[type="checkbox"][disabled]');
		const selectableCount = await selectable.count();
		expect(selectableCount).toBeGreaterThan(0);

		await selectAll.check();

		const payBar = page.locator('.pay-bar');
		await expect(payBar).toBeVisible();
		await expect(payBar.locator('.pay-bar-count')).toContainText(`${selectableCount} selected`);

		// Every selectable checkbox is now checked...
		for (let i = 0; i < selectableCount; i++) {
			await expect(selectable.nth(i)).toBeChecked();
		}
		// ...and every blocked one is still not, which is the half that matters:
		// select-all must never sweep a blocked invoice into a payment run.
		const blockedCount = await blocked.count();
		for (let i = 0; i < blockedCount; i++) {
			await expect(blocked.nth(i)).not.toBeChecked();
		}
	});

	test('Clear button drops selection and hides pay-bar', async ({ page }) => {
		const row = firstSelectableRow(page);
		const checkbox = row.locator('input[type="checkbox"]');
		await checkbox.check();
		const payBar = page.locator('.pay-bar');
		await expect(payBar).toBeVisible();

		await payBar.getByRole('button', { name: 'Clear' }).click();
		await expect(payBar).toBeHidden();
		await expect(checkbox).not.toBeChecked();
	});

	test('Review & Pay opens the panel; method selects default to ACH', async ({ page }) => {
		await firstSelectableRow(page).locator('input[type="checkbox"]').check();
		await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();

		const reviewPanel = page.locator('.review-panel');
		await expect(reviewPanel).toBeVisible();
		await expect(reviewPanel.locator('.review-title')).toContainText('Payment Review');

		// One row per selected invoice; the method <select> defaults to "ach".
		const methodSelect = reviewPanel.locator('select.method-select').first();
		await expect(methodSelect).toBeVisible();
		await expect(methodSelect).toHaveValue('ach');

		// All four options exist (ach, wire, check, virtual_card).
		const optionValues = await methodSelect.locator('option').evaluateAll(
			(opts) => (opts as HTMLOptionElement[]).map((o) => o.value)
		);
		expect(optionValues).toEqual(['ach', 'wire', 'check', 'virtual_card']);
	});

	test('changing the payment-method select sticks while panel is open', async ({ page }) => {
		await firstSelectableRow(page).locator('input[type="checkbox"]').check();
		await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();

		const select = page.locator('.review-panel select.method-select').first();
		await select.selectOption('wire');
		await expect(select).toHaveValue('wire');
		// Switching it again confirms the binding isn't one-way.
		await select.selectOption('check');
		await expect(select).toHaveValue('check');
	});

	test('pay-bar total is the numeric SUM of selected amounts, not a string concat', async ({
		page
	}) => {
		// Regression guard for the money-on-the-wire change: /api/payments/queue
		// now returns `amount` as a Decimal STRING. The pay-bar total reduces
		// those amounts, so a missing Number() coercion would concatenate them
		// ("$0250.00250.00") instead of summing. Two $250.00 invoices must total
		// exactly $500.00.
		const stamp = Date.now();
		const created: string[] = [];
		try {
			created.push(await createApprovedInvoice(page, `E2E-SUM-${stamp}-A`));
			created.push(await createApprovedInvoice(page, `E2E-SUM-${stamp}-B`));

			// Wait for the queue's own fetch before paging, so `loadMoreUntilRow`
			// starts from a settled list rather than one still filling. The
			// helper no longer mistakes `DataTable`'s placeholder row for a real
			// one, but the "Load more" footer arrives with the same response as
			// the rows, so this keeps the first paging decision off an empty DOM.
			const queueLoaded = page.waitForResponse((r) =>
				new URL(r.url()).pathname.endsWith('/api/payments/queue')
			);
			await page.reload();
			await queueLoaded;
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const rowA = page.locator('table tbody tr', { hasText: `E2E-SUM-${stamp}-A` });
			const rowB = page.locator('table tbody tr', { hasText: `E2E-SUM-${stamp}-B` });
			// Page to the rows rather than assume they landed on page 1 — the
			// queue orders by due date and pages at 20, and these carry none.
			await loadMoreUntilRow(page, rowA);
			await loadMoreUntilRow(page, rowB);
			await expect(rowA).toBeVisible();
			await expect(rowB).toBeVisible();

			await rowA.locator('input[type="checkbox"]').check();
			await rowB.locator('input[type="checkbox"]').check();

			const count = page.locator('.pay-bar .pay-bar-count');
			await expect(count).toContainText('2 selected');
			// The exact summed total — proves the reduce is arithmetic, not concat.
			await expect(count).toContainText('$500.00');
		} finally {
			for (const id of created) hardDeleteInvoice(id);
		}
	});

	test('Review panel button label reflects selection size pluralization', async ({
		page
	}) => {
		// Seed produces only one approved invoice that's still in the
		// queue (the other approved rows already have payment records).
		// Mint two throwaway approved invoices so we can assert "2
		// Invoices" pluralization, then hard-delete via psql.
		const stamp = Date.now();
		const created: string[] = [];
		try {
			created.push(await createApprovedInvoice(page, `E2E-PAY-${stamp}-A`));
			created.push(await createApprovedInvoice(page, `E2E-PAY-${stamp}-B`));

			// Wait for the queue's own fetch before paging, so `loadMoreUntilRow`
			// starts from a settled list rather than one still filling. The
			// helper no longer mistakes `DataTable`'s placeholder row for a real
			// one, but the "Load more" footer arrives with the same response as
			// the rows, so this keeps the first paging decision off an empty DOM.
			const queueLoaded = page.waitForResponse((r) =>
				new URL(r.url()).pathname.endsWith('/api/payments/queue')
			);
			await page.reload();
			await queueLoaded;
			await page.locator('.tab', { hasText: 'Queue' }).click();

			const rowA = page.locator('table tbody tr', { hasText: `E2E-PAY-${stamp}-A` });
			const rowB = page.locator('table tbody tr', { hasText: `E2E-PAY-${stamp}-B` });
			await loadMoreUntilRow(page, rowA);
			await loadMoreUntilRow(page, rowB);
			await expect(rowA).toBeVisible();
			await expect(rowB).toBeVisible();

			await rowA.locator('input[type="checkbox"]').check();
			await rowB.locator('input[type="checkbox"]').check();
			await page.locator('.pay-bar').getByRole('button', { name: 'Review & Pay' }).click();

			const executeBtn = page.locator('.review-panel .btn-execute');
			await expect(executeBtn).toContainText('2 Invoices');
		} finally {
			for (const id of created) hardDeleteInvoice(id);
		}
	});
});
