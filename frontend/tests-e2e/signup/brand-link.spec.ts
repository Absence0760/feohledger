import type { Page } from '@playwright/test';
import { VANITY_ORIGIN } from '../fixtures/env';
import { ACME_BASE, expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * The form-column mark on the pre-auth pages links home on the platform apex
 * only (`components/auth/AuthBrandMark.svelte`, issue #635).
 *
 * On the apex `/` is the landing page the visitor came from, so the mark is
 * the way back to it. On a workspace subdomain `/` only redirects to
 * `/login`, and on a customer's vanity host a link to FeohLedger's marketing
 * page would be platform branding the white-label partner never chose — so
 * there the mark stays a plain, silent image.
 */

test.use({ storageState: { cookies: [], origins: [] } });

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

test.describe('on the platform apex', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	test('signup: the form mark is a named link home, and the panel mark is not a second one', async ({
		page
	}) => {
		await page.goto('/signup');
		const home = page.getByRole('link', { name: 'FeohLedger home' });
		await expect(home).toHaveCount(1);
		await expect(home).toHaveAttribute('href', '/');
		// The image inside stays silent: the link carries the name.
		await expect(home.locator('img.brand-mark')).toHaveAttribute('alt', '');
		// The brand panel's mark is decoration, not a duplicate link.
		await expect(page.locator('aside.panel a')).toHaveCount(0);

		await home.click();
		await expect(page).toHaveURL(`${NO_TENANT_BASE}/`);
	});
});

test.describe('on a workspace subdomain', () => {
	test.use({ baseURL: ACME_BASE });

	test('the mark is not a link: "/" there only leads back to sign-in', async ({ page }) => {
		await page.goto('/login/forgot-password');
		await expect(page.locator('.auth-card img.brand-mark')).toBeVisible();
		await expect(page.getByRole('link', { name: 'FeohLedger home' })).toHaveCount(0);
		await expect(page.locator('.auth-card .head a')).toHaveCount(0);
	});
});

test.describe('on a vanity (white-label) host', () => {
	test.use({ baseURL: VANITY_ORIGIN });

	test('the mark is not a link to FeohLedger marketing', async ({ page }) => {
		await page.goto('/login/forgot-password');
		await expect(page.locator('.auth-card img.brand-mark')).toBeVisible();
		await expect(page.getByRole('link', { name: 'FeohLedger home' })).toHaveCount(0);
	});
});

/**
 * The rest of the pre-auth pages render their form-column mark through the
 * same component (issue #635 finding 31b): sign-in, its MFA step, and the
 * signup verification page all used a plain `BrandMark`.
 */
test.describe('the other pre-auth pages, on the apex', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	const homeLink = (page: Page) =>
		page.getByRole('link', { name: 'FeohLedger home' });

	test('sign-in: the mark is the named link home', async ({ page }) => {
		await page.goto('/login');
		await expect(homeLink(page)).toHaveCount(1);
		await expect(homeLink(page)).toHaveAttribute('href', '/');
		await expect(homeLink(page).locator('img.brand-mark')).toHaveAttribute('alt', '');
	});

	test('verify: the mark is the named link home', async ({ page }) => {
		// No token: the page's error state, which still carries the mark.
		await page.goto('/verify');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(homeLink(page)).toHaveCount(1);
		await expect(homeLink(page)).toHaveAttribute('href', '/');
	});

});

test.describe('the other pre-auth pages, on a workspace subdomain', () => {
	test.use({ baseURL: ACME_BASE });

	test('sign-in: the mark is not a link', async ({ page }) => {
		await page.goto('/login');
		await expect(page.locator('form.login-form img.brand-mark')).toBeVisible();
		await expect(page.getByRole('link', { name: 'FeohLedger home' })).toHaveCount(0);
	});
});
