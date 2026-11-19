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

test.describe('/invoices bulk bar at phone width (375px)', () => {
	test.use({ viewport: { width: 375, height: 740 } });

	test('the bar is the shared toolbar, fits the screen, and never covers the pagination', async ({
		page
	}) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await page
			.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
			.first()
			.check();

		// The shared <BulkBar>: a named toolbar, one button size throughout.
		const bar = page.getByRole('toolbar', { name: 'Bulk actions' });
		await expect(bar).toBeVisible();
		await expect(bar.locator('button.bulk-clear')).toHaveClass(/\bbtn-cancel\b/);
		await expect(bar.locator('button.bulk-delete-btn')).toHaveClass(/\bbtn-danger\b/);

		// No horizontal overflow: neither the page nor the wrapped bar.
		const widths = await page.evaluate(() => {
			const box = document.querySelector('.bulk-bar')!.getBoundingClientRect();
			return {
				scroll: document.documentElement.scrollWidth,
				viewport: window.innerWidth,
				left: box.left,
				right: box.right
			};
		});
		expect(widths.scroll).toBeLessThanOrEqual(widths.viewport);
		expect(widths.left).toBeGreaterThanOrEqual(0);
		expect(widths.right).toBeLessThanOrEqual(widths.viewport);

		// The page reserves the bar's height under the list, so the pagination
		// row scrolls clear of the fixed bar and still takes the click.
		const pagination = page.locator('.load-more-row').first();
		await pagination.scrollIntoViewIfNeeded();
		await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
		const [rowBox, barBox] = await Promise.all([pagination.boundingBox(), bar.boundingBox()]);
		expect(rowBox, 'the pagination row renders').not.toBeNull();
		expect(barBox).not.toBeNull();
		expect(rowBox!.y + rowBox!.height).toBeLessThanOrEqual(barBox!.y);
		const loadMore = pagination.locator('button.btn-load-more');
		if ((await loadMore.count()) > 0) {
			// `trial` runs every actionability check, the hit test included,
			// without loading the next page.
			await loadMore.click({ trial: true });
		}
	});
});

test.describe('/invoices advanced-filter button', () => {
	test('says how many advanced filters are set, to a screen reader too', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		const toggle = page.getByRole('button', { name: 'Advanced search', exact: true });
		await expect(toggle).not.toHaveAttribute('aria-describedby');

		await toggle.click();
		const dialog = page.locator('div.modal[role="dialog"][aria-label="Advanced search"]');
		await expect(dialog).toBeVisible();
		await dialog.getByLabel('Vendor', { exact: true }).fill('Acme');
		await dialog.locator('button.btn-apply').click();
		await expect(dialog).toHaveCount(0);

		await expect(toggle).toHaveAttribute('aria-describedby', 'advanced-filters-set');
		await expect(toggle).toHaveAccessibleDescription('1 filter set');
	});
});
