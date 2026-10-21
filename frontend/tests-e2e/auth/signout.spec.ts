import { expect, signInAndWait, signOut, test } from '../fixtures/helpers';

// Start unauthenticated — this spec tests the logout flow, which
// blocklists the JWT server-side. Reusing the worker's storage-state
// admin would poison subsequent tests in the file because the cached
// JWT is now invalidated in Redis. Each test signs in fresh instead.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Sign-out flow + post-logout protection. The two assertions together
 * are the contract: "Log Out clears the session, and the cleared
 * session means a re-visit to a protected route bounces to /login."
 */

test.describe('sign out', () => {
	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('Log Out clears the session and lands on /login', async ({ page }) => {
		await signOut(page);

		// auth_token is the localStorage key cleared by auth.logout().
		// Reaching this assertion proves the sidebar's profile-popover
		// path executed clearToken() and rerouted us.
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		expect(token).toBeNull();
	});

	test('after logout, /invoices bounces back to /login', async ({ page }) => {
		await signOut(page);

		await page.goto('/invoices');
		// The auth wall carries the page as ?next= (post-login-redirect.spec.ts).
		await page.waitForURL((url) => url.pathname === '/login', { timeout: 5_000 });
		expect(new URL(page.url()).searchParams.get('next')).toBe('/invoices');
	});
});
