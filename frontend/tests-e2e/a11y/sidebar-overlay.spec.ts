import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * The sidebar on a phone (#635 finding 39; WCAG 1.4.10, 2.1.2, 2.4.3).
 *
 * At 700px and below the sidebar starts as its 60px icon rail. Expanding it
 * used to push the page right by the full 220px panel, which at 320px left the
 * content about 100px wide. On a narrow viewport the expanded panel is now an
 * OVERLAY: the page keeps the rail's offset and its full width, a backdrop
 * covers it, the page is inert while the panel is open, focus moves into the
 * panel on open and back to the toggle on close, and Escape, the backdrop and
 * following a link each close it. Desktop keeps the push layout untouched.
 */

const toggle = (page: Page) =>
	page.getByRole('button', { name: /^(Expand|Collapse) sidebar$/ });
const main = (page: Page) => page.locator('main#main-content');
const backdrop = (page: Page) => page.getByTestId('sidebar-backdrop');
const primaryNav = (page: Page) => page.getByRole('navigation', { name: 'Primary' });

async function openDashboard(page: Page, width: number) {
	await page.setViewportSize({ width, height: 720 });
	await page.goto('/');
	await expect(page.locator('aside.sidebar')).toBeVisible();
	await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

async function expectOpen(page: Page, width: number) {
	await expect(page.locator('aside.sidebar')).toHaveClass(/\boverlay\b/);
	// The full panel, once its width animation has landed.
	await expect.poll(async () => (await page.locator('aside.sidebar').boundingBox())?.width).toBe(220);
	await expect(backdrop(page)).toBeVisible();
	await expect(main(page)).toHaveAttribute('inert', '');
	await expect(page.locator('a.skip-link')).toHaveAttribute('inert', '');
	expect(await page.evaluate(() => document.body.style.overflow)).toBe('hidden');

	// The page underneath keeps its width: only the rail's 60px is taken.
	// (Polled: the shell animates `margin-left`, and the rail's first frame
	// can still be settling from the desktop default.)
	await expect.poll(async () => (await main(page).boundingBox())?.x).toBe(60);
	expect((await main(page).boundingBox())?.width).toBe(width - 60);
	// Focus went into the panel — to its first nav row.
	await expect(primaryNav(page).getByRole('link').first()).toBeFocused();
}

async function expectClosed(page: Page) {
	await expect(page.locator('aside.sidebar')).toHaveClass(/\bcollapsed\b/);
	await expect(backdrop(page)).toHaveCount(0);
	await expect(main(page)).not.toHaveAttribute('inert', /.*/);
	await expect(page.locator('a.skip-link')).not.toHaveAttribute('inert', /.*/);
	expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
}

for (const width of [375, 320]) {
	test.describe(`sidebar overlay at ${width}px`, () => {
		test('opens over the page and closes on Escape and on the backdrop', async ({ page }) => {
			await openDashboard(page, width);

			// Starts as the rail, and the page beside it is live.
			await expect(toggle(page)).toHaveAccessibleName('Expand sidebar');
			await expectClosed(page);

			await toggle(page).click();
			await expectOpen(page, width);

			// WCAG 1.4.10 in the expanded state too: the panel floats, so the
			// document must not scroll sideways under it.
			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow).toBeLessThanOrEqual(1);

			// Escape closes it and puts focus back on the toggle.
			await page.keyboard.press('Escape');
			await expectClosed(page);
			await expect(toggle(page)).toBeFocused();

			// The backdrop closes it too — clicked beside the panel, on the page.
			await toggle(page).click();
			await expectOpen(page, width);
			await backdrop(page).click({ position: { x: width - 20, y: 360 } });
			await expectClosed(page);
			await expect(toggle(page)).toBeFocused();
		});

		test('a popup inside the panel takes Escape before the panel does', async ({ page }) => {
			await openDashboard(page, width);
			await toggle(page).click();
			await expectOpen(page, width);

			await page.locator('.bell-btn').click();
			await expect(page.getByTestId('bell-popover')).toBeVisible();

			await page.keyboard.press('Escape');
			await expect(page.getByTestId('bell-popover')).toHaveCount(0);
			await expect(backdrop(page)).toBeVisible();

			await page.keyboard.press('Escape');
			await expectClosed(page);
		});

		test('the profile menu takes Escape before the panel does', async ({ page }) => {
			await openDashboard(page, width);
			await toggle(page).click();
			await expectOpen(page, width);

			const profile = page.locator('.profile-btn');
			await profile.click();
			await expect(profile).toHaveAttribute('aria-expanded', 'true');

			await page.keyboard.press('Escape');
			await expect(profile).toHaveAttribute('aria-expanded', 'false');
			await expect(profile).toBeFocused();
			await expect(backdrop(page)).toBeVisible();

			await page.keyboard.press('Escape');
			await expectClosed(page);
		});

		test('the panel stays above a bulk-action bar', async ({ page }) => {
			await page.setViewportSize({ width, height: 720 });
			// Status "new": selectable rows (the seed always has some), where an
			// unfiltered first page can be all system-managed, disabled rows.
			await page.goto('/invoices?status=new');
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			await page.locator('tbody input[type="checkbox"]:not([disabled])').first().check();
			await expect(page.locator('.bulk-bar').first()).toBeVisible();

			await toggle(page).click();
			await expectOpen(page, width);

			// The bar really does cross the panel's footer at this width, so the
			// order they stack in decides what a reader sees. Compared as
			// z-indexes because hit-testing cannot answer it: the bar lives in
			// the inert <main>, which `elementFromPoint` looks straight through.
			// Neither <main> nor its ancestors form a stacking context, so both
			// numbers compete in the root one.
			const layers = await page.evaluate(() => {
				const bar = document.querySelector('.bulk-bar')!;
				const aside = document.querySelector('aside.sidebar')!;
				const b = bar.getBoundingClientRect();
				const a = aside.getBoundingClientRect();
				return {
					overlaps: b.left < a.right && b.top < a.bottom && b.bottom > a.top,
					bar: Number(getComputedStyle(bar).zIndex),
					panel: Number(getComputedStyle(aside).zIndex)
				};
			});
			expect(layers.overlaps).toBe(true);
			expect(layers.panel).toBeGreaterThan(layers.bar);
		});

		test('following a link closes it', async ({ page }) => {
			await openDashboard(page, width);
			await toggle(page).click();
			await expectOpen(page, width);

			await primaryNav(page).getByRole('link', { name: 'Invoices' }).click();
			await expect(page).toHaveURL(/\/invoices/);
			await expectClosed(page);
		});
	});
}

test('the open overlay passes axe in both themes', async ({ page }) => {
	await openDashboard(page, 375);
	await toggle(page).click();
	await expectOpen(page, 375);
	for (const colorScheme of ['dark', 'light'] as const) {
		await page.emulateMedia({ colorScheme });
		await expectNoA11yViolations(page);
	}
});

test('desktop keeps the push layout, with no overlay', async ({ page }) => {
	await openDashboard(page, 1280);

	await expect(toggle(page)).toHaveAccessibleName('Collapse sidebar');
	await expect(page.locator('aside.sidebar')).not.toHaveClass(/\boverlay\b/);
	await expect(backdrop(page)).toHaveCount(0);
	await expect(main(page)).not.toHaveAttribute('inert', /.*/);
	expect((await main(page).boundingBox())?.x).toBe(220);

	// Collapsing to the rail pulls the page left with it, as before.
	await toggle(page).click();
	await expect(page.locator('aside.sidebar')).toHaveClass(/\bcollapsed\b/);
	await expect.poll(async () => (await main(page).boundingBox())?.x).toBe(60);
	await expect(backdrop(page)).toHaveCount(0);

	// Escape on desktop is not a sidebar shortcut. Opening and closing the
	// profile menu after it gives the press a re-render to have shown up in.
	await toggle(page).click();
	await expect.poll(async () => (await main(page).boundingBox())?.x).toBe(220);
	await page.keyboard.press('Escape');
	const profile = page.locator('.profile-btn');
	await profile.click();
	await expect(profile).toHaveAttribute('aria-expanded', 'true');
	await page.keyboard.press('Escape');
	await expect(profile).toHaveAttribute('aria-expanded', 'false');
	await expect(toggle(page)).toHaveAccessibleName('Collapse sidebar');
	expect((await main(page).boundingBox())?.x).toBe(220);
});

test('resizing while open hands the layout over cleanly', async ({ page }) => {
	await openDashboard(page, 375);
	await toggle(page).click();
	await expectOpen(page, 375);

	// Narrow → wide with the panel open: it becomes the desktop push panel.
	await page.setViewportSize({ width: 1280, height: 720 });
	await expect(backdrop(page)).toHaveCount(0);
	await expect(main(page)).not.toHaveAttribute('inert', /.*/);
	expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
	await expect.poll(async () => (await main(page).boundingBox())?.x).toBe(220);

	// Wide → narrow while expanded: back to the rail, no overlay.
	await page.setViewportSize({ width: 375, height: 720 });
	await expectClosed(page);
	await expect.poll(async () => (await main(page).boundingBox())?.x).toBe(60);
});
