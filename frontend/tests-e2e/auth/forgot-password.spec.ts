import { expect, test } from '../fixtures/helpers';

// Start unauthenticated — these specs test the forgot/reset-password UI,
// same reason tests-e2e/auth/login.spec.ts opts out of the preloaded admin
// storage state.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Forgot/reset password UI (`/login/forgot-password`, `/login/reset-password`).
 *
 * Deliberately does NOT exercise the full request -> email -> redeem round
 * trip here — that would either mutate a real seeded tenant credential (the
 * `demo+<role>@<slug>.localhost` accounts other specs and workers depend on
 * staying at their seed password) or require intercepting the console email
 * adapter's output, neither of which belongs in a UI smoke spec. The full
 * round trip (including the seeded-password mutation risk) is covered
 * end-to-end against real Postgres + Redis in
 * `backend/tests/test_forgot_reset_password.py`, which mints its own
 * throwaway control-plane user precisely to avoid that hazard. This spec
 * only proves the UI is wired to the right endpoints and states.
 */

test.describe('/login/forgot-password', () => {
	test('is reachable from the login page', async ({ page }) => {
		await page.goto('/login');

		const link = page.getByRole('link', { name: /Forgot password/i });
		await expect(link).toBeVisible();
		await link.click();

		await expect(page).toHaveURL(/\/login\/forgot-password$/);
		await expect(page.getByRole('heading', { name: /Reset your password/i })).toBeVisible();
	});

	test('shows the generic success message regardless of whether the email exists', async ({
		page
	}) => {
		await page.goto('/login/forgot-password');

		await page.locator('input[type="email"]').fill(`nobody-${Date.now()}@nowhere.example`);
		await page.getByRole('button', { name: /Send reset link/i }).click();

		// Same success copy whether or not the address matched an account —
		// the enumeration-resistance contract backend/app/api/auth.py
		// ::forgot_password documents.
		const success = page.getByText(/we've sent a link to reset your password/i);
		await expect(success).toBeVisible();
		await expect(page.getByRole('link', { name: /Back to sign in/i })).toBeVisible();
		// The submit button that held focus went with the form. Focus moves onto
		// the confirmation, which is how a screen reader hears it at all — left
		// alone it fell to <body> and the success was silent (WCAG 2.4.3, 4.1.3).
		await expect(success).toBeFocused();
	});
});

test.describe('/login/reset-password', () => {
	test('flags a missing token instead of rendering a form', async ({ page }) => {
		await page.goto('/login/reset-password');

		// Assert the rendered no-token state FIRST: it is the auto-waiting
		// signal that the route mounted. The absence check below only means
		// something once something is on the page.
		await expect(page.getByText(/missing its token/i)).toBeVisible();
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
	});

	test('a bogus token is refused before any password is typed', async ({ page }) => {
		await page.goto('/login/reset-password?token=not-a-real-token');

		// The page reads the token first (`/auth/reset-password/check`, which
		// never consumes it), so an unknown, used or expired link explains
		// itself up front and offers a fresh one instead of a form.
		await expect(page.getByText(/invalid or has expired/i)).toBeVisible();
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
		await expect(page.getByRole('link', { name: /Reset your password/i })).toHaveAttribute(
			'href',
			'/login/forgot-password'
		);
	});

	test('a used set-password link names Forgot password as the way back in', async ({ page }) => {
		await page.goto('/login/reset-password?token=not-a-real-token&mode=set');
		await expect(page.getByRole('heading', { name: 'Set your password' })).toBeVisible();
		await expect(page.getByText(/set-password links work once and expire/i)).toBeVisible();
		await expect(page.getByRole('link', { name: /Reset your password/i })).toBeVisible();
	});

	/** Stub the non-consuming read on its exact pathname, as the account `email`. */
	async function stubCheck(page: import('@playwright/test').Page, email: string) {
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password/check',
			(route) => route.fulfill({ json: { email } })
		);
	}

	test('moves focus onto the confirmation once the password is reset', async ({ page }) => {
		// Stubbed on exact pathnames: a real redemption needs a minted token
		// and would change a seeded credential (see the file header). What is
		// under test is only the page's reaction to a 2xx.
		await stubCheck(page, 'someone@acme.example');
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) => route.fulfill({ json: { message: 'ok' } })
		);
		await page.goto('/login/reset-password?token=stubbed-token');

		await page.locator('input[type="password"]').first().fill('BrandNewPassw0rd!42');
		await page.locator('input[type="password"]').nth(1).fill('BrandNewPassw0rd!42');
		await page.getByRole('button', { name: /Reset password/i }).click();

		// Same defect as the forgot-password confirmation: the form (and the
		// focused submit button) is replaced, so focus has to be put somewhere
		// that announces the outcome rather than dropping to <body>.
		const success = page.getByText(/your password has been reset/i);
		await expect(success).toBeVisible();
		await expect(success).toBeFocused();
	});

	test('a mismatched confirmation is named beside the field on submit, and nothing is sent', async ({
		page
	}) => {
		// #635 #14: Submit used to stay disabled until the two matched, so this
		// message could never appear. Now Submit is enabled and says why.
		await stubCheck(page, 'someone@acme.example');
		let posted = false;
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) => {
				posted = true;
				return route.fulfill({ json: { message: 'ok' } });
			}
		);
		await page.goto('/login/reset-password?token=stubbed-token');

		const newPw = page.getByLabel('New password', { exact: true });
		const confirm = page.getByLabel('Confirm new password', { exact: true });
		await newPw.fill('BrandNewPassw0rd!42');
		await confirm.fill('BrandNewPassw0rd!43');
		const submit = page.getByRole('button', { name: /Reset password/i });
		await expect(submit).toBeEnabled();
		await submit.click();

		await expect(confirm).toBeFocused();
		await expect(confirm).toHaveAttribute('aria-invalid', 'true');
		await expect(confirm).toHaveAccessibleDescription('Passwords do not match.');
		await expect(newPw).not.toHaveAttribute('aria-invalid', 'true');
		// Still the form: the refusal was the page's own, not the server's.
		await expect(page.locator('input[autocomplete="new-password"]')).toHaveCount(2);
		expect(posted).toBe(false);
	});

	test('opening the link (again and again) never redeems it — only the submit does', async ({
		page
	}) => {
		// A corporate mail scanner fetches every link before the user does, and
		// a user reloads. Neither may spend the single-use token.
		const calls = { check: 0, redeem: 0 };
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password/check',
			(route) => {
				calls.check += 1;
				return route.fulfill({ json: { email: 'scan@acme.example' } });
			}
		);
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) => {
				calls.redeem += 1;
				return route.fulfill({ json: { message: 'ok' } });
			}
		);
		await page.goto('/login/reset-password?token=stubbed-token&mode=set');
		await expect(page.getByRole('button', { name: 'Set password' })).toBeVisible();
		await page.reload();
		await expect(page.getByRole('button', { name: 'Set password' })).toBeVisible();
		expect(calls).toEqual({ check: 2, redeem: 0 });

		await page.locator('input[type="password"]').first().fill('MyOwnChoice!2026');
		await page.locator('input[type="password"]').nth(1).fill('MyOwnChoice!2026');
		await page.getByRole('button', { name: 'Set password' }).click();
		await expect(page.getByText('Your password is set. You can now sign in.')).toBeVisible();
		expect(calls.redeem).toBe(1);
	});

	test('a failed check is not reported as a dead link, and can be retried', async ({ page }) => {
		let fail = true;
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password/check',
			(route) =>
				fail
					? route.fulfill({ status: 429, json: { detail: 'Too many requests. Slow down.' } })
					: route.fulfill({ json: { email: 'retry@acme.example' } })
		);
		await page.goto('/login/reset-password?token=stubbed-token');
		await expect(page.getByText('Too many requests. Slow down.')).toBeVisible();
		await expect(page.getByText(/invalid or has expired/i)).toHaveCount(0);

		fail = false;
		await page.getByRole('button', { name: 'Try again' }).click();
		await expect(page.locator('form input[autocomplete="username"]')).toHaveValue(
			'retry@acme.example'
		);
	});

	test('a link that dies between loading and submitting switches to the request-a-new-link screen', async ({
		page
	}) => {
		// The load-time check passed, then the token was spent (another tab, the
		// welcome-email copy of a set-password link) or expired before submit.
		// The redemption answers the same opaque 400 the check would have; the
		// page used to banner it over a form that could never succeed.
		await stubCheck(page, 'late@acme.example');
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) =>
				route.fulfill({ status: 400, json: { detail: 'Invalid or expired reset link.' } })
		);
		await page.goto('/login/reset-password?token=stubbed-token&mode=set');

		await page.locator('input[type="password"]').first().fill('BrandNewPassw0rd!42');
		await page.locator('input[type="password"]').nth(1).fill('BrandNewPassw0rd!42');
		await page.getByRole('button', { name: 'Set password' }).click();

		// The dead-link screen, focused so it is announced (the focused submit
		// button went with the form) — with the set-mode hint and the way on.
		// One focus target holds both the reason and the set-mode next step, and
		// it is not also an alert (that would be read twice).
		const focused = page.locator(':focus');
		await expect(focused).toContainText(
			'This reset link is invalid or has expired. Please request a new one.'
		);
		await expect(focused).toContainText(/set-password links work once and expire/i);
		await expect(focused).not.toHaveAttribute('role', 'alert');
		await expect(page.getByRole('link', { name: /Reset your password/i })).toHaveAttribute(
			'href',
			'/login/forgot-password'
		);
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
		// Not ALSO the server's English in the banner over it.
		await expect(page.getByText('Invalid or expired reset link.', { exact: true })).toHaveCount(0);
	});

	test('a refusal that is not about the link keeps the form, with the reason', async ({
		page
	}) => {
		await stubCheck(page, 'weak@acme.example');
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) =>
				route.fulfill({ status: 429, json: { detail: 'Too many requests. Slow down.' } })
		);
		await page.goto('/login/reset-password?token=stubbed-token');

		await page.locator('input[type="password"]').first().fill('BrandNewPassw0rd!42');
		await page.locator('input[type="password"]').nth(1).fill('BrandNewPassw0rd!42');
		await page.getByRole('button', { name: /Reset password/i }).click();

		await expect(page.getByText('Too many requests. Slow down.')).toBeVisible();
		await expect(page.locator('input[type="password"]')).toHaveCount(2);
		await expect(page.getByText(/invalid or has expired/i)).toHaveCount(0);
	});

	test('a new password the server refuses is named beside the field, not in the banner', async ({
		page
	}) => {
		await stubCheck(page, 'weak@acme.example');
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			(route) =>
				route.fulfill({ status: 422, json: { detail: 'Password must contain a digit.' } })
		);
		await page.goto('/login/reset-password?token=stubbed-token');

		const newPw = page.getByLabel('New password', { exact: true });
		await newPw.fill('BrandNewPassw0rd!42');
		await page.getByLabel('Confirm new password', { exact: true }).fill('BrandNewPassw0rd!42');
		await page.getByRole('button', { name: /Reset password/i }).click();

		await expect(newPw).toBeFocused();
		await expect(newPw).toHaveAttribute('aria-invalid', 'true');
		await expect(newPw).toHaveAccessibleDescription(/^Password must contain a digit\./);
		// Editing the field clears the server's verdict on the old value.
		await newPw.fill('BrandNewPassw0rd!43');
		await expect(newPw).not.toHaveAttribute('aria-invalid', 'true');
	});

	test('set mode: a password manager can save the new password under the account', async ({
		page
	}) => {
		const email = 'new-admin+ap@acme.example';
		await stubCheck(page, email);
		const redeemed: Array<{ token: string; new_password: string }> = [];
		await page.route(
			(url) => url.pathname === '/api/auth/reset-password',
			async (route) => {
				redeemed.push(route.request().postDataJSON());
				await route.fulfill({ json: { message: 'ok' } });
			}
		);
		await page.goto('/login/reset-password?token=stubbed-token&mode=set');

		await expect(page.getByRole('heading', { name: 'Set your password' })).toBeVisible();
		// The username a password manager files the password under: the account
		// email, in the same form, hidden and out of the tab order.
		const username = page.locator('form input[autocomplete="username"]');
		await expect(username).toHaveValue(email);
		await expect(username).toBeHidden();
		await expect(page.locator('form input[autocomplete="new-password"]')).toHaveCount(2);

		await page.locator('input[type="password"]').first().fill('MyOwnChoice!2026');
		await page.locator('input[type="password"]').nth(1).fill('MyOwnChoice!2026');
		await page.getByRole('button', { name: 'Set password' }).click();

		await expect(page.getByText('Your password is set. You can now sign in.')).toBeFocused();
		expect(redeemed).toEqual([{ token: 'stubbed-token', new_password: 'MyOwnChoice!2026' }]);
		// Onward to sign in with the address already filled in.
		await page.getByRole('link', { name: 'Go to sign in' }).click();
		await expect(page).toHaveURL(/\/login\?email=new-admin%2Bap%40acme\.example$/);
		await expect(page.locator('input[type="email"]')).toHaveValue(email);
	});
});
