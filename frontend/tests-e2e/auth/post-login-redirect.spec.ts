import type { Page } from '@playwright/test';

import { acceptConsent, expect, TENANT_ROOT_URL, test } from '../fixtures/helpers';

// Start unauthenticated — the journey begins at the auth wall.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Sign-in returns the user to the page they were opening (issue #635,
 * finding 7). A notification email links to /invoices/{id}; before this the
 * auth wall sent the visitor to a bare /login and every sign-in path ended in
 * `goto('/')`, so the link's whole point was lost.
 *
 * The destination rides as `?next=` and is honoured only as a same-origin
 * relative path — `#lib/utils/postLoginRedirect.ts`, unit-tested for every
 * shape it refuses. These specs pin the wiring end to end.
 */

/** Fill and submit the sign-in form already on screen (unlike `signIn`,
 *  which navigates to a bare /login first and would drop `?next=`). */
async function submitSignIn(page: Page, creds: { email: string; password: string }) {
	await page.locator('input[type="email"]').fill(creds.email);
	await page.locator('input[type="password"]').fill(creds.password);
	await page.locator('form button[type="submit"]').click();
}

test.describe('sign-in returns to the page asked for', () => {
	test.beforeEach(async ({ page }) => {
		await acceptConsent(page);
	});

	test('the auth wall carries the path and query into ?next=, and sign-in lands there', async ({
		page,
		tenantAdmin: creds
	}) => {
		await page.goto('/invoices?status=pending');

		await page.waitForURL((url) => url.pathname === '/login');
		expect(new URL(page.url()).searchParams.get('next')).toBe('/invoices?status=pending');

		await submitSignIn(page, creds);

		await page.waitForURL((url) => url.pathname === '/invoices');
		expect(new URL(page.url()).search).toBe('?status=pending');
		await expect(page.locator('#main-content')).toBeVisible();
	});

	test('an off-site ?next= is ignored and sign-in lands on the dashboard', async ({
		page,
		tenantAdmin: creds
	}) => {
		await page.goto('/login?next=' + encodeURIComponent('//evil.example/phish'));
		await submitSignIn(page, creds);
		await page.waitForURL(TENANT_ROOT_URL);
	});

	test('?next= survives the MFA step and its Back to sign in link', async ({ page }) => {
		// MFA is off in the default e2e backend, so seed a challenge the way
		// /login would (mfa.spec.ts § challenge UI does the same).
		await page.goto('/login');
		await page.evaluate(() =>
			sessionStorage.setItem(
				'mfa_challenge',
				JSON.stringify({
					mfa_required: true,
					mfa_challenge_token: 'e2e-fake-challenge-token',
					methods: ['totp', 'email'],
					must_enroll: false
				})
			)
		);
		await page.goto('/login/mfa?next=' + encodeURIComponent('/invoices/abc'));

		const back = page.getByRole('link', { name: 'Back to sign in' });
		await expect(back).toHaveAttribute('href', '/login?next=%2Finvoices%2Fabc');
		await back.click();

		await page.waitForURL((url) => url.pathname === '/login');
		expect(new URL(page.url()).searchParams.get('next')).toBe('/invoices/abc');
		// The abandoned challenge is cleared, so /login/mfa no longer resumes it.
		expect(await page.evaluate(() => sessionStorage.getItem('mfa_challenge'))).toBeNull();
	});
});
