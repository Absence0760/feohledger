import { expect, test } from '../fixtures/helpers';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — the bulk selection must never outlive the rows it points at.
 *
 * The prune ran inside the loader's `try`, so the one path that empties the
 * table — the `catch`, which sets `exceptions = []` — skipped it. The floating
 * bulk bar went on reporting "N selected" over an empty table, and Resolve
 * would still have POSTed ids for rows nobody could see. The prune now runs in
 * the `finally`, against the rows the action can actually apply to
 * (open / escalated).
 *
 * Both the list and the summary are stubbed so the test doesn't depend on what
 * the shard's tenant happens to hold.
 */

function exceptionRow(n: number) {
	return {
		id: `00000000-0000-4000-9000-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-EXC-${n}`,
		vendor_name: 'E2E Vendor',
		amount: 100,
		exception_type: 'duplicate',
		type_label: 'Duplicate',
		severity: 'warning',
		description: 'stubbed',
		status: 'open',
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

test.describe('/exceptions — stale selection', () => {
	test('a failed refetch clears the selection instead of leaving a bulk bar over nothing', async ({
		page
	}) => {
		let failList = false;

		await page.route('**/api/exceptions/summary*', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(
					exceptionSummary({
						open: 2,
						escalated: 1,
						by_type: { duplicate: 2 },
						by_severity: { warning: 2 }
					})
				)
			});
		});

		await page.route('**/api/exceptions?*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/exceptions') {
				await route.continue();
				return;
			}
			if (failList) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [exceptionRow(1), exceptionRow(2)], total: 2 })
			});
		});

		await page.goto('/exceptions');

		const selectAll = page.getByLabel('Select all selectable exceptions');
		await expect(selectAll).toBeVisible();
		await selectAll.check();

		const bulkBar = page.locator('.bulk-bar');
		await expect(bulkBar).toBeVisible();

		// The next load fails; trigger one via a status chip.
		failList = true;
		await page.getByRole('button', { name: /^Escalated/ }).click();

		// The table is empty (error state) …
		await expect(page.getByTestId('table-empty')).toBeVisible();
		// … so nothing may still be selected.
		await expect(bulkBar).toHaveCount(0);
	});
});
