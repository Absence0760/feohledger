import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * `ui/Toast.svelte` in a real page (decisions §307, WCAG 2.2.1 / 2.4.3 /
 * 2.4.11). The queue's rules are unit-tested in `utils/toastQueue.test.ts`;
 * this pins the WIRING those tests cannot see — the hover/focus pause reaching
 * the queue, an error outliving the old five seconds, focus going back where it
 * came from, the region's name, the dismiss target, and the region clearing
 * the bulk-action bar.
 *
 * Time is moved with Playwright's clock (`page.clock`), never a sleep. The
 * toasts come from /vendors' "Sync from ERP": the org's ERP is stubbed to the
 * mock adapter (so the button is offered) and the sync endpoint is stubbed, so
 * nothing is written.
 */

async function offerSync(page: Page, sync: { status: number; json: unknown }) {
	await page.route(
		(url) => url.pathname === '/api/organization',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			await route.fulfill({
				response: real,
				json: { ...body, settings: { ...body.settings, erp: { type: 'mock', integration_method: 'direct' } } }
			});
		}
	);
	await page.route(
		(url) => url.pathname === '/api/vendors/sync-erp',
		(route) => route.fulfill(sync)
	);
}

const region = (page: Page) => page.getByRole('region', { name: 'Status messages' });

test.describe('toast timing and focus', () => {
	test('an error toast stays until dismissed; focus returns to where it came from', async ({
		page
	}) => {
		await page.clock.install();
		await offerSync(page, { status: 500, json: { detail: 'Sync exploded' } });
		await page.goto('/vendors');

		const sync = page.getByRole('button', { name: 'Sync from ERP' });
		await sync.click();
		const toast = region(page).getByTestId('toast').filter({ hasText: 'Sync exploded' });
		await expect(toast).toBeVisible();

		await page.clock.fastForward(60_000);
		await expect(toast).toBeVisible();

		const dismiss = toast.getByRole('button', { name: 'Dismiss' });
		const box = await dismiss.boundingBox();
		expect(box!.width).toBeGreaterThanOrEqual(24);
		expect(box!.height).toBeGreaterThanOrEqual(24);

		// Keyboard: into the toast from the Sync button, then dismiss.
		await sync.focus();
		await dismiss.focus();
		await page.keyboard.press('Enter');
		await expect(toast).toHaveCount(0);
		await expect(sync).toBeFocused();
	});

	test('a success toast pauses while hovered and resumes after', async ({ page }) => {
		await page.clock.install();
		await offerSync(page, {
			status: 200,
			json: { created: 1, updated: 0, unchanged: 0 }
		});
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Sync from ERP' }).click();

		const toast = region(page).getByTestId('toast').filter({ hasText: /Synced 1 new/ });
		await expect(toast).toBeVisible();
		await toast.hover();
		await page.clock.fastForward(30_000);
		await expect(toast).toBeVisible();

		await page.mouse.move(5, 5);
		await page.clock.fastForward(6_000);
		await expect(toast).toHaveCount(0);
	});

	test('a toast pauses while focus is inside the region', async ({ page }) => {
		await page.clock.install();
		await offerSync(page, { status: 200, json: { created: 1, updated: 0, unchanged: 0 } });
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Sync from ERP' }).click();

		const toast = region(page).getByTestId('toast').filter({ hasText: /Synced 1 new/ });
		await toast.getByRole('button', { name: 'Dismiss' }).focus();
		await page.clock.fastForward(30_000);
		await expect(toast).toBeVisible();
	});

	test('the region rises above the bulk-action bar', async ({ page }) => {
		await offerSync(page, { status: 500, json: { detail: 'Sync exploded' } });
		await page.goto('/vendors');
		await page.locator('table tbody input[type="checkbox"]').first().check();
		const bar = page.getByRole('toolbar');
		await expect(bar).toBeVisible();

		await page.getByRole('button', { name: 'Sync from ERP' }).click();
		const toast = region(page).getByTestId('toast').filter({ hasText: 'Sync exploded' });
		await expect(toast).toBeVisible();

		await expect
			.poll(async () => {
				const t = await toast.boundingBox();
				const b = await bar.boundingBox();
				return t && b ? t.y + t.height <= b.y : false;
			})
			.toBe(true);
	});
});
