import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * The shared menu button (`#lib/components/ui/MenuButton.svelte`, the WAI-ARIA
 * APG menu-button pattern), on its first user: the /invoices bulk bar's
 * Export menu (CSV / JSON / XML).
 *
 * The key-to-item rules are unit-tested (`src/lib/utils/menuButton.test.ts`);
 * what only a browser shows is that focus really moves, Escape and Tab really
 * close, an open menu passes axe in both themes, and the menu stays on screen
 * at 320px (1.4.10) above a bar pinned to the bottom of the viewport.
 */

async function storeTheme(page: Page, choice: 'light' | 'dark'): Promise<void> {
	await page.addInitScript((value) => {
		try {
			localStorage.setItem('feoh_theme', value);
		} catch {
			// about:blank — ignore
		}
	}, choice);
}

async function openBulkBar(page: Page) {
	await page.goto('/invoices');
	await expect(page.locator('table tbody tr').first()).toBeVisible();
	await page
		.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
		.first()
		.check();
	const bar = page.locator('.bulk-bar');
	await expect(bar).toBeVisible();
	return {
		trigger: bar.getByRole('button', { name: 'Export' }),
		menu: bar.getByRole('menu', { name: 'Export' }),
		item: (name: string) => bar.getByRole('menuitem', { name, exact: true })
	};
}

test.describe('menu button — /invoices bulk Export', () => {
	for (const theme of ['dark', 'light'] as const) {
		test(`the open menu has no axe violations in the ${theme} theme`, async ({ page }) => {
			await storeTheme(page, theme);
			const { trigger, menu } = await openBulkBar(page);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);

			await trigger.click();
			await expect(trigger).toHaveAttribute('aria-expanded', 'true');
			await expect(menu).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}

	test('keyboard: open on the first item, move, wrap, and Escape back to the trigger', async ({
		page
	}) => {
		const { trigger, menu, item } = await openBulkBar(page);
		await expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');

		await trigger.focus();
		await page.keyboard.press('Enter');
		await expect(menu).toBeVisible();
		await expect(trigger).toHaveAttribute('aria-expanded', 'true');
		await expect(item('CSV')).toBeFocused();

		await page.keyboard.press('ArrowDown');
		await expect(item('JSON')).toBeFocused();
		await page.keyboard.press('End');
		await expect(item('XML')).toBeFocused();
		await page.keyboard.press('ArrowDown');
		await expect(item('CSV')).toBeFocused();
		await page.keyboard.press('ArrowUp');
		await expect(item('XML')).toBeFocused();
		await page.keyboard.press('Home');
		await expect(item('CSV')).toBeFocused();
		await page.keyboard.press('j');
		await expect(item('JSON')).toBeFocused();

		await page.keyboard.press('Escape');
		await expect(menu).toBeHidden();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
		await expect(trigger).toBeFocused();

		// ArrowUp opens on the last item.
		await page.keyboard.press('ArrowUp');
		await expect(item('XML')).toBeFocused();

		// Tab closes the menu and moves focus on, out of it.
		await page.keyboard.press('Tab');
		await expect(menu).toBeHidden();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	});

	test('keyboard: Enter on an item runs it, closes the menu and returns focus', async ({
		page
	}) => {
		const { trigger, menu, item } = await openBulkBar(page);
		const exported = page.waitForRequest(
			(r) => new URL(r.url()).pathname === '/api/invoices/bulk/export' && r.method() === 'POST'
		);

		await trigger.focus();
		await page.keyboard.press('ArrowDown');
		await expect(item('CSV')).toBeFocused();
		await page.keyboard.press('ArrowDown');
		await page.keyboard.press('ArrowDown');
		await expect(item('XML')).toBeFocused();
		await page.keyboard.press('Enter');

		expect((await exported).postDataJSON()).toMatchObject({ format: 'xml' });
		await expect(menu).toBeHidden();
		await expect(trigger).toBeFocused();
	});

	test('a click outside closes the menu', async ({ page }) => {
		const { trigger, menu } = await openBulkBar(page);
		await trigger.click();
		await expect(menu).toBeVisible();

		await page.locator('h1').first().click();
		await expect(menu).toBeHidden();
		await expect(trigger).toHaveAttribute('aria-expanded', 'false');
	});

	test('at 320px the menu opens inside the viewport', async ({ page }) => {
		await page.setViewportSize({ width: 320, height: 640 });
		const { trigger, menu } = await openBulkBar(page);
		await trigger.click();
		await expect(menu).toBeVisible();

		const box = await menu.boundingBox();
		expect(box, 'menu has a layout box').not.toBeNull();
		expect(box!.x).toBeGreaterThanOrEqual(0);
		expect(box!.x + box!.width).toBeLessThanOrEqual(320);
		expect(box!.y).toBeGreaterThanOrEqual(0);
		expect(box!.y + box!.height).toBeLessThanOrEqual(640);
		expect(
			await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			),
			'the open menu widened the document'
		).toBeLessThanOrEqual(1);
	});
});
