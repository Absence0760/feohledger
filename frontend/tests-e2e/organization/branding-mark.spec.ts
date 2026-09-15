import type { Page, Route } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * The FeohLedger mark in the app chrome, and its white-label fork
 * (docs/decisions.md §171).
 *
 * The fallback used to be the letters "AP", which named nobody. The FeohLedger
 * mark does name somebody, so `brandMark` shows it only while the tenant's
 * product still carries the platform's name: a tenant that renamed the product
 * without a logo gets its own initial, and a configured logo beats both.
 *
 * `GET /api/organization/branding` is stubbed per test rather than written
 * through the admin PUT, so all three branches run without leaving the shard's
 * tenant rebranded for whichever spec runs next. The brand store loads once per
 * session, which is why each stub is installed before the first navigation.
 */

const BRANDING = '/api/organization/branding';

async function stubBranding(page: Page, brand: Record<string, string>): Promise<void> {
	await page.route(`**${BRANDING}*`, async (route: Route) => {
		if (new URL(route.request().url()).pathname !== BRANDING) {
			await route.continue();
			return;
		}
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(brand) });
	});
}

/** A 404 still lays out as a visible <img>; only a decoded image has a width. */
async function naturalWidth(page: Page, selector: string): Promise<number> {
	return page.locator(selector).evaluate((img: HTMLImageElement) => img.naturalWidth);
}

test.describe('sidebar brand mark', () => {
	test('an unbranded tenant shows the FeohLedger mark, decorative beside the name', async ({
		page
	}) => {
		await stubBranding(page, {});
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		const mark = header.locator('img.brand-mark');
		await expect(mark).toBeVisible();
		await expect(header.locator('.logo-text')).toHaveText('FeohLedger');
		// The product name is spelled out beside it, so the image stays silent.
		await expect(mark).toHaveAttribute('alt', '');
		await expect
			.poll(() => naturalWidth(page, '.sidebar-header img.brand-mark'))
			.toBeGreaterThan(0);
	});

	test('a renamed tenant without a logo gets its own initial, not the platform mark', async ({
		page
	}) => {
		await stubBranding(page, { product_name: 'Acme Payables' });
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		await expect(header.locator('.logo-text')).toHaveText('Acme Payables');
		await expect(header.locator('.logo-monogram')).toHaveText('A');
		await expect(header.locator('img.brand-mark')).toHaveCount(0);
	});

	test("a configured logo wins over the tenant's name", async ({ page }) => {
		const logo = 'https://cdn.example.test/acme-logo.svg';
		await page.route(logo, (route: Route) =>
			route.fulfill({
				status: 200,
				contentType: 'image/svg+xml',
				body: '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="24"/>'
			})
		);
		await stubBranding(page, { product_name: 'Acme Payables', logo_url: logo });
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		await expect(header.locator('img.logo-img')).toHaveAttribute('src', logo);
		await expect(header.locator('.logo-monogram')).toHaveCount(0);
		await expect(header.locator('img.brand-mark')).toHaveCount(0);
	});
});

test.describe('sign-in brand mark', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('the sign-in card pairs the mark with the product name', async ({ page }) => {
		await page.goto('/login');

		const mark = page.locator('.login-card img.brand-mark');
		await expect(mark).toBeVisible();
		await expect(page.getByRole('heading', { level: 1, name: 'FeohLedger' })).toBeVisible();
		await expect.poll(() => naturalWidth(page, '.login-card img.brand-mark')).toBeGreaterThan(0);
	});
});
