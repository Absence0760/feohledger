import { TENANT_ROOT_URL, escapeRegExp, expect, test } from '../fixtures/helpers';

/**
 * The login page's dev-only "Dev accounts" panel (`src/lib/dev/`).
 *
 * Under `vite dev` (the local default) it lists the worker tenant's seeded
 * accounts and a click fills the form. Under `vite preview` — the built bundle,
 * which is what CI's e2e shards serve (`FEOH_E2E_USE_PREVIEW=true`) — it must
 * not exist at all: `import.meta.env.DEV` is compiled to `false` there. That is
 * the runtime half of `scripts/check_dev_accounts_stripped.mjs`, which searches
 * the build's files; both modes assert something, so neither run is a no-op.
 */

const BUILT = process.env.FEOH_E2E_USE_PREVIEW === 'true';

test.describe('login — dev accounts panel', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test(
		BUILT
			? 'the built bundle renders no dev accounts panel'
			: 'clicking a seeded account fills the form, and it signs in',
		async ({ page, tenantSlug, tenantClerk }) => {
			await page.goto('/login');
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			const panel = page.locator('[data-dev-accounts]');

			if (BUILT) {
				// The form rendered (the heading above), so absence is a fact,
				// not a page that never loaded.
				await expect(page.locator('input[type="password"]')).toBeVisible();
				await expect(panel).toHaveCount(0);
				return;
			}

			await expect(panel).toBeVisible();
			await expect(panel).toHaveAccessibleName(/.+/);
			// One button per seeded role for an e2e worker tenant.
			await expect(panel.getByRole('button')).toHaveCount(4);

			await panel.getByRole('button', { name: new RegExp(escapeRegExp(tenantClerk.email)) }).click();
			await expect(page.locator('input[type="email"]')).toHaveValue(tenantClerk.email);
			await expect(page.locator('input[type="password"]')).toHaveValue(tenantClerk.password);
			expect(tenantClerk.email).toContain(tenantSlug);

			// The filled credentials are the seeded ones: they actually sign in.
			await page.locator('form button[type="submit"]').click();
			await page.waitForURL(TENANT_ROOT_URL);
		}
	);
});
