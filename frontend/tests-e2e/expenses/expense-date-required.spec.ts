import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * Expense date is required — and its absence is no longer a mystery error.
 *
 * Regression, both halves:
 *
 *  - **Create.** The date input had no `required`, the save guard checked only
 *    merchant + amount, and the payload sent `expense_date: expense_date || null`.
 *    `ExpenseCreate.expense_date` is a bare `date`, so a blank field 422'd — and
 *    a Pydantic 422's `detail` is a LIST, which the shared api client rendered
 *    as literally "[object Object]". Two bugs stacked: an avoidable failure, and
 *    an unreadable message when it happened.
 *  - **Edit.** `ExpenseUpdate.expense_date` was `date | None` while the column is
 *    NOT NULL, so a PATCH carrying an explicit null reached `setattr` and raised
 *    `NotNullViolationError` → a bare 500.
 *
 * The UI now defaults create-mode to today, marks the field `required`, and
 * guards the save; the schema refuses an explicit null with a 422. The 500 half
 * has its own backend test (`backend/tests/test_expense_schema_date.py`); this
 * spec covers the surfaces a user touches, plus the API contract for the null
 * PATCH.
 */

/** Today as `YYYY-MM-DD` in the BROWSER's timezone, which is the one the
 *  modal computes its default in. `playwright.config.ts` pins the browser to
 *  `timezoneId: 'UTC'`; computing this in the Node runner used the machine's
 *  zone instead, so the spec failed every evening west of UTC locally while CI
 *  (UTC throughout) stayed green. */
function todayLocalIso(page: import('@playwright/test').Page): Promise<string> {
	return page.evaluate(() => {
		const now = new Date();
		const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
		return local.toISOString().slice(0, 10);
	});
}

test.describe('/expenses expense-date handling', () => {
	test('the New Expense modal opens with today prefilled and the date marked required', async ({
		page
	}) => {
		await page.goto('/expenses');

		await page.getByRole('button', { name: '+ New Expense' }).click();
		const dialog = page.getByRole('dialog', { name: 'New expense' });
		await expect(dialog).toBeVisible();

		const dateInput = dialog.locator('input[type="date"]');
		// Prefilled, so the common path can never post a null.
		await expect(dateInput).toHaveValue(await todayLocalIso(page));
		// …and marked required, so clearing it is caught by native validation
		// before any request leaves the browser.
		await expect(dateInput).toHaveAttribute('required', '');
	});

	test('clearing the date blocks the save instead of firing an "[object Object]" toast', async ({
		page
	}) => {
		await page.goto('/expenses');

		await page.getByRole('button', { name: '+ New Expense' }).click();
		const dialog = page.getByRole('dialog', { name: 'New expense' });

		await dialog.locator('input[type="date"]').fill('');
		await dialog.getByLabel(/Merchant/).fill('E2E Date Guard');
		await dialog.locator('input[type="number"]').first().fill('12.34');

		// Fail the request loudly if one is somehow sent — the whole point is that
		// nothing reaches the API in this state.
		let posted = false;
		await page.route(
			(url) => url.pathname === '/api/expenses',
			(route) => {
				if (route.request().method() === 'POST') posted = true;
				return route.continue();
			}
		);

		await dialog.getByRole('button', { name: 'Create', exact: true }).click();

		// The dialog stays open (nothing was created) and no toast claims success
		// or renders the old "[object Object]".
		await expect(dialog).toBeVisible();
		await expect(page.locator('.toast')).toHaveCount(0);
		expect(posted, 'no POST should be attempted with a blank date').toBe(false);
	});

	test('PATCHing an explicit null date is a 422, never a 500', async ({ page }) => {
		await page.goto('/expenses');
		const headers = await authedTenantHeaders(page);

		let expenseId: string | null = null;
		try {
			const created = await page.request.post(`${API_BASE}/api/expenses`, {
				headers,
				data: {
					merchant: `E2E Null Date ${Date.now()}`,
					amount: '10.00',
					currency: 'USD',
					expense_date: '2026-03-01',
					category: 'travel'
				}
			});
			expect(created.status()).toBe(201);
			expenseId = ((await created.json()) as { id: string }).id;

			const patched = await page.request.patch(`${API_BASE}/api/expenses/${expenseId}`, {
				headers,
				data: { expense_date: null }
			});
			// A required, NOT NULL column refusing to be cleared is a client error.
			expect(patched.status()).toBe(422);

			// Omitting the key entirely still leaves the stored date untouched.
			const untouched = await page.request.patch(`${API_BASE}/api/expenses/${expenseId}`, {
				headers,
				data: { merchant: 'E2E Null Date (renamed)' }
			});
			expect(untouched.status()).toBe(200);
			expect(((await untouched.json()) as { expense_date: string }).expense_date).toBe(
				'2026-03-01'
			);
		} finally {
			if (expenseId) tenantPsql(`DELETE FROM expenses WHERE id='${expenseId}'`);
		}
	});
});
