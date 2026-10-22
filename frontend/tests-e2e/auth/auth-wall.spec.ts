import { expect, test } from '../fixtures/helpers';

// Start unauthenticated — this spec tests the auth-wall redirect for anon visitors.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Auth wall — every tenant-scoped route under the app shell must
 * redirect an anonymous visitor to /login. Implemented in
 * `frontend/src/routes/+layout.svelte` (lines 38-39): if the visitor
 * has no token and the path doesn't start with `/login`, goto('/login').
 *
 * Parametrised over the routes the sidebar's nav covers so a regression
 * on any single one trips the suite.
 */

const PROTECTED_ROUTES = [
	'/',
	'/invoices',
	'/payments',
	'/vendors',
	'/exceptions',
	'/workflows',
	'/audit',
	'/organization',
	'/admin',
	'/profile'
];

test.describe('auth wall (acme tenant, anon visitor)', () => {
	for (const path of PROTECTED_ROUTES) {
		test(`${path} → /login`, async ({ page }) => {
			await page.goto(path);
			// `goto('/login')` runs in onMount once auth state resolves.
			// Wait for the redirect rather than asserting on the initial
			// URL — Svelte navigation isn't instant.
			await page.waitForURL((url) => url.pathname === '/login', { timeout: 5_000 });
			// The page asked for rides along as ?next= so sign-in returns
			// there (post-login-redirect.spec.ts); the dashboard is the
			// default and is not carried.
			expect(new URL(page.url()).searchParams.get('next')).toBe(path === '/' ? null : path);
			await expect(page.locator('input[type="password"]')).toBeVisible();
		});
	}
});
