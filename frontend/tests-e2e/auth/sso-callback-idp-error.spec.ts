import { expectNoA11yViolations } from '../a11y/axe-helper';
import { expect, test } from '../fixtures/helpers';

// The callback pages run before any session exists.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * An identity provider that refuses or abandons a sign-in sends the browser
 * back with `?error=<code>` (RFC 6749 §4.1.2.1). The OIDC and SAML callback
 * pages printed that code as the whole message — "Identity provider error:
 * access_denied" — in a card unlike every other sign-in screen (issue #635
 * finding 36). They are one component now (`AuthCallback`), inside the same
 * shell as sign-in, and say what happened in words; the raw code stays as
 * small support detail, and only when it looks like a code.
 */

for (const path of ['/login/sso-callback', '/login/saml-callback']) {
	test.describe(path, () => {
		test('a cancelled sign-in is stated in words, with the code as detail', async ({ page }) => {
			let exchanged = false;
			await page.route(
				(url) => url.pathname === '/api/auth/sso/callback' || url.pathname === '/api/auth/saml/exchange',
				(route) => {
					exchanged = true;
					return route.fulfill({ status: 500, json: { detail: 'should not be called' } });
				}
			);
			await page.goto(`${path}?error=access_denied`);

			await expect(page.getByRole('heading', { level: 1, name: 'Sign-in failed' })).toBeVisible();
			await expect(page.getByRole('alert')).toHaveText(
				"Sign-in was cancelled, or your identity provider didn't allow you into this app."
			);
			await expect(page.getByText('Code from your identity provider: access_denied')).toBeVisible();
			await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login');
			// The same shell as every other sign-in screen.
			await expect(page.locator('.auth-card img.brand-mark')).toBeVisible();
			expect(exchanged).toBe(false);
		});

		test('an unknown code gets the generic sentence', async ({ page }) => {
			await page.goto(`${path}?error=unauthorized_client`);
			await expect(page.getByRole('alert')).toHaveText(
				"Your identity provider couldn't complete the sign-in. Try again, or ask your administrator."
			);
			await expect(page.getByText('Code from your identity provider: unauthorized_client')).toBeVisible();
		});

		test('text someone wrote into the link is never echoed', async ({ page }) => {
			const planted = 'Your account is locked. Call 1-800-555-0100';
			await page.goto(`${path}?error=${encodeURIComponent(planted)}`);
			await expect(page.getByRole('alert')).toHaveText(
				"Your identity provider couldn't complete the sign-in. Try again, or ask your administrator."
			);
			await expect(page.getByText(/1-800-555-0100/)).toHaveCount(0);
		});

		test('reads in the visitor\'s language', async ({ page }) => {
			await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
			await page.goto(`${path}?error=temporarily_unavailable`);
			await expect(page.getByRole('alert')).toHaveText(
				'Ihr Identitätsanbieter ist vorübergehend nicht erreichbar. Versuchen Sie es in einigen Minuten erneut.'
			);
		});
	});
}

test('the destination stashed before the IdP round trip survives a refusal, for the way back', async ({
	page
}) => {
	await page.addInitScript(() => sessionStorage.setItem('auth_next', '/payments?tab=history'));
	await page.goto('/login/sso-callback?error=login_required');
	await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute(
		'href',
		'/login?next=%2Fpayments%3Ftab%3Dhistory'
	);
	// Taken, not left behind to steer a later sign-in in this tab.
	expect(await page.evaluate(() => sessionStorage.getItem('auth_next'))).toBeNull();
});

test('a callback URL missing what it needs says so, per protocol', async ({ page }) => {
	await page.goto('/login/sso-callback?code=only-a-code');
	await expect(page.getByRole('alert')).toHaveText('Missing code or state in the callback URL.');
	await page.goto('/login/saml-callback');
	await expect(page.getByRole('alert')).toHaveText('Missing code in the callback URL.');
});

for (const width of [1280, 375]) {
	test(`the refusal screen has no axe violations at ${width}px`, async ({ page }) => {
		await page.setViewportSize({ width, height: 900 });
		await page.goto('/login/sso-callback?error=access_denied');
		await expect(page.getByRole('heading', { level: 1, name: 'Sign-in failed' })).toBeVisible();
		await expectNoA11yViolations(page);
	});
}
