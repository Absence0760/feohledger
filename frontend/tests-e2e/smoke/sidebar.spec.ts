import { expect, test } from '../fixtures/helpers';

/**
 * Shared side panel (the left nav `Sidebar`, rendered in every authenticated
 * route via `+layout.svelte`).
 *
 * The profile popover is an overlay menu opened from the sidebar footer. It
 * dismisses on backdrop click — but used to ignore Escape (its backdrop carried
 * a no-op `onkeydown`), stranding a keyboard user who opened it. These assert
 * both dismissal paths and that Escape restores focus to the trigger.
 *
 * Read-only (no mutation) → parallel-safe across worker tenants.
 */
test.describe('sidebar profile popover', () => {
	test('Escape closes the popover and restores focus to the trigger', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar')).toBeVisible();

		await page.locator('.profile-btn').click();
		await expect(page.locator('.profile-popover')).toBeVisible();

		await page.keyboard.press('Escape');
		await expect(page.locator('.profile-popover')).toBeHidden();

		// Focus must land back on the trigger, not be lost to <body> — so the
		// keyboard user keeps their place in the nav.
		await expect(page.locator('.profile-btn')).toBeFocused();
	});

	test('backdrop click still closes the popover', async ({ page }) => {
		await page.goto('/');
		await page.locator('.profile-btn').click();
		await expect(page.locator('.profile-popover')).toBeVisible();

		await page.locator('.profile-backdrop').click();
		await expect(page.locator('.profile-popover')).toBeHidden();
	});
});

/**
 * The notification bell lives in the sidebar header (it replaced the old
 * Notifications nav row). It opens a popover peek with a "View all" link to the
 * full page, and — like the other overlay menus — closes on Escape.
 */
test.describe('sidebar notification bell', () => {
	test('Notifications is no longer a top-level nav row', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expect(page.locator('aside.sidebar a.nav-item[href="/notifications"]')).toHaveCount(0);
		// The bell is the way in instead.
		await expect(page.locator('.bell-btn')).toBeVisible();
	});

	test('bell opens a popover; "View all" deep-links to the full page', async ({ page }) => {
		await page.goto('/');
		await page.locator('.bell-btn').click();
		await expect(page.locator('.bell-popover')).toBeVisible();

		// A link, not a button: it navigates. The arrow is decorative (aria-hidden).
		await page.getByRole('link', { name: 'View all', exact: true }).click();
		await expect(page).toHaveURL(/\/notifications$/);
	});

	test('Escape closes the bell popover and restores focus to the bell', async ({ page }) => {
		await page.goto('/');
		await page.locator('.bell-btn').click();
		await expect(page.locator('.bell-popover')).toBeVisible();

		await page.keyboard.press('Escape');
		await expect(page.locator('.bell-popover')).toBeHidden();
		await expect(page.locator('.bell-btn')).toBeFocused();
	});
});
