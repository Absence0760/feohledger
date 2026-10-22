import type { Page } from '@playwright/test';

import {
	ACME_ADMIN,
	API_BASE,
	expect,
	NO_TENANT_BASE,
	TENANT_ROOT_URL,
	tenantBase,
	test
} from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import { captchaEvent, stubCaptcha } from './captchaStub';

/**
 * The palette the page is actually showing, read the way the app resolves it
 * (`src/lib/theme.ts::resolveTheme`): an explicit `data-theme` wins, and
 * System follows `prefers-color-scheme: light`, else dark.
 */
async function resolvedPageTheme(page: Page): Promise<'light' | 'dark'> {
	return page.evaluate(() => {
		const choice = document.documentElement.dataset.theme;
		if (choice === 'light' || choice === 'dark') return choice;
		return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
	});
}

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
 *     no password is ever emailed, decisions §283) → sign in with the email
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

/**
 * Like {@link waitForEmail}, but for an address other specs also mail (a seeded
 * login): the message must also mention `needle`, so an older one with the same
 * subject is never mistaken for this run's.
 */
async function waitForEmailMentioning(
	page: Page,
	email: string,
	subjectPrefix: string,
	needle: string
): Promise<string> {
	let text: string | null = null;
	await expect
		.poll(
			async () => {
				for (const msg of await messagesTo(page, email)) {
					if (!msg.Subject.startsWith(subjectPrefix)) continue;
					const body = await messageText(page, msg.ID);
					if (body.includes(needle)) {
						text = body;
						break;
					}
				}
				return text;
			},
			{ timeout: 15_000 }
		)
		.not.toBeNull();
	return text!;
}

// The captcha stub (sitekey, endpoints, a fake explicit-render hCaptcha) is
// shared with captcha.spec.ts and form-rules.spec.ts: ./captchaStub.ts.

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

	// Run under each OS colour scheme: the page follows the system by default
	// (docs/decisions.md §289), so the widget must take whichever palette the
	// page resolved to — pinned per run rather than left to the runner's default.
	for (const scheme of ['dark', 'light'] as const) test(`with captcha enabled, "try again" re-renders the widget and demands a fresh token (${scheme} OS)`, async ({
		page
	}) => {
		await page.emulateMedia({ colorScheme: scheme });
		const { submissions } = await stubCaptcha(page);

		await page.goto('/signup');
		const widget = page.locator('.h-captcha');
		await expect(widget).toHaveAttribute('data-widget', '1');
		// The widget is an iframe and cannot see our palette: it must be told the
		// one the page is showing, or it is a white box on a dark form (or the
		// reverse). No stored choice, so the page is on System → the OS scheme.
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'system');
		expect(await resolvedPageTheme(page)).toBe(scheme);
		await expect(widget).toHaveAttribute('data-rendered-theme', scheme);

		await fillForm(page, {
			company: 'Captcha Co',
			slug: 'captchaco',
			name: 'Captcha Admin',
			email: 'captcha@example.com'
		});
		await expect(page.locator('small.hint.ok')).toBeVisible();
		await captchaEvent(page, 'solve', 'token-1');
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeFocused();
		expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1']);

		await page.getByRole('button', { name: 'try again' }).click();

		// A NEW container was mounted, long after the script loaded, and gets a
		// widget of its own; the old one, whose container left with the form, is
		// removed.
		await expect(widget).toHaveAttribute('data-widget', '2');
		expect(await page.evaluate(() => (window as unknown as { __removedWidgets: string[] }).__removedWidgets)).toEqual(['w1']);
		await expect(widget).toHaveAttribute('data-rendered-theme', scheme);

		// The spent token must not ride along: submitting unsolved is refused
		// client-side and never reaches the endpoint.
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.locator('.error')).toHaveText('Please complete the captcha.');
		expect(submissions).toHaveLength(1);

		// Solving the re-rendered widget is what the retry then carries.
		await captchaEvent(page, 'solve', 'token-2');
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
		expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1', 'token-2']);
	});

	// The widget follows the theme AFTER first render too (docs/decisions.md
	// §290): it was read once at render, so a theme picked on the shell — or the
	// OS switching while on System — left a dark captcha on a light form. A
	// solved widget is the exception: redrawing it would throw away the token.
	test('with captcha enabled, the widget follows a theme change, but never at the cost of a solved token', async ({
		page
	}) => {
		await page.emulateMedia({ colorScheme: 'dark' });
		const { submissions } = await stubCaptcha(page);
		await page.goto('/signup');
		const widget = page.locator('.h-captcha');
		await expect(widget).toHaveAttribute('data-widget', '1');
		await expect(widget).toHaveAttribute('data-rendered-theme', 'dark');

		// Picked on the auth shell's own theme control: redrawn in light.
		const picker = page.getByRole('group', { name: 'Theme' });
		await picker.getByRole('radio', { name: 'Light' }).check();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await expect(widget).toHaveAttribute('data-widget', '2');
		await expect(widget).toHaveAttribute('data-rendered-theme', 'light');

		// Back to System while the OS is dark, then the OS turns light: the
		// widget follows the OS with no click at all.
		await picker.getByRole('radio', { name: 'System' }).check();
		await expect(widget).toHaveAttribute('data-rendered-theme', 'dark');
		await page.emulateMedia({ colorScheme: 'light' });
		await expect(widget).toHaveAttribute('data-rendered-theme', 'light');
		await expect(widget).toHaveAttribute('data-widget', '4');
		expect(
			await page.evaluate(() => (window as unknown as { __removedWidgets: string[] }).__removedWidgets)
		).toEqual(['w1', 'w2', 'w3']);

		// Solved, then the theme changes: the widget — and its token — stay.
		await fillForm(page, {
			company: 'Theme Co',
			slug: 'themeco',
			name: 'Theme Admin',
			email: 'theme@example.com'
		});
		await captchaEvent(page, 'solve', 'token-kept');
		await picker.getByRole('radio', { name: 'Dark' }).check();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
		await expect(widget).toHaveAttribute('data-widget', '4');
		await expect(widget).toHaveAttribute('data-rendered-theme', 'light');

		// Once the token expires it is worth nothing, so the widget is redrawn in
		// the palette now showing...
		await captchaEvent(page, 'expire');
		await expect(widget).toHaveAttribute('data-widget', '5');
		await expect(widget).toHaveAttribute('data-rendered-theme', 'dark');

		// ...and a token solved before a theme change is the one submitted.
		await captchaEvent(page, 'solve', 'token-final');
		await picker.getByRole('radio', { name: 'Light' }).check();
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
		expect(submissions.map((b) => b.captcha_token)).toEqual(['token-final']);
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

	test('a re-opened link shows the workspace and a way to sign in, with nothing re-issued', async ({
		page
	}) => {
		// decisions §292: a reload or a second tab used to get "invalid, sign up
		// again". The server now re-answers a spent link with its workspace.
		const signInUrl = `${tenantBase('stubco')}/login?email=admin%40stubco.example`;
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) =>
				route.fulfill({
					json: {
						status: 'provisioned',
						slug: 'stubco',
						tenant_url: tenantBase('stubco'),
						admin_email: 'admin@stubco.example',
						set_password_url: null,
						sign_in_url: signInUrl,
						already_verified: true
					}
				})
		);
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible();
		await expect(page.getByText(/you already confirmed this email/i)).toBeVisible();
		await expect(page.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute(
			'href',
			signInUrl
		);
		await expect(page.getByRole('link', { name: 'Set your password' })).toHaveCount(0);
	});

	test('an address that already has a workspace is pointed at it', async ({ page }) => {
		const signInUrl = `${tenantBase('stubco')}/login?email=admin%40stubco.example`;
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) =>
				route.fulfill({
					status: 409,
					json: {
						detail: {
							code: 'signup_email_has_workspace',
							message: 'server English',
							params: { sign_in_url: signInUrl }
						}
					}
				})
		);
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(
			page.getByRole('heading', { name: 'You already have a workspace' })
		).toBeVisible();
		// The localized sentence, not the server's English.
		await expect(
			page.getByText('This email address already has a FeohLedger workspace.', { exact: false })
		).toBeVisible();
		await expect(
			page.getByRole('link', { name: "Go to your workspace's sign-in page" })
		).toHaveAttribute('href', signInUrl);
		await expect(page.getByText(/something went wrong/i)).toHaveCount(0);
	});

	test('an invalid/expired token says the link expired, in the reader\'s language', async ({
		page
	}) => {
		// The real backend: the uniform 410 for a token that never existed.
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'This link has expired' })).toBeVisible({
			timeout: 10_000
		});
		await expect(page.getByText(/verification links work once and last 24 hours/i)).toBeVisible();
		// Both ways on: a fresh link, or the workspace someone already made.
		await expect(page.getByRole('link', { name: 'Start over' })).toHaveAttribute(
			'href',
			'/signup'
		);
		await expect(page.getByText(/already set up your workspace\?/i)).toBeVisible();
		// Neither the generic failure nor the server's English sentence.
		await expect(page.getByText(/something went wrong/i)).toHaveCount(0);
		await expect(page.getByText(/please sign up again/i)).toHaveCount(0);
	});

	test('the 410 is read by its status, never by the server\'s wording', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) => route.fulfill({ status: 410, json: { detail: 'any wording at all' } })
		);
		await page.goto('/verify?token=' + 'x'.repeat(40));
		await expect(page.getByRole('heading', { name: 'This link has expired' })).toBeVisible();
		await expect(page.getByText('any wording at all')).toHaveCount(0);
	});

	test('the token leaves the address bar once the link has been answered', async ({ page }) => {
		// Left in place it stays in history, a copied URL and a screenshot.
		const requested: string[] = [];
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) => {
				requested.push(route.request().postDataJSON().token);
				return route.fulfill({
					json: {
						status: 'provisioned',
						slug: 'stubco',
						tenant_url: tenantBase('stubco'),
						admin_email: 'admin@stubco.example',
						set_password_url: null
					}
				});
			}
		);
		const token = 'y'.repeat(40);
		await page.goto(`/verify?token=${token}&utm_source=email`);
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible();
		await expect(page).toHaveURL(/\/verify\?utm_source=email$/);
		expect(page.url()).not.toContain(token);
		// The request itself still carried it.
		expect(requested).toEqual([token]);
	});

	test('a reload after the strip still re-sends the token, from the tab\'s own history entry', async ({
		page
	}) => {
		// The token left the address bar, but a reload must still reach the
		// backend's re-answer for a spent link (decisions §292), not "no token".
		const signInUrl = `${tenantBase('stubco')}/login?email=admin%40stubco.example`;
		const sent: string[] = [];
		await page.route(
			(url) => url.pathname === '/api/signup/complete',
			(route) => {
				sent.push(route.request().postDataJSON().token);
				return route.fulfill({
					json: {
						status: 'provisioned',
						slug: 'stubco',
						tenant_url: tenantBase('stubco'),
						admin_email: 'admin@stubco.example',
						set_password_url: null,
						sign_in_url: signInUrl,
						already_verified: sent.length > 1
					}
				});
			}
		);
		const token = 'r'.repeat(40);
		await page.goto(`/verify?token=${token}`);
		await expect(page.getByRole('heading', { name: 'Your workspace is ready' })).toBeVisible();
		await expect(page).toHaveURL(/\/verify$/);

		await page.reload();
		await expect(page.getByText(/you already confirmed this email/i)).toBeVisible();
		await expect(page.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute(
			'href',
			signInUrl
		);
		await expect(page).toHaveURL(/\/verify$/);
		expect(sent).toEqual([token, token]);
	});

	test('the token is stripped from a refused link too', async ({ page }) => {
		const token = 'z'.repeat(40);
		await page.goto(`/verify?token=${token}`);
		await expect(page.getByRole('heading', { name: 'This link has expired' })).toBeVisible({
			timeout: 10_000
		});
		await expect(page).toHaveURL(/\/verify$/);
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

		// 3b. Opening the same link again — a second tab here — is re-answered
		//     with the workspace, not "invalid, sign up again", and issues no
		//     second set-password link (decisions §292).
		const secondTab = await page.context().newPage();
		await secondTab.goto(verifyLink![0]);
		await expect(
			secondTab.getByRole('heading', { name: 'Your workspace is ready' })
		).toBeVisible({ timeout: 20_000 });
		await expect(secondTab.getByText(/you already confirmed this email/i)).toBeVisible();
		await expect(secondTab.getByRole('link', { name: 'Set your password' })).toHaveCount(0);
		await expect(secondTab.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute(
			'href',
			`${tenantBase(slug)}/login?email=${encodeURIComponent(email)}`
		);
		await secondTab.close();

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

	test('an address that already has a workspace: verify link → pointed at that workspace', async ({
		page
	}) => {
		// `users.email` is unique across workspaces, so this used to end in a 500
		// "contact support" on every open of the link (decisions §292). /start
		// still answers it like any other address — it is public.
		const slug = `e2edup${Date.now().toString().slice(-9)}`;
		const email = ACME_ADMIN.email;

		await page.goto('/signup');
		await page.getByLabel('Company name').fill('Duplicate Co');
		await page.getByPlaceholder('acme').fill(slug);
		await page.getByLabel('Your name').fill('Duplicate Admin');
		await page.getByLabel('Email').fill(email);
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });
		await page.getByRole('button', { name: 'Send verification email' }).click();
		await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible({
			timeout: 10_000
		});

		// The seeded login is mailed by other specs too: pick this run's message.
		const verifyBody = await waitForEmailMentioning(page, email, 'Verify your FeohLedger', slug);
		const verifyLink = verifyBody.match(/\/verify\?token=[A-Za-z0-9_-]+/);
		expect(verifyLink, 'verify link in email').toBeTruthy();

		const ownSignIn = `${tenantBase('acme')}/login?email=${encodeURIComponent(email)}`;
		for (const open of ['first open', 'reload']) {
			if (open === 'first open') await page.goto(verifyLink![0]);
			else await page.reload();
			await expect(
				page.getByRole('heading', { name: 'You already have a workspace' }),
				open
			).toBeVisible({ timeout: 20_000 });
			await expect(
				page.getByRole('link', { name: "Go to your workspace's sign-in page" }),
				open
			).toHaveAttribute('href', ownSignIn);
		}

		// Nothing was created under the requested name.
		const check = await page.request.get(`${API_BASE}/api/signup/slug-check`, {
			params: { slug }
		});
		expect(((await check.json()) as { available: boolean }).available).toBe(true);
	});
});
