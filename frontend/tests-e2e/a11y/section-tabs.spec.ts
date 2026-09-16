import { expect, test } from '../fixtures/helpers';

/**
 * Section sub-tab bar (`$lib/components/layout/SectionTabs.svelte`).
 *
 * The bar lays a NAV group's children out as one horizontal row. It used to be
 * a flex container with `white-space: nowrap` children and neither `flex-wrap`
 * nor `overflow-x`, so a group with more tabs than fit widened the DOCUMENT and
 * the whole page scrolled sideways — clipping the sidebar and the page content,
 * and failing WCAG 1.4.10 (Reflow). Settings carried 15 tabs; Billing still
 * carries 9, which overflows a 1280px laptop.
 *
 * The fix is twofold and both halves are guarded here: the row can never exceed
 * its container, and whatever does not fit is reachable from a "More" menu
 * rather than simply invisible.
 */

/** Widths chosen to force overflow on a 9-tab group without hiding the bar. */
const NARROW = { width: 1000, height: 800 };

const pageOverflow = (page: import('@playwright/test').Page) =>
	page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

test.describe('section tabs — overflow', () => {
	test('a group with more tabs than fit never scrolls the document', async ({ page }) => {
		await page.setViewportSize(NARROW);
		// Billing (9 children) is the widest surviving group after the Settings
		// split, so it is the one that still overflows an ordinary laptop.
		await page.goto('/contracts');
		await expect(page.getByRole('navigation', { name: /billing sections/i })).toBeVisible();

		expect(await pageOverflow(page), 'the tab row widened the document').toBeLessThanOrEqual(1);

		// The row itself must also stay within its container — a row that merely
		// clipped would hide tabs with no way to reach them, which is what the
		// More button exists to prevent.
		const more = page.getByRole('button', { name: /more sections/i });
		await expect(more).toBeVisible();
	});

	test('the active tab stays in the row even when it would overflow', async ({ page }) => {
		await page.setViewportSize(NARROW);
		// Sweep Health is the LAST child of Settings, so at this width it falls
		// outside the fitting set and has to be pulled back in — a section whose
		// current page is only reachable by opening a menu reads as unselected.
		await page.goto('/admin/health');
		const bar = page.getByRole('navigation', { name: /settings sections/i });
		await expect(bar).toBeVisible();

		const active = bar.locator('a[aria-current="page"]');
		await expect(active).toHaveCount(1);
		await expect(active).toBeVisible();
		expect(await pageOverflow(page)).toBeLessThanOrEqual(1);
	});

	test('the More menu reveals the hidden tabs and closes on Escape', async ({ page }) => {
		await page.setViewportSize(NARROW);
		await page.goto('/contracts');

		const more = page.getByRole('button', { name: /more sections/i });
		await expect(more).toHaveAttribute('aria-expanded', 'false');

		await more.click();
		await expect(more).toHaveAttribute('aria-expanded', 'true');

		const menu = page.locator('#section-more-menu');
		await expect(menu).toBeVisible();
		// Whatever fell out of the row is reachable here — the menu is never empty
		// when it renders, and every item is a real destination.
		const items = menu.locator('a');
		expect(await items.count()).toBeGreaterThan(0);
		await expect(items.first()).toHaveAttribute('href', /\/.+/);

		// Esc closes it and returns focus to the trigger (2.1.2 / 2.4.3).
		await page.keyboard.press('Escape');
		await expect(menu).toBeHidden();
		await expect(more).toHaveAttribute('aria-expanded', 'false');
		await expect(more).toBeFocused();
	});

	test('navigating from the More menu lands on the page and closes the menu', async ({ page }) => {
		await page.setViewportSize(NARROW);
		await page.goto('/contracts');

		await page.getByRole('button', { name: /more sections/i }).click();
		const menu = page.locator('#section-more-menu');
		const first = menu.locator('a').first();
		const href = await first.getAttribute('href');
		await first.click();

		await expect(page).toHaveURL(new RegExp(`${href}$`));
		await expect(menu).toBeHidden();
		expect(await pageOverflow(page)).toBeLessThanOrEqual(1);
	});

	test('a group that fits shows every tab and no More button', async ({ page }) => {
		// Wide enough for Automation's 3 tabs — the bar must not manufacture an
		// overflow control it does not need.
		await page.setViewportSize({ width: 1600, height: 900 });
		await page.goto('/workflows');
		const bar = page.getByRole('navigation', { name: /automation sections/i });
		await expect(bar).toBeVisible();
		await expect(page.getByRole('button', { name: /more sections/i })).toHaveCount(0);
		expect(await pageOverflow(page)).toBeLessThanOrEqual(1);
	});
});
