import { expect, test } from '../fixtures/helpers';

/**
 * Credit-memo Void is a two-click armed confirmation.
 *
 * Void is irreversible, yet it used to fire on a single click with no
 * confirm and no in-flight guard — a mis-click (or a double-click sending
 * two voids) destroyed a memo instantly. It now matches the app-wide
 * destructive-action pattern: first click arms (label → Confirm), an outside
 * click un-arms, and only the second click POSTs the void.
 */
test.describe('credit-memo void confirmation', () => {
	test('first click arms, outside click un-arms, second click voids', async ({ page }) => {
		// Every stub here matches the EXACT pathname. The vendor stub used to be
		// the regex `/\/api\/vendors/`, which under `vite dev` also matches the
		// module URL `/src/lib/api/vendors.ts` (imported by `ui/VendorPicker`);
		// answering it with JSON meant the route never loaded, so this spec
		// failed locally while CI's preview build stayed green (issue #443). The
		// page no longer fetches vendors on mount at all, so that stub is gone.
		await page.route((url) => url.pathname === '/api/credit-memos', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [
						{
							id: '00000000-0000-0000-0000-0000000000aa',
							memo_number: 'CM-VOID-1',
							vendor_name: 'Void Co',
							amount: '100.00',
							currency: 'USD',
							issued_date: '2026-06-01',
							invoice_number: null,
							status: 'open'
						}
					],
					total: 1
				})
			});
		});
		// The other list the page loads on mount (its invoice selects) — keep it
		// empty.
		await page.route(
			(url) => url.pathname === '/api/invoices',
			(r) =>
				r.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[],"total":0}' })
		);

		let voidCalls = 0;
		await page.route((url) => /^\/api\/credit-memos\/[^/]+\/void$/.test(url.pathname), async (route) => {
			voidCalls += 1;
			await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
		});

		await page.goto('/credit-memos');

		// Scope to the row-action cell — the status filter also has a "Void" chip.
		const table = page.getByRole('table');
		const voidBtn = table.getByRole('button', { name: 'Void', exact: true });
		await expect(voidBtn).toBeVisible();

		// First click arms — label flips to Confirm, no void sent yet.
		await voidBtn.click();
		const confirmBtn = table.getByRole('button', { name: 'Confirm', exact: true });
		await expect(confirmBtn).toBeVisible();
		expect(voidCalls).toBe(0);

		// Clicking outside the action un-arms it (back to Void), still no call.
		await page.locator('h1').click();
		await expect(table.getByRole('button', { name: 'Void', exact: true })).toBeVisible();
		expect(voidCalls).toBe(0);

		// Re-arm and confirm → exactly one void POST.
		await table.getByRole('button', { name: 'Void', exact: true }).click();
		await table.getByRole('button', { name: 'Confirm', exact: true }).click();
		await expect.poll(() => voidCalls).toBe(1);
	});
});
