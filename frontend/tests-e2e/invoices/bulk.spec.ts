import { deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /invoices — bulk operations bar.
 *
 * Selecting any row reveals the bulk-bar with "X selected", a Clear
 * button, a Change Status dropdown, a Delete button, and CSV/JSON/XML
 * export buttons. System-managed statuses can't be selected (the row
 * checkbox is disabled), so this spec selects rows whose status is
 * known to be selectable (new / ready_for_review / approved / rejected).
 */

test.describe('/invoices bulk bar (acme admin)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});

	test('bulk bar appears after selecting a row', async ({ page }) => {
		await expect(page.locator('.bulk-bar')).toHaveCount(0);
		// Pick the first row whose checkbox is enabled (i.e. not system-
		// managed). The seed always has at least one such row.
		const enabledCheckbox = page
			.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
			.first();
		await enabledCheckbox.check();

		await expect(page.locator('.bulk-bar')).toBeVisible();
		await expect(page.locator('.bulk-count')).toContainText('1 selected');
	});

	test('Clear deselects everything and hides the bar', async ({ page }) => {
		const enabledCheckbox = page
			.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
			.first();
		await enabledCheckbox.check();
		await expect(page.locator('.bulk-bar')).toBeVisible();

		await page.locator('.bulk-bar button.bulk-clear').click();
		await expect(page.locator('.bulk-bar')).toHaveCount(0);
	});

	test('selecting two rows shows "2 selected"', async ({ page }) => {
		const enabled = page.locator(
			'table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])'
		);
		await enabled.nth(0).check();
		await enabled.nth(1).check();
		await expect(page.locator('.bulk-count')).toContainText('2 selected');
	});

	test('Change Status, Delete, and CSV/JSON/XML buttons are present', async ({
		page
	}) => {
		await page
			.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
			.first()
			.check();

		const bar = page.locator('.bulk-bar');
		await expect(bar.getByRole('button', { name: 'Change Status' })).toBeVisible();
		await expect(bar.getByRole('button', { name: 'Delete' })).toBeVisible();
		await expect(bar.getByRole('button', { name: 'CSV' })).toBeVisible();
		await expect(bar.getByRole('button', { name: 'JSON' })).toBeVisible();
		await expect(bar.getByRole('button', { name: 'XML' })).toBeVisible();
	});

	test('Change Status dropdown opens with the valid transitions', async ({ page }) => {
		await page
			.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
			.first()
			.check();
		await page.getByRole('button', { name: 'Change Status' }).click();

		const dropdown = page.locator('.bulk-status-dropdown select');
		await expect(dropdown).toBeVisible();
		// The select has at least one option (whichever state the
		// selected row's status can transition to per VALID_TRANSITIONS).
		const optionCount = await dropdown.locator('option').count();
		expect(optionCount).toBeGreaterThan(0);
	});
});

/**
 * Bulk "Rejected" must collect the reason the endpoint requires.
 *
 * `POST /api/invoices/bulk/status` answers 422 for a `rejected` target with no
 * `reason` (rejections route through `review.reject_invoice`, which records it
 * on the audit row and the `review_rejected` exception). The picker offered
 * Rejected for a `ready_for_review` selection and posted `{ids, status}` only,
 * so every bulk reject was a guaranteed error toast and no row ever moved.
 */
test.describe('/invoices bulk reject', () => {
	const MARKER = `E2E-BULKREJ-${Date.now()}`;

	test.beforeEach(() => {
		tenantPsql(
			`INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, vendor_name, amount, currency, status, created_at, updated_at)
			 VALUES (gen_random_uuid(), gen_random_uuid(), (SELECT organization_id FROM invoices LIMIT 1),
			         '${MARKER}', 'E2E Bulk Reject Vendor', 42.00, 'USD', 'ready_for_review', now(), now())`
		);
	});

	// The reject raises a `review_rejected` exception; `deleteInvoicesWhere`
	// owns that child along with the rest of the graph.
	test.afterEach(() => deleteInvoicesWhere(`invoice_number = '${MARKER}'`));

	test('asks for a reason, sends it, and the invoice is rejected', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		const searched = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes(`search=${MARKER}`)
		);
		await page.getByPlaceholder('Search invoices...').fill(MARKER);
		await searched;
		const row = page.locator('table tbody tr', { hasText: MARKER });
		await expect(row).toHaveCount(1);

		await row.locator('td.checkbox-col input[type="checkbox"]').check();
		await page.getByRole('button', { name: 'Change Status' }).click();
		await page.locator('.bulk-status-dropdown select').selectOption('rejected');

		const reason = page.getByRole('textbox', { name: 'Reason for rejecting the selected invoices' });
		const apply = page.locator('.bulk-status-dropdown').getByRole('button', { name: 'Apply' });
		await expect(reason).toBeVisible();
		// Nothing to send yet — the server would refuse it.
		await expect(apply).toBeDisabled();

		await reason.fill('Duplicate of an invoice already paid');
		await expect(apply).toBeEnabled();
		const posted = page.waitForResponse(
			(r) => r.url().includes('/api/invoices/bulk/status') && r.request().method() === 'POST'
		);
		await apply.click();
		const res = await posted;
		expect(res.status()).toBe(200);
		expect(res.request().postDataJSON()).toMatchObject({
			status: 'rejected',
			reason: 'Duplicate of an invoice already paid'
		});
		expect(((await res.json()) as { updated: number }).updated).toBe(1);

		const status = tenantPsql(`SELECT status FROM invoices WHERE invoice_number = '${MARKER}'`);
		expect(status.trim()).toBe('rejected');
	});

	test('a non-reject target shows no reason field', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		const searched = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes(`search=${MARKER}`)
		);
		await page.getByPlaceholder('Search invoices...').fill(MARKER);
		await searched;
		const row = page.locator('table tbody tr', { hasText: MARKER });
		await row.locator('td.checkbox-col input[type="checkbox"]').check();
		await page.getByRole('button', { name: 'Change Status' }).click();
		await page.locator('.bulk-status-dropdown select').selectOption('approved');
		await expect(page.locator('.bulk-status-dropdown input')).toHaveCount(0);
		await expect(
			page.locator('.bulk-status-dropdown').getByRole('button', { name: 'Apply' })
		).toBeEnabled();
	});
});
