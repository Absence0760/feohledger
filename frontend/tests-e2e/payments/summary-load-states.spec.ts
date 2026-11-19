import { expect, test } from '../fixtures/helpers';

/**
 * /payments — a failed summary load is stated in place, with a Retry.
 *
 * The four headline cards stay drawn from first paint (layout stability), so a
 * failed `GET /api/payments/summary` used to leave them on the no-figure dash
 * with only a toast to explain it — and the toast fades, leaving a strip of
 * dashes and no way to ask again short of reloading the page.
 *
 * Stubbed so the failure is deterministic: a real backend never fails on
 * demand. The retry is let through to the real endpoint.
 */

test.describe('/payments summary load states', () => {
	test('a FAILED summary load offers a retry that reloads the cards', async ({ page }) => {
		let attempts = 0;
		await page.route('**/api/payments/summary', async (route) => {
			attempts += 1;
			if (attempts === 1) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.continue();
		});

		await page.goto('/payments');

		const error = page.getByTestId('summary-error');
		await expect(error).toBeVisible({ timeout: 10_000 });
		await expect(error).toContainText('Could not load the payment summary.');
		// The cards are still drawn (no layout shift), but no longer busy.
		await expect(page.locator('.summary-cards')).not.toHaveAttribute('aria-busy', 'true');

		await error.getByRole('button', { name: 'Retry' }).click();
		// The flag clears when the retry STARTS and stays down once it lands.
		await expect(error).toHaveCount(0);
		// The payment-count card holds a real figure, not the no-figure dash.
		await expect(page.locator('.summary-cards .scard-value').nth(3)).toHaveText(/\d/, {
			timeout: 10_000
		});
		expect(attempts).toBeGreaterThan(1);
	});
});
