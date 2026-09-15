import type { Page, Route } from '@playwright/test';
import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * The FeohLedger mark in the app chrome, and its white-label fork
 * (docs/decisions.md §171).
 *
 * The fallback used to be the letters "AP", which named nobody. The FeohLedger
 * mark does name somebody, so `brandMark` shows it only while the tenant's
 * product still carries the platform's name: a tenant that renamed the product
 * without a logo gets its own initial (or nothing, where the name is already
 * spelled out), and a configured logo beats both. The employee sidebar and the
 * supplier portal share that fork.
 *
 * The branding reads (`GET /api/organization/branding`, the public
 * `GET /api/portal/branding`) are stubbed per test rather than written through
 * the admin PUT, so every branch runs without leaving the shard's tenant
 * rebranded for whichever spec runs next. Both brand stores load once per
 * session, which is why each stub is installed before the first navigation.
 */

const BRANDING = '/api/organization/branding';
const PORTAL_BRANDING = '/api/portal/branding';
const LOGO = 'https://cdn.example.test/acme-logo.svg';

async function stubJson(page: Page, pathname: string, body: unknown): Promise<void> {
	await page.route(`**${pathname}*`, async (route: Route) => {
		if (new URL(route.request().url()).pathname !== pathname) {
			await route.continue();
			return;
		}
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
	});
}

async function stubLogo(page: Page): Promise<void> {
	await page.route(LOGO, (route: Route) =>
		route.fulfill({
			status: 200,
			contentType: 'image/svg+xml',
			body: '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="24"/>'
		})
	);
}

/** A 404 still lays out as a visible <img>; only a decoded image has a width. */
async function naturalWidth(page: Page, selector: string): Promise<number> {
	return page.locator(selector).first().evaluate((img: HTMLImageElement) => img.naturalWidth);
}

test.describe('sidebar brand mark', () => {
	test('an unbranded tenant shows the FeohLedger mark, decorative beside the name', async ({
		page
	}) => {
		await stubJson(page, BRANDING, {});
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

	test('collapsed, the mark is all that names the product, so it is labelled', async ({
		page
	}) => {
		await stubJson(page, BRANDING, {});
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		const mark = header.locator('img.brand-mark');
		await expect(header.locator('.logo-text')).toHaveText('FeohLedger');
		await expect(mark).toHaveAttribute('alt', '');

		await page.locator('.collapse-btn').click();
		await expect(header.locator('.logo-text')).toHaveCount(0);
		await expect(mark).toHaveAttribute('alt', 'FeohLedger');

		await page.locator('.collapse-btn').click();
		await expect(header.locator('.logo-text')).toHaveText('FeohLedger');
		await expect(mark).toHaveAttribute('alt', '');
	});

	test('a renamed tenant without a logo gets its own initial, not the platform mark', async ({
		page
	}) => {
		await stubJson(page, BRANDING, { product_name: 'Acme Payables' });
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		await expect(header.locator('.logo-text')).toHaveText('Acme Payables');
		await expect(header.locator('.logo-monogram')).toHaveText('A');
		await expect(header.locator('img.brand-mark')).toHaveCount(0);
	});

	test('collapsed, the monogram is announced as the product name', async ({ page }) => {
		await stubJson(page, BRANDING, { product_name: 'Acme Payables' });
		await page.goto('/');

		const monogram = page.locator('.sidebar-header .logo-monogram');
		await expect(monogram).toHaveText('A');
		// Expanded, the name follows it, so the letter is hidden from assistive tech.
		await expect(monogram).toHaveAttribute('aria-hidden', 'true');

		await page.locator('.collapse-btn').click();
		await expect(monogram).toHaveAttribute('role', 'img');
		await expect(monogram).toHaveAttribute('aria-label', 'Acme Payables');
		await expect(page.getByRole('img', { name: 'Acme Payables' })).toBeVisible();
	});

	test("a configured logo wins over the tenant's name", async ({ page }) => {
		await stubLogo(page);
		await stubJson(page, BRANDING, { product_name: 'Acme Payables', logo_url: LOGO });
		await page.goto('/');

		const header = page.locator('.sidebar-header');
		await expect(header.locator('img.logo-img')).toHaveAttribute('src', LOGO);
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

	for (const path of ['/login/forgot-password', '/login/reset-password']) {
		test(`the ${path} card carries the mark`, async ({ page }) => {
			await page.goto(path);

			await expect(page.locator('.auth-card img.brand-mark')).toBeVisible();
			await expect.poll(() => naturalWidth(page, '.auth-card img.brand-mark')).toBeGreaterThan(0);
		});
	}

	test('the MFA challenge card carries the mark', async ({ page }) => {
		// Same seeding as auth/mfa.spec.ts: the page reads its challenge from
		// sessionStorage and bounces to /login without one.
		await page.goto('/login');
		const challenge = {
			mfa_required: true,
			mfa_challenge_token: 'e2e-fake-challenge-token',
			methods: ['totp'],
			must_enroll: false
		};
		await page.evaluate(
			([key, value]) => sessionStorage.setItem(key, value),
			['mfa_challenge', JSON.stringify(challenge)] as const
		);
		await page.goto('/login/mfa');

		await expect(page.getByRole('heading', { name: 'Two-factor verification' })).toBeVisible();
		await expect(page.locator('.login-card img.brand-mark')).toBeVisible();
	});
});

test.describe('signup brand mark', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	test('the self-service signup card carries the mark', async ({ page }) => {
		await page.goto('/signup');

		await expect(page.locator('form.card img.brand-mark')).toBeVisible();
	});
});

test.describe('supplier portal brand mark', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('an unbranded tenant shows the FeohLedger mark on the portal sign-in', async ({ page }) => {
		await stubJson(page, PORTAL_BRANDING, {});
		await page.goto('/portal/login');

		const card = page.locator('.login-card');
		await expect(card.getByRole('heading', { name: 'FeohLedger' })).toBeVisible();
		await expect(card.locator('img.brand-mark')).toBeVisible();
	});

	test("a renamed tenant's portal never shows the platform mark", async ({ page }) => {
		await stubJson(page, PORTAL_BRANDING, { product_name: 'Acme Payables' });
		await page.goto('/portal/login');

		const card = page.locator('.login-card');
		await expect(card.getByRole('heading', { name: 'Acme Payables' })).toBeVisible();
		await expect(card.locator('img.brand-mark')).toHaveCount(0);
	});

	test("a tenant's logo replaces the mark on the portal sign-in", async ({ page }) => {
		await stubLogo(page);
		await stubJson(page, PORTAL_BRANDING, { product_name: 'Acme Payables', logo_url: LOGO });
		await page.goto('/portal/login');

		const card = page.locator('.login-card');
		await expect(card.locator('img.brand-logo')).toHaveAttribute('src', LOGO);
		await expect(card.locator('img.brand-mark')).toHaveCount(0);
	});
});
