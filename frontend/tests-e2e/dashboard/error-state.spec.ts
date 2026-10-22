import { expect, test } from '../fixtures/helpers';

/**
 * Dashboard recoverable-error state.
 *
 * Regression for the silent dead-end: a failed `GET /api/dashboard` used
 * to leave `loading=false` with `data=null` and no `{:else}` branch, so
 * the single most-visited page rendered a blank shell with no error, no
 * retry, no toast. This spec forces the dashboard fetch to fail and asserts
 * the page surfaces an error + a Retry control, then recovers when the
 * next fetch succeeds.
 *
 * It also pins what the KPI row does on this state: the row is no longer gated
 * on the response, so a failed load renders it `unavailable` — dashes, not
 * busy, not tinted — above the banner rather than collapsing
 * (`docs/decisions.md` §154). The pending half of the same convention is
 * `a11y/kpi-pending.spec.ts`.
 */
test.describe('dashboard error state', () => {
	test('failed load shows an error + retry, and retry recovers', async ({ page }) => {
		// Fail only the FIRST dashboard fetch; let the retry through.
		let failed = false;
		await page.route('**/api/dashboard', async (route) => {
			if (!failed) {
				failed = true;
				await route.fulfill({ status: 500, body: '{"detail":"boom"}' });
			} else {
				await route.continue();
			}
		});

		await page.goto('/');

		// Error branch is visible (not a blank page). Scope to the dashboard's
		// own error block — the global Toast live-regions also carry role="alert".
		const errorBox = page.getByTestId('dashboard-error');
		await expect(errorBox).toBeVisible();
		const retry = errorBox.getByRole('button');
		await expect(retry).toBeVisible();

		// The KPI row renders here too, as an `unavailable` row: five dashes, no
		// `aria-busy` (nothing is arriving), no verdict tint — with this banner
		// directly below saying why. That placement is `docs/decisions.md` §154,
		// mirroring /cfo: the dashes are what the page knows, the banner is why.
		// The row used to live inside `{:else if data}`, so a failed load showed
		// the banner with nothing above it.
		const row = page.locator('.kpi-row');
		await expect(row.locator('.kpi[data-kpi-state="unavailable"]')).toHaveCount(5);
		await expect(row.locator('.kpi[aria-busy]')).toHaveCount(0);
		await expect(row.locator('.kpi.highlight-green, .kpi.highlight-red')).toHaveCount(0);

		// Retrying succeeds → the cards hold real figures and the error block is
		// gone. Asserted on the card STATE rather than on `.kpi` being visible:
		// the row is on screen in the error state now, so mere visibility would
		// have passed before the click and proved nothing about the recovery.
		await retry.click();
		await expect(row.locator('.kpi[data-kpi-state="value"]').first()).toBeVisible();
		await expect(page.getByTestId('dashboard-error')).toHaveCount(0);
	});
});
