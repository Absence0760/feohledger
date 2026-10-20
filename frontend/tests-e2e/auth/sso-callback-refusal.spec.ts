import { expect, test } from '../fixtures/helpers';

// Start unauthenticated — the SSO / SAML callback pages run before any session.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * An SSO sign-in whose IdP asserted an address that already has an account in
 * another workspace (docs/decisions.md §292). It used to be a bare 500 from the
 * UNIQUE on `users.email`; the backend now answers the OIDC callback, and the
 * SAML bridge's code exchange, with a coded 403 `sso_email_in_other_workspace`.
 *
 * Stubbed on exact pathnames: a real round trip needs an IdP and a second
 * workspace holding the address — the backend half of that is covered against
 * real Postgres in `backend/tests/test_email_identity_case.py` and
 * `backend/tests/test_saml_acs_integration.py`. What is under test here is that
 * each callback page states the refusal in words, from its code, with no link
 * to the other workspace and no session left behind.
 */

const DETAIL = {
	code: 'sso_email_in_other_workspace',
	// Deliberately not the English the page renders: the page must state the
	// refusal from its CODE, not echo the server's fallback.
	message: 'server fallback text',
	params: {}
};
const STATED =
	"This email address already belongs to an account in another workspace, so it can't sign in here.";

test.describe('an SSO address held by another workspace', () => {
	test('the OIDC callback page states the refusal and signs nobody in', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/auth/sso/callback',
			(route) => route.fulfill({ status: 403, json: { detail: DETAIL } })
		);
		await page.goto('/login/sso-callback?code=stub-code&state=stub-state');

		const error = page.getByRole('alert');
		await expect(error).toContainText(STATED);
		await expect(error).not.toContainText('server fallback text');
		await expect(page.getByRole('heading', { name: 'Sign-in failed' })).toBeVisible();
		await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute(
			'href',
			'/login'
		);
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
	});

	test('the SAML bridge states the refusal its handoff code carries', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/auth/saml/exchange',
			(route) => route.fulfill({ status: 403, json: { detail: DETAIL } })
		);
		await page.goto('/login/saml-callback?code=stub-handoff');

		const error = page.getByRole('alert');
		await expect(error).toContainText(STATED);
		await expect(error).not.toContainText('server fallback text');
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
	});
});
