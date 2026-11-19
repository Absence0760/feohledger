import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * Shared side panel (the left nav `Sidebar`, rendered in every authenticated
 * route via `+layout.svelte`).
 *
 * The profile popover is an overlay menu opened from the sidebar footer. It
 * dismisses on backdrop click — but used to ignore Escape (its backdrop carried
 * a no-op `onkeydown`), stranding a keyboard user who opened it. These assert
 * both dismissal paths and that Escape restores focus to the trigger, and that
 * focus leaving it closes it (it is drawn over the Help link, 2.4.11).
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

	test('is a disclosure: aria-expanded + aria-controls, no menu popup, Tab moves in', async ({
		page
	}) => {
		await page.goto('/');
		const trigger = page.locator('.profile-btn');
		// Its contents are identity, links and Log out, not menuitems, so the
		// trigger must not promise an ARIA menu (WAI-ARIA APG § Disclosure).
		await expect(trigger).not.toHaveAttribute('aria-haspopup', /.*/);
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');

		await trigger.click();
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');
		const popover = page.locator('.profile-popover');
		await expect(trigger).toHaveAttribute('aria-controls', (await popover.getAttribute('id'))!);

		// The content follows its trigger, so Tab goes into it, not past it.
		await page.keyboard.press('Tab');
		await expect(popover.locator('a[href="/profile"]')).toBeFocused();
	});

	test('backdrop click still closes the popover', async ({ page }) => {
		await page.goto('/');
		await page.locator('.profile-btn').click();
		await expect(page.locator('.profile-popover')).toBeVisible();

		await page.locator('.profile-backdrop').click();
		await expect(page.locator('.profile-popover')).toBeHidden();
	});

	// WCAG 2.4.11 Focus Not Obscured. The popover is drawn ABOVE its trigger
	// (`bottom: 100%`), over the Help link, so a disclosure left open while
	// focus moved on hid the very control focus moved to. Focus leaving the
	// wrapper closes it; focus itself is not moved.
	test('Shift+Tab out of the open popover closes it, and Help is focused and not covered', async ({
		page
	}) => {
		await page.goto('/');
		const trigger = page.locator('.profile-btn');
		await trigger.click();
		await expect(page.locator('.profile-popover')).toBeVisible();
		await expect(trigger).toBeFocused();

		await page.keyboard.press('Shift+Tab');
		await expect(page.locator('.profile-popover')).toBeHidden();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		const help = page.getByTestId('sidebar-help');
		await expect(help).toBeFocused();
		expect(await topmostIsInside(page, 'sidebar-help')).toBe(true);
	});

	test('Tab past Log out closes it and leaves focus on the next control', async ({ page }) => {
		await page.goto('/');
		await page.locator('.profile-btn').click();
		const popover = page.locator('.profile-popover');
		await expect(popover).toBeVisible();

		await page.keyboard.press('Tab'); // Profile & security
		await page.keyboard.press('Tab'); // Legal & privacy
		await page.keyboard.press('Tab'); // Log out
		await expect(popover.locator('.profile-logout')).toBeFocused();
		await expect(popover).toBeVisible();

		await page.keyboard.press('Tab');
		await expect(popover).toBeHidden();
		await expect(page.locator('.collapse-btn')).toBeFocused();
	});

	for (const theme of ['dark', 'light'] as const) {
		test(`open in the collapsed rail it has no axe violations in the ${theme} theme`, async ({
			page
		}) => {
			await page.addInitScript((value) => {
				try {
					localStorage.setItem('feoh_theme', value);
				} catch {
					// about:blank — ignore
				}
			}, theme);
			await page.goto('/');
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await page.locator('.collapse-btn').click();
			const trigger = page.locator('.profile-btn');
			await expect(trigger).toHaveClass(/collapsed/);

			await trigger.click();
			await expect(page.locator('.profile-popover')).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}
});

/** Whether the element drawn at the centre of `testId` is that element (or inside it). */
async function topmostIsInside(page: Page, testId: string): Promise<boolean> {
	return page.getByTestId(testId).evaluate((el) => {
		const r = el.getBoundingClientRect();
		const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
		return hit !== null && el.contains(hit);
	});
}

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
