import { expect, test } from '../fixtures/helpers';

// Signed out: a supplier who has forgotten their password cannot sign in.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * /portal/forgot-password — a supplier asks for their own reset link
 * (`POST /api/portal/auth/forgot-password`), which lands on
 * `/portal/reset-password`.
 *
 * This proves the page is reachable from the portal sign-in and wired to the
 * right endpoint and states. The request -> email -> redeem round trip, the
 * enumeration resistance, both budgets and the tenant binding are pinned
 * against real Postgres + Redis in `backend/tests/test_portal_forgot_password.py`.
 */
test.describe('/portal/forgot-password', () => {
	test('is reachable from the portal sign-in, signed out', async ({ page }) => {
		await page.goto('/portal/login');
		const link = page.getByRole('link', { name: 'Forgot password?' });
		await expect(link).toHaveAttribute('href', '/portal/forgot-password');
		await link.click();

		// Not bounced back to /portal/login, and not the employee page.
		await expect(page).toHaveURL(/\/portal\/forgot-password$/);
		await expect(page.getByRole('heading', { name: 'Reset your password' })).toBeVisible();
	});

	test('posts the address to the portal endpoint and focuses the generic confirmation', async ({
		page
	}) => {
		const address = `nobody-${Date.now()}@nowhere.example`;
		await page.goto('/portal/forgot-password');
		await page.locator('input[type="email"]').fill(address);

		const posted = page.waitForRequest(
			(r) => new URL(r.url()).pathname === '/api/portal/auth/forgot-password'
		);
		await page.getByRole('button', { name: 'Send reset link' }).click();
		expect((await posted).postDataJSON()).toEqual({ email: address });

		// The same copy whether or not the address matched a supplier account.
		const success = page.getByText(/we've sent a link to reset your password/i);
		await expect(success).toBeVisible();
		await expect(success).toBeFocused();
		await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute(
			'href',
			'/portal/login'
		);
	});
});
