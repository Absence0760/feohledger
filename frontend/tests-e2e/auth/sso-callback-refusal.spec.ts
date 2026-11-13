import { expect, test } from '../fixtures/helpers';

// Start unauthenticated — the SSO / SAML callback pages run before any session.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * An SSO sign-in whose IdP asserted an address that already has an account in
 * another workspace (docs/decisions.md §291). It used to be a bare 500 from the
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

/**
 * Every other SAML ACS refusal lands on the bridge too (docs/decisions.md §359),
 * and the OIDC callback's identity refusals carry the same codes. Each page
 * must state the refusal from its code, never echo the server's fallback, and
 * leave a way back to sign-in. `saml_login_unverified` is the ONE sentence for
 * every response the ACS could not verify — a forged signature, a replay, an
 * unsolicited response — so the page can say no more than that.
 */
const IDENTITY_REFUSALS: [code: string, stated: string][] = [
	['sso_account_inactive', 'This account has been deactivated. Contact your administrator.'],
	['sso_email_domain_blocked', "Your email domain isn't allowed to sign in to this workspace."],
	['sso_email_unusable', "Your identity provider sent an email address that can't be used to sign in."]
];
const SAML_ONLY_REFUSALS: [code: string, status: number, stated: string][] = [
	['saml_login_unverified', 400, 'Your SAML sign-in could not be verified. Try signing in again.'],
	['saml_not_configured', 400, "SAML sign-in isn't set up for this workspace."]
];

test.describe('every other SSO refusal is stated from its code', () => {
	for (const [code, status, stated] of [
		...IDENTITY_REFUSALS.map(([c, s]) => [c, 403, s] as const),
		...SAML_ONLY_REFUSALS
	]) {
		test(`the SAML bridge states ${code}`, async ({ page }) => {
			await page.route(
				(url) => url.pathname === '/api/auth/saml/exchange',
				(route) =>
					route.fulfill({
						status,
						json: { detail: { code, message: 'server fallback text', params: {} } }
					})
			);
			await page.goto('/login/saml-callback?code=stub-handoff');

			const error = page.getByRole('alert');
			await expect(error).toContainText(stated);
			await expect(error).not.toContainText('server fallback text');
			await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute(
				'href',
				'/login'
			);
			expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
		});
	}

	for (const [code, stated] of IDENTITY_REFUSALS) {
		test(`the OIDC callback page states ${code}`, async ({ page }) => {
			await page.route(
				(url) => url.pathname === '/api/auth/sso/callback',
				(route) =>
					route.fulfill({
						status: 403,
						json: { detail: { code, message: 'server fallback text', params: {} } }
					})
			);
			await page.goto('/login/sso-callback?code=stub-code&state=stub-state');

			const error = page.getByRole('alert');
			await expect(error).toContainText(stated);
			await expect(error).not.toContainText('server fallback text');
			expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
		});
	}
});
