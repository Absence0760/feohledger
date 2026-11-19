import type { Page } from '@playwright/test';

import { acceptConsent, expect, test } from '../fixtures/helpers';

/**
 * WCAG 1.4.10 (reflow) on the supplier portal's lists.
 *
 * The invoices, payments and discount-offers tables carry seven or eight
 * columns, which cannot reflow at 390px. They render through `ui/DataTable`,
 * whose container scrolls sideways inside itself as a named, focusable region
 * — so the PAGE never scrolls horizontally, and a keyboard user can still
 * reach the clipped columns. Before, each was a bare `<table>` that pushed the
 * whole document sideways.
 *
 * The discount-offers list may be empty for the seeded vendor, so the region
 * is only asserted where rows exist; the no-page-scroll assertion holds on
 * every list regardless.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

test.use({ storageState: { cookies: [], origins: [] }, viewport: { width: 390, height: 844 } });

async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
}

async function pageScrollsSideways(page: Page): Promise<boolean> {
	return page.evaluate(
		() => document.documentElement.scrollWidth > document.documentElement.clientWidth,
	);
}

test.describe('/portal lists reflow at 390px', () => {
	test('the invoices table scrolls inside its own region, not the page', async ({ page }) => {
		await portalSignIn(page);
		await page.goto('/portal/invoices');
		await expect(page.locator('table tbody tr.clickable').first()).toBeVisible({
			timeout: 10_000,
		});

		const region = page.getByRole('region', { name: 'Your invoices' });
		await expect(region).toBeVisible();
		await expect(region).toHaveAttribute('tabindex', '0');
		expect(await pageScrollsSideways(page)).toBe(false);
	});

	for (const path of ['/portal/payments', '/portal/discount-offers']) {
		test(`${path} does not scroll the page sideways`, async ({ page }) => {
			await portalSignIn(page);
			await page.goto(path);
			// Settled: rows, the empty state, or the error — never the loading line.
			await expect(page.locator('main')).not.toContainText(/Loading/);
			expect(await pageScrollsSideways(page)).toBe(false);
		});
	}
});
