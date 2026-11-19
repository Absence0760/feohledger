import { expect, test } from '../fixtures/helpers';

/**
 * Vendor filter-chip counts reflect the whole set, not the loaded page.
 *
 * Regression: the "Unverified" attention badge was computed from the loaded
 * (page-1, size-20) vendor array, so it silently undercounted unverified
 * vendors past the first page — exactly the badge meant to flag attention.
 * The page now reads tallies from GET /api/vendors/counts. We mock that
 * endpoint to report 42 unverified while the list page returns only 2 rows,
 * and assert the chip shows 42 (the whole-set tally), not 2.
 */
test.describe('vendors chip counts', () => {
	test('Unverified badge reflects the counts endpoint, not the page', async ({ page }) => {
		await page.route('**/api/vendors/counts**', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ total: 45, by_status: { unverified: 42, active: 3 } })
			});
		});
		// The list page returns only two rows — far fewer than the 42 unverified.
		await page.route(/\/api\/vendors\?/, async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [
						{ id: '00000000-0000-0000-0000-000000000001', name: 'Loaded One', code: 'L1', status: 'unverified', source: 'manual', invoice_count: 0 },
						{ id: '00000000-0000-0000-0000-000000000002', name: 'Loaded Two', code: 'L2', status: 'active', source: 'manual', invoice_count: 0 }
					],
					total: 45,
					page: 1,
					page_size: 20
				})
			});
		});

		await page.goto('/vendors');

		// The Unverified chip carries the whole-set count (42). Without the fix
		// it would show the loaded-page tally (1 unverified row mocked above).
		const unverifiedChip = page.locator('.filter-chip', { hasText: 'Unverified' });
		await expect(unverifiedChip).toBeVisible();
		await expect(unverifiedChip).toContainText('42');
	});

	// `?status=inactive` is a filter the page and the backend list both honour;
	// it used to have no chip, so the deactivated-only view looked unfiltered
	// and could not be cleared from the chip row.
	test('?status=inactive shows a pressed Inactive chip, and All clears it', async ({ page }) => {
		await page.goto('/vendors?status=inactive');
		const inactiveChip = page.locator('.filter-chip', { hasText: 'Inactive' });
		await expect(inactiveChip).toHaveAttribute('aria-pressed', 'true');
		await page.locator('.filter-chip', { hasText: 'All' }).click();
		await expect(inactiveChip).toHaveAttribute('aria-pressed', 'false');
		await expect(page).not.toHaveURL(/[?&]status=/);
	});
});
