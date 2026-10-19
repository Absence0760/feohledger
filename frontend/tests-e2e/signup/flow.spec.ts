import type { Page } from '@playwright/test';

import { API_BASE, expect, NO_TENANT_BASE, TENANT_ROOT_URL, tenantBase, test } from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';

/**
 * Self-service signup — the full new-user journey, driven through the UI.
 *
 * Three regimes:
 *  1. Form validation + the "check your email" success state — runs anywhere
 *     (no email delivery needed; /start sends via whatever adapter is configured).
 *  2. The /verify page's error states (no token, invalid/expired token) — also
 *     runs anywhere; we drive it with bogus tokens.
 *  3. The full happy path landing → signup → verification email → /verify →
 *     "Set your password" (the same single-use link the welcome email carries;
 *     no password is ever emailed, decisions §282) → sign in with the email
 *     prefilled → dashboard. Gated on Mailpit (the real outbound sink); ALSO needs the
 *     backend on the `smtp` adapter pointed at Mailpit:
 *        pnpm mail:up && FEOH_EMAIL_PROVIDER=smtp FEOH_SMTP_PORT=1025 pnpm dev:backend
 *     If the backend is on `console` the verification email never arrives and
 *     the happy-path test fails loudly (a setup error, never a silent pass).
 *
 * Signup + verify render only on the root (no-tenant) origin, so these specs
 * pin baseURL to NO_TENANT_BASE and start unauthenticated.
 */

const MAILPIT = 'http://localhost:8025';

interface MailpitMessage {
	ID: string;
	Subject: string;
	To: Array<{ Address: string }>;
}

async function messagesTo(page: Page, email: string): Promise<MailpitMessage[]> {
	const resp = await page.request.get(`${MAILPIT}/api/v1/search`, {
		params: { query: `to:${email}` }
	});
	if (!resp.ok()) return [];
	return ((await resp.json()) as { messages: MailpitMessage[] }).messages;
}

async function messageText(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${MAILPIT}/api/v1/message/${id}`);
	return ((await resp.json()) as { Text: string }).Text;
}

/** Wait for a message with the given subject to land, return its plaintext body. */
async function waitForEmail(page: Page, email: string, subjectPrefix: string): Promise<string> {
	let id: string | null = null;
	await expect
		.poll(
			async () => {
				const msgs = await messagesTo(page, email);
				const hit = msgs.find((m) => m.Subject.startsWith(subjectPrefix));
				id = hit?.ID ?? null;
				return id;
			},
			{ timeout: 15_000 }
		)
		.not.toBeNull();
	return messageText(page, id!);
}

test.use({ storageState: { cookies: [], origins: [] } });

// The consent banner is position:fixed at the bottom of the viewport with
// z-index 10000. On the signup/verify pages (unauthenticated, empty storage
// state) the banner would overlap the submit button — on Ubuntu CI the form
// renders slightly taller than on macOS/Fedora due to font metrics, pushing
// the button centre below the banner's top edge and making Playwright's
// click() fail with a 30 s intercept timeout. Suppress the banner for all
// tests in this file by recording the consent choice before each navigation;
// no test here is verifying the consent banner itself.
test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 1. Signup form — validation + success state (no email delivery needed)
// ───────────────────────────────────────────────────────────────────────────

test.describe('/signup — form', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	async function fillForm(
		page: Page,
		{ company, slug, name, email }: { company: string; slug: string; name: string; email: string }
	) {
		await page.getByLabel('Company name').fill(company);
		await page.getByPlaceholder('acme').fill(slug);
		await page.getByLabel('Your name').fill(name);
		await page.getByLabel('Email').fill(email);
	}

	test('"what happens next" promises a set-password link, not a temporary password', async ({
		page
	}) => {
		await page.goto('/signup');
		await expect(page.getByText('Set your password and sign in')).toBeVisible();
		await expect(page.getByText(/temporary/i)).toHaveCount(0);
	});

	test('submitting a valid form shows the "check your email" state', async ({ page }) => {
		const slug = `e2eform${Date.now().toString().slice(-9)}`;
		await page.goto('/signup');

		await fillForm(page, {
			company: 'Form E2E Co',
			slug,
			name: 'Form Admin',
			email: `${slug}@example.com`
		});
		// Wait for the debounced slug-check to clear so submit is enabled.
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });

		await page.getByRole('button', { name: 'Send verification email' }).click();

		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible({
			timeout: 10_000
		});
		// The form is gone; next-step guidance is shown.
		await expect(page.getByText(/click the link in that email/i)).toBeVisible();
	});

	test('the confirmation takes focus, and "try again" returns to the filled form', async ({
		page
	}) => {
		const slug = `e2eretry${Date.now().toString().slice(-9)}`;
		const email = `${slug}@example.com`;
		await page.goto('/signup');

		await fillForm(page, { company: 'Retry Co', slug, name: 'Retry Admin', email });
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });
		await page.getByRole('button', { name: 'Send verification email' }).click();

		// The submit button that held focus is removed with the form. Focus must
		// land on the confirmation's heading rather than falling to <body>.
		const confirmation = page.getByRole('heading', { name: 'Check your email' });
		await expect(confirmation).toBeFocused({ timeout: 10_000 });

		// Regression: this was `<a href="/signup">` on /signup — a same-route
		// navigation the SPA router resolved without remounting, so the click did
		// nothing and the user was stuck on the confirmation.
		await page.getByRole('button', { name: 'try again' }).click();

		await expect(page.getByRole('button', { name: 'Send verification email' })).toBeVisible();
		await expect(page.getByLabel('Company name')).toHaveValue('Retry Co');
		await expect(page.getByPlaceholder('acme')).toHaveValue(slug);
		// Focus goes to the field most likely to be why no email arrived.
		const emailField = page.getByLabel('Email');
		await expect(emailField).toHaveValue(email);
		await expect(emailField).toBeFocused();
	});

	test('with captcha enabled, "try again" re-renders the widget and demands a fresh token', async ({
		page
	}) => {
		// Local dev and CI run with no hCaptcha sitekey (local-first default), so
		// this path is otherwise never exercised. Everything external is stubbed:
		// the sitekey, the submit endpoint (so the test can read what each
		// attempt sent), and hCaptcha's script — faked with the one behaviour
		// that matters here, IMPLICIT rendering: it draws a widget into each
		// `.h-captcha` present when it loads, and nothing added later. It also
		// records the theme each widget got: implicit mode reads `data-theme`,
		// an explicit render reads `opts.theme`.
		const submissions: Array<{ captcha_token: string | null }> = [];
		await page.route('**/api/public-config', (route) =>
			route.fulfill({
				json: { hcaptcha_sitekey: 'e2e-sitekey', tenant_url_template: tenantBase('{slug}') }
			})
		);
		await page.route('**/api/signup/slug-check**', (route) =>
			route.fulfill({ json: { slug: 'x', available: true, reason: null } })
		);
		await page.route('**/api/signup/start', async (route) => {
			submissions.push(route.request().postDataJSON());
			await route.fulfill({ json: { status: 'pending', message: 'We sent a verification link.' } });
		});
		await page.route('https://hcaptcha.com/1/api.js', (route) =>
			route.fulfill({
				contentType: 'application/javascript',
				body: `(() => {
					let widgets = 0;
					window.hcaptcha = {
						render(el, opts) {
							el.dataset.widget = String(++widgets);
							el.dataset.renderedTheme = (opts && opts.theme) || el.dataset.theme || 'light';
							window.__solveCaptcha = opts && opts.callback;
						}
					};
					document.querySelectorAll('.h-captcha').forEach((el) => window.hcaptcha.render(el, {}));
				})();`
			})
		);

		await page.goto('/signup');
		const widget = page.locator('.h-captcha');
		await expect(widget).toHaveAttribute('data-widget', '1');
		// The app is dark-only, so a light widget is a white box on the form.
		await expect(widget).toHaveAttribute('data-rendered-theme', 'dark');

		await fillForm(page, {
			company: 'Captcha Co',
			slug: 'captchaco',
			name: 'Captcha Admin',
			email: 'captcha@example.com'
		});
		await expect(page.locator('small.hint.ok')).toBeVisible();
		// Implicit mode reports a solve through the named global callback.
		await page.evaluate(() => (window as unknown as { hcaptchaCallback: (t: string) => void }).hcaptchaCallback('token-1'));
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeFocused();
		expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1']);

		await page.getByRole('button', { name: 'try again' }).click();

		// A NEW container was mounted, long after the script loaded: only an
		// explicit render puts a widget in it.
		await expect(widget).toHaveAttribute('data-widget', '2');
		await expect(widget).toHaveAttribute('data-rendered-theme', 'dark');

		// The spent token must not ride along: submitting unsolved is refused
		// client-side and never reaches the endpoint.
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.locator('.error')).toHaveText('Please complete the captcha.');
		expect(submissions).toHaveLength(1);

		// Solving the re-rendered widget is what the retry then carries.
		await page.evaluate(() => (window as unknown as { __solveCaptcha: (t: string) => void }).__solveCaptcha('token-2'));
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
		expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1', 'token-2']);
	});

	test('a taken slug blocks submission with an inline reason', async ({ page }) => {
		await page.goto('/signup');
		await page.getByPlaceholder('acme').fill('acme'); // seeded tenant
		await expect(page.locator('small.hint.bad')).toBeVisible({ timeout: 5_000 });
		await expect(
			page.getByRole('button', { name: 'Send verification email' })
		).toBeDisabled();
	});

	test('the email field enforces a valid address before submit', async ({ page }) => {
		const slug = `e2email${Date.now().toString().slice(-9)}`;
		await page.goto('/signup');
		await fillForm(page, {
			company: 'Bad Email Co',
			slug,
			name: 'Nope',
			email: 'not-an-email'
		});
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });
		await page.getByRole('button', { name: 'Send verification email' }).click();

		// type="email" keeps us on the form; the success state never appears.
		await expect(page.getByRole('heading', { name: 'Check your email' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Send verification email' })).toBeVisible();
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 2. /verify — success + error states (stubbed or bogus tokens; no email needed)
// ───────────────────────────────────────────────────────────────────────────

test.describe('/verify — outcomes', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	test('no token in the URL surfaces an error', async ({ page }) => {
		await page.goto('/verify');
		await expect(page.getByRole('heading', { name: 'Something went wrong' })).toBeVisible({
			timeout: 10_000
		});
		await expect(page.getByText(/no verification token/i)).toBeVisible();
		await expect(page.getByRole('link', { name: 'Start over' })).toBeVisible();
	});

	test('success offers "Set your password" straight to the server-built link', async ({ page }) => {
		const setUrl = `${tenantBase('stubco')}/login/reset-password?token=stubbed&mode=set`;
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) =>
				route.fulfill({
					json: {
						status: 'provisioned',
						slug: 'stubco',
						tenant_url: tenantBase('stubco'),
						admin_email: 'admin@stubco.example',
						set_password_url: setUrl
					}
				})
		);
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible();
		await expect(page.getByRole('link', { name: 'Set your password' })).toHaveAttribute(
			'href',
			setUrl
		);
		await expect(page.getByText('admin@stubco.example')).toBeVisible();
		// Nothing left to copy out of an email.
		await expect(page.getByText(/temporary password/i)).toHaveCount(0);
	});

	test('success without a link points at Forgot password', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) =>
				route.fulfill({
					json: {
						status: 'provisioned',
						slug: 'stubco',
						tenant_url: '',
						admin_email: 'admin@stubco.example',
						set_password_url: null
					}
				})
		);
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible();
		await expect(page.getByText(/use forgot password on your workspace/i)).toBeVisible();
		await expect(page.getByRole('link', { name: 'Set your password' })).toHaveCount(0);
	});

	test('an invalid/expired token surfaces the uniform error', async ({ page }) => {
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'Something went wrong' })).toBeVisible({
			timeout: 10_000
		});
		// Backend returns the uniform 410 message (no 404-vs-410 enumeration).
		await expect(page.getByText(/invalid or has expired/i)).toBeVisible();
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 3. Full happy path — UI + real email (Mailpit) end to end
// ───────────────────────────────────────────────────────────────────────────

test.describe('signup happy path (UI + Mailpit)', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	test.beforeEach(async () => {
		await skipUnlessReachable(SERVICES.mailpit);
	});

	test('new user: form → verify email → set password → sign in → dashboard', async ({
		page
	}) => {
		const slug = `e2eflow${Date.now().toString().slice(-9)}`;
		const email = `${slug}@example.com`;

		// 1. Fill + submit the signup form through the UI.
		await page.goto('/signup');
		await page.getByLabel('Company name').fill('Happy Path Co');
		await page.getByPlaceholder('acme').fill(slug);
		await page.getByLabel('Your name').fill('Happy Admin');
		await page.getByLabel('Email').fill(email);
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible({
			timeout: 10_000
		});

		// 2. Pull the verification link out of the email and open it (the user clicks it).
		const verifyBody = await waitForEmail(page, email, 'Verify your FeohLedger');
		const verifyLink = verifyBody.match(/\/verify\?token=[A-Za-z0-9_-]+/);
		expect(verifyLink, 'verify link in email').toBeTruthy();

		await page.goto(verifyLink![0]);
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible({
			timeout: 20_000
		});

		// 3. The welcome email carries the same set-password link, and no password.
		const welcomeBody = await waitForEmail(page, email, 'Your FeohLedger workspace');
		expect(welcomeBody).not.toMatch(/Password:/);
		const emailed = welcomeBody.match(/\S+\/login\/reset-password\?token=\S+&mode=set/);
		expect(emailed, 'set-password link in welcome email').toBeTruthy();
		const setPassword = page.getByRole('link', { name: 'Set your password' });
		await expect(setPassword).toHaveAttribute('href', emailed![0]);

		// 4. "Set your password" goes straight to the tenant's set-password page.
		await setPassword.click();
		await expect(page).toHaveURL(new RegExp(`^${tenantBase(slug)}/login/reset-password\\?`));
		await expect(page.getByRole('heading', { name: 'Set your password' })).toBeVisible();
		await expect(page.locator('form input[autocomplete="username"]')).toHaveValue(email);

		// A reload (or a mail scanner's visit) reads the token, never spends it.
		await page.reload();
		await expect(page.locator('form input[autocomplete="username"]')).toHaveValue(email);

		// 5. Choose a password; the link is now spent.
		const newPassword = 'Sup3rSecret!newpw';
		await page.getByLabel('New password', { exact: true }).fill(newPassword);
		await page.getByLabel('Confirm new password').fill(newPassword);
		await page.getByRole('button', { name: 'Set password' }).click();
		await expect(page.getByText('Your password is set. You can now sign in.')).toBeVisible();

		// 6. Sign in with the address prefilled — no forced change, straight in.
		await page.getByRole('link', { name: 'Go to sign in' }).click();
		await expect(page.locator('input[type="email"]')).toHaveValue(email);
		await page.locator('input[type="password"]').fill(newPassword);
		await page.locator('form button[type="submit"]').click();
		await expect(page).toHaveURL(TENANT_ROOT_URL, { timeout: 15_000 });

		// 7. The emailed copy of the link died with that use (one token for both).
		const token = new URL(emailed![0]).searchParams.get('token');
		const spent = await page.request.post(`${API_BASE}/api/auth/reset-password/check`, {
			data: { token }
		});
		expect(spent.status()).toBe(400);
	});
});
