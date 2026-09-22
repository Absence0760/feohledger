import { expect, test } from '../fixtures/helpers';
import { exceptionSummary } from './summary';

/**
 * /exceptions — loading + error states for the queue table.
 *
 * Regression: `loadExceptions()` set no loading flag (only `loadingMore`, for
 * the Load-More button) and its `catch` only fired a toast. So the table showed
 * "No open exceptions. Everything looks good!" WHILE the fetch was in flight —
 * and permanently after a failed one. On this page that empty message is a
 * substantive claim: no open duplicates, no fraud flags, no payment-compliance
 * holds, no line-total mismatches. Asserting it when we never managed to look is
 * the worst possible failure mode for a financial-control queue.
 *
 * The dashboard and /notifications already implement the right pattern; this
 * page now matches it.
 *
 * Stubbed so both states are deterministic — a real backend answers instantly
 * and never fails on demand.
 */

const EMPTY_SUMMARY = exceptionSummary();

test.describe('/exceptions load states', () => {
	test('a FAILED load says so — never "Everything looks good!"', async ({ page }) => {
		await page.route('**/api/exceptions/summary*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(EMPTY_SUMMARY)
			})
		);
		await page.route('**/api/exceptions?**', (route) =>
			route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			})
		);

		await page.goto('/exceptions');

		const empty = page.getByTestId('table-empty');
		await expect(empty).toBeVisible({ timeout: 10_000 });
		await expect(empty).toHaveText('Could not load exceptions. Try again.');
		// The critical negative: we must not claim the queue is clean.
		await expect(empty).not.toContainText('Everything looks good');
		await expect(empty).not.toContainText('No exceptions found');
	});

	test('a SLOW load shows the loading state, not the clean-queue message', async ({ page }) => {
		await page.route('**/api/exceptions/summary*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(EMPTY_SUMMARY)
			})
		);

		// Hold the list request open until the assertion below has run. This is a
		// real readiness gate, not a sleep: the response is released by resolving
		// the promise, so nothing is timing-dependent.
		let release!: () => void;
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		await page.route('**/api/exceptions?**', async (route) => {
			await held;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 })
			});
		});

		await page.goto('/exceptions');

		const empty = page.getByTestId('table-empty');
		await expect(empty).toBeVisible({ timeout: 10_000 });
		await expect(empty).toHaveText('Loading…');
		await expect(empty).not.toContainText('Everything looks good');

		release();

		// Once the (genuinely empty) response lands, the clean-queue message is
		// the honest one — and now it is earned.
		await expect(empty).toHaveText('No open exceptions. Everything looks good!', {
			timeout: 10_000
		});
	});

	test('a successful load with rows renders them, not an empty state', async ({ page }) => {
		await page.route('**/api/exceptions/summary*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(
					exceptionSummary({ open: 1, by_type: { duplicate: 1 }, by_severity: { error: 1 } })
				)
			})
		);
		await page.route('**/api/exceptions?**', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [
						{
							id: 'ex_1',
							invoice_id: 'inv_1',
							invoice_number: 'EXC-STUB-1',
							vendor_name: 'Stub Vendor',
							amount: 100.0,
							exception_type: 'duplicate',
							type_label: 'Duplicate',
							severity: 'error',
							description: 'Possible duplicate',
							status: 'open',
							resolution: null,
							resolved_by: null,
							resolved_at: null,
							assigned_to: null,
							assigned_to_user_id: null,
							due_at: null,
							is_overdue: false,
							time_to_resolution_hours: null,
							created_at: '2026-06-01T00:00:00Z'
						}
					],
					total: 1,
					page: 1,
					page_size: 20
				})
			})
		);

		await page.goto('/exceptions');

		await expect(page.getByText('EXC-STUB-1')).toBeVisible({ timeout: 10_000 });
		await expect(page.getByTestId('table-empty')).toHaveCount(0);
	});
});
