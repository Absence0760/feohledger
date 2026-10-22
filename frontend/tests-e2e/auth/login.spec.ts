import type { Route } from '@playwright/test';
import { expect, NO_TENANT_BASE, signIn, TENANT_ROOT_URL, test } from '../fixtures/helpers';

// Start unauthenticated — this spec tests the login UI itself.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Login surface — anonymous-visitor + happy-path smoke.
 *
 * baseURL is the current worker's per-tenant origin (the seeded
 * `e2e<N>` tenant). `localhost:7777` (no subdomain) is exercised
 * separately because it routes to the no-tenant Landing component,
 * not the login form.
 */

test.describe('/login — decorative art', () => {
	test('the sign-in panel art cannot be dragged off the page', async ({ page }) => {
		// Same defect and same reasoning as the landing page's copy of this
		// render (see `marketing/landing.spec.ts`): an `<img>` is draggable by
		// default, so the illustration peeled away under the cursor on the first
		// screen a customer sees. Pinned on BOTH surfaces because they are two
		// separate components that happen to share one asset — fixing one says
		// nothing about the other.
		await page.goto('/login');
		const art = page.locator('.panel-art');
		await expect(art).toHaveAttribute('draggable', 'false');
		await expect(art).toHaveCSS('user-select', 'none');
		expect(await art.evaluate((el: HTMLImageElement) => el.draggable)).toBe(false);
	});
});

test.describe('/login', () => {
	test('renders the sign-in form for an anon visitor', async ({ page }) => {
		await page.goto('/login');

		await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(page.locator('input[type="password"]')).toBeVisible();
		await expect(page.getByRole('button', { name: /Sign in/ })).toBeVisible();
	});

	test('?email= prefills the email field, and only with a plausible address', async ({ page }) => {
		// The signup welcome email's sign-in link and the reset page's "go to
		// sign in" both carry the account's address (decisions §283).
		await page.goto('/login?email=' + encodeURIComponent('first+ap@acme.example'));
		const field = page.locator('input[type="email"]');
		await expect(field).toHaveValue('first+ap@acme.example');
		// Prefilled, not committed: the visitor can still change it, and the
		// password field is left for them to reach themselves.
		await expect(field).not.toBeFocused();
		await expect(page.locator('input[type="password"]')).toHaveValue('');

		// A crafted value is dropped rather than shown in the field.
		await page.goto('/login?email=' + encodeURIComponent('your account is locked, call us'));
		await expect(page.getByRole('button', { name: /Sign in/ })).toBeVisible();
		await expect(field).toHaveValue('');
	});

	test('rejects an unknown email/password and stays on /login', async ({ page }) => {
		await signIn(page, { email: 'noone@nowhere.test', password: 'wrong-password' });

		// Stay on /login (the form re-renders with an error banner).
		// We don't assert on the error copy — backend wording may
		// shift; the URL behaviour is the security contract and the
		// banner is the UX contract.
		await expect(page).toHaveURL(/\/login/);
		await expect(page.locator('.error')).toBeVisible();
	});

	test('seeded admin signs in and lands on the app shell', async ({ page }) => {
		await signIn(page);

		// Successful login calls `goto('/')`. The exact landing chrome
		// is the dashboard for an authenticated tenant user — assert on
		// the URL transition, not on dashboard internals (those evolve).
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
	});
});

/**
 * What the page says it is (issue #635 finding 34): the heading names the task
 * (WCAG 2.4.6) rather than repeating the product, the tab title is in the
 * reader's language, and the page says WHICH workspace it signs in to — by the
 * host the visitor is on, which no public endpoint has to disclose.
 */
test.describe('/login — heading, title and workspace', () => {
	test('says "Sign in", in the tab title too, and names the workspace host', async ({ page }) => {
		await page.goto('/login');
		await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
		await expect(page).toHaveTitle('Sign in — FeohLedger');
		await expect(page.locator('form.login-form .subtitle')).toHaveText(
			`Workspace: ${new URL(page.url()).host}`
		);
	});

	test('the heading and tab title follow the reader\'s language', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
		await page.goto('/login');
		await expect(page.getByRole('heading', { level: 1, name: 'Anmelden' })).toBeVisible();
		await expect(page).toHaveTitle('Anmelden — FeohLedger');
	});

	test('the MFA step, which sets no title of its own, keeps the sign-in title', async ({ page }) => {
		await page.goto('/login');
		await page.evaluate(() =>
			sessionStorage.setItem(
				'mfa_challenge',
				JSON.stringify({
					mfa_required: true,
					mfa_challenge_token: 'e2e-fake-challenge-token',
					methods: ['totp'],
					must_enroll: false
				})
			)
		);
		await page.goto('/login/mfa');
		await expect(page.getByRole('heading', { name: 'Two-factor verification' })).toBeVisible();
		await expect(page).toHaveTitle('Sign in — FeohLedger');
	});
});

/**
 * The two SSO config answers decide whether there is a password form at all,
 * so the form waits for both (issue #635 finding 33). It used to render first
 * and be pulled away when an SSO-only answer arrived — after the SECOND of two
 * calls made one after the other.
 */
test.describe('/login — SSO config', () => {
	test('an SSO-only workspace never shows the password form, and both configs are asked for at once', async ({
		page
	}) => {
		const asked: string[] = [];
		let release!: () => void;
		const answered = new Promise<void>((resolve) => (release = resolve));
		const hold = (name: string, json: unknown) => async (route: Route) => {
			asked.push(name);
			await answered;
			await route.fulfill({ json });
		};
		await page.route(
			(url) => url.pathname === '/api/auth/sso/config',
			hold('oidc', { enabled: true, provider: 'okta', sso_only: true })
		);
		await page.route(
			(url) => url.pathname === '/api/auth/saml/config',
			hold('saml', { enabled: false, provider: null })
		);
		// Records a password field EVER being attached, however briefly.
		await page.addInitScript(() => {
			const w = window as unknown as { __sawPassword: boolean };
			w.__sawPassword = false;
			new MutationObserver(() => {
				if (document.querySelector('input[type="password"]')) w.__sawPassword = true;
			}).observe(document, { subtree: true, childList: true });
		});

		await page.goto('/login');
		await expect(page.getByRole('status').filter({ hasText: 'Loading sign-in options' })).toBeVisible();
		// Both requests are in flight before either is answered.
		await expect.poll(() => [...asked].sort()).toEqual(['oidc', 'saml']);
		release();

		await expect(page.locator('.sso-only-note')).toBeVisible();
		await expect(page.locator('button.sso-btn')).toBeVisible();
		await expect(page.getByText('Loading sign-in options')).toHaveCount(0);
		expect(await page.evaluate(() => (window as unknown as { __sawPassword: boolean }).__sawPassword)).toBe(
			false
		);
	});

	test('a config call that fails still leaves password sign-in', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/auth/sso/config' || url.pathname === '/api/auth/saml/config',
			(route) => route.fulfill({ status: 500, json: { detail: 'boom' } })
		);
		await page.goto('/login');
		await expect(page.locator('input[type="password"]')).toBeVisible();
		await expect(page.locator('button.sso-btn')).toHaveCount(0);
	});
});

test.describe('/ (no-tenant landing)', () => {
	// Override the tenant subdomain in the baseURL — the marketing
	// landing only renders when the request has no tenant slug.
	test.use({ baseURL: NO_TENANT_BASE });

	test('shows the marketing landing, not the login form', async ({ page }) => {
		await page.goto('/');

		// Anchor on the marketing hero first. Without it the absence check
		// below would pass on a page that had rendered nothing at all.
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

		// Login form should NOT be present on the no-tenant route.
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
	});
});
