import { createHmac } from 'node:crypto';

import { API_BASE, currentTenantSlug, expect, signIn, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * MFA e2e coverage — the two MFA surfaces in the web app:
 *
 *   1. `/login/mfa`  — the second-factor challenge step (TOTP + email-OTP,
 *                      method switching, validation).
 *   2. `/profile`    — the per-user enrollment / disable UI.
 *
 * ── Why this file is split into two regimes ───────────────────────────────
 *
 * MFA is OFF by default. The master switch is the backend env var
 * `FEOH_MFA_ENABLED` (committed `false` in `backend/.env.development`, which
 * `main.py` loads — so the CI/e2e backend boots with MFA disabled). With MFA
 * off:
 *   - `POST /api/auth/login` never returns an MFA challenge (it mints a token
 *     directly), so a *real* login never reaches `/login/mfa`.
 *   - `/api/auth/mfa/{enroll,enroll/verify,verify,challenge/email}` all
 *     hard-400 with "MFA is disabled".
 *
 * BUT `/login/mfa` is a purely client-side page: it reads its challenge from
 * `sessionStorage['mfa_challenge']` (stashed by `/login` after a 'mfa' login
 * result), NOT from a backend call on mount. So the *entire challenge UI* —
 * render, method switching, validation gating, the missing-challenge redirect
 * — is reachable in the default e2e env by seeding a synthetic challenge into
 * sessionStorage. The describe block "challenge UI (sessionStorage-seeded)"
 * needs no MFA-enabled backend and runs everywhere.
 *
 * The *full happy path* — real TOTP enrollment on /profile, and trading a real
 * challenge token + a computed TOTP code for a session at /login/mfa — needs a
 * backend booted with MFA on. Those live in the describe blocks guarded by
 * `mfaBackendEnabled()` (env `FEOH_E2E_MFA_ENABLED=true`) and are skipped when
 * the flag is absent, with a `test.info().annotations` note so a skip is never
 * silent. See "Running the MFA happy-path suite" at the bottom of this file.
 */

const CHALLENGE_KEY = 'mfa_challenge';

/** True when the operator has brought up an MFA-enabled backend and signalled
 *  it to Playwright. Gates the real-verify / real-enroll suites. */
function mfaBackendEnabled(): boolean {
	return process.env.FEOH_E2E_MFA_ENABLED === 'true';
}

/** A synthetic MFA challenge for the sessionStorage-driven UI tests. The token
 *  is deliberately bogus — these tests exercise the *page's* render/switch/
 *  validation logic, which never depends on the token being valid until the
 *  user submits. `methods` controls which switch affordances appear. */
function fakeChallenge(opts?: {
	methods?: string[];
	must_enroll?: boolean;
	token?: string;
}) {
	return {
		mfa_required: true as const,
		mfa_challenge_token: opts?.token ?? 'e2e-fake-challenge-token',
		methods: opts?.methods ?? ['totp', 'email'],
		must_enroll: opts?.must_enroll ?? false
	};
}

/**
 * Compute a RFC-6238 TOTP code from a base32 secret, entirely in-process (no
 * dependency — Node's built-in crypto does HMAC-SHA1). Used by the happy-path
 * suite to produce a code the backend will accept against the enrolled secret.
 */
function totpNow(base32Secret: string, forTime = Date.now()): string {
	const key = base32Decode(base32Secret);
	const counter = Math.floor(forTime / 1000 / 30);
	const buf = Buffer.alloc(8);
	buf.writeBigUInt64BE(BigInt(counter));
	const hmac = createHmac('sha1', key).update(buf).digest();
	const offset = hmac[hmac.length - 1] & 0x0f;
	const bin =
		((hmac[offset] & 0x7f) << 24) |
		((hmac[offset + 1] & 0xff) << 16) |
		((hmac[offset + 2] & 0xff) << 8) |
		(hmac[offset + 3] & 0xff);
	return (bin % 1_000_000).toString().padStart(6, '0');
}

/** Decode an RFC-4648 base32 string (the format pyotp emits) to bytes. */
function base32Decode(input: string): Buffer {
	const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
	const clean = input.replace(/=+$/, '').toUpperCase().replace(/\s/g, '');
	let bits = 0;
	let value = 0;
	const out: number[] = [];
	for (const ch of clean) {
		const idx = alphabet.indexOf(ch);
		if (idx === -1) continue;
		value = (value << 5) | idx;
		bits += 5;
		if (bits >= 8) {
			out.push((value >>> (bits - 8)) & 0xff);
			bits -= 8;
		}
	}
	return Buffer.from(out);
}

// ───────────────────────────────────────────────────────────────────────────
// 1. Challenge UI — sessionStorage-seeded (runs in every e2e env, MFA off OK)
// ───────────────────────────────────────────────────────────────────────────

test.describe('/login/mfa — challenge UI (sessionStorage-seeded)', () => {
	// The challenge page renders without auth (the layout lets any /login*
	// path through). Start from a clean, unauthenticated context.
	test.use({ storageState: { cookies: [], origins: [] } });

	/** Navigate to /login/mfa with a challenge already in sessionStorage.
	 *  `addInitScript` runs before the page's own scripts, so the value is
	 *  present when the route's `onMount` reads it. */
	async function gotoChallenge(
		page: import('@playwright/test').Page,
		challenge: ReturnType<typeof fakeChallenge>
	) {
		// Land on the tenant origin first so sessionStorage is same-origin
		// writable, then seed + navigate to the MFA page.
		await page.goto('/login');
		await page.evaluate(
			([key, value]) => sessionStorage.setItem(key, value),
			[CHALLENGE_KEY, JSON.stringify(challenge)] as const
		);
		await page.goto('/login/mfa');
	}

	test('redirects back to /login when no challenge is present', async ({ page }) => {
		// No challenge seeded → onMount bounces to /login. This is the
		// guard that stops someone deep-linking straight to the MFA step.
		await page.goto('/login/mfa');
		await expect(page).toHaveURL(/\/login$/, { timeout: 5_000 });
	});

	test('renders the TOTP challenge by default when totp is offered', async ({ page }) => {
		await gotoChallenge(page, fakeChallenge({ methods: ['totp', 'email'] }));

		await expect(page.getByRole('heading', { name: 'Two-factor verification' })).toBeVisible();
		// TOTP is preferred when present in `methods` — the code input is
		// shown immediately (no "email me a code" gate).
		await expect(page.getByText('Enter the 6-digit code from your authenticator app.')).toBeVisible();
		await expect(page.locator('input[autocomplete="one-time-code"]')).toBeVisible();
		// Verify button is disabled until the code reaches 6 digits.
		await expect(page.getByRole('button', { name: /Verify/ })).toBeDisabled();
	});

	test('Verify button enables only once 6 digits are entered', async ({ page }) => {
		await gotoChallenge(page, fakeChallenge({ methods: ['totp', 'email'] }));

		const code = page.locator('input[autocomplete="one-time-code"]');
		const verify = page.getByRole('button', { name: /Verify/ });

		await code.fill('123');
		await expect(verify).toBeDisabled();
		await code.fill('123456');
		await expect(verify).toBeEnabled();
	});

	test('switches from authenticator to email method', async ({ page }) => {
		await gotoChallenge(page, fakeChallenge({ methods: ['totp', 'email'] }));

		// Both methods offered → the "or / Use email instead" switch shows.
		await page.getByRole('button', { name: 'Use email instead' }).click();

		// Email mode: subtitle changes, the code input is replaced by the
		// "Email me a code" request button (no code field until requested).
		await expect(page.getByText("We'll email a one-time code to your account address.")).toBeVisible();
		await expect(page.getByRole('button', { name: 'Email me a code' })).toBeVisible();
		await expect(page.locator('input[autocomplete="one-time-code"]')).toHaveCount(0);

		// Switch back to the authenticator.
		await page.getByRole('button', { name: 'Use authenticator app' }).click();
		await expect(page.locator('input[autocomplete="one-time-code"]')).toBeVisible();
	});

	test('email-only challenge starts in email mode with no method switch', async ({ page }) => {
		// An unenrolled user under org-enforcement is offered email only.
		await gotoChallenge(page, fakeChallenge({ methods: ['email'], must_enroll: true }));

		await expect(page.getByText("We'll email a one-time code to your account address.")).toBeVisible();
		await expect(page.getByRole('button', { name: 'Email me a code' })).toBeVisible();
		// Single method → the "or / Use … instead" switch must NOT render.
		await expect(page.getByRole('button', { name: 'Use authenticator app' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Use email instead' })).toHaveCount(0);
		// must_enroll + email mode surfaces the enrollment notice.
		await expect(page.getByText(/Your organization requires MFA/)).toBeVisible();
	});

	test('a bad TOTP code surfaces the verification error and stays on the page', async ({ page }) => {
		// With MFA disabled the verify endpoint 400s ("MFA is disabled"); with
		// MFA enabled but a bogus token it 401s. Either way the page must NOT
		// navigate away — it shows the error and lets the user retry. We assert
		// the error-surfacing contract (the security contract is "no token, no
		// redirect"), not the exact copy.
		await gotoChallenge(page, fakeChallenge({ methods: ['totp', 'email'] }));

		await page.locator('input[autocomplete="one-time-code"]').fill('000000');
		const verifyResp = page.waitForResponse(
			(r) => r.url().includes('/api/auth/mfa/verify')
		);
		await page.getByRole('button', { name: /Verify/ }).click();
		await verifyResp;

		await expect(page.locator('.error')).toBeVisible({ timeout: 5_000 });
		await expect(page).toHaveURL(/\/login\/mfa$/);
	});

	test('requesting an email code calls the email-challenge endpoint', async ({ page }) => {
		await gotoChallenge(page, fakeChallenge({ methods: ['totp', 'email'] }));

		await page.getByRole('button', { name: 'Use email instead' }).click();

		const emailResp = page.waitForResponse(
			(r) => r.url().includes('/api/auth/mfa/challenge/email')
		);
		await page.getByRole('button', { name: 'Email me a code' }).click();
		const resp = await emailResp;

		// With MFA disabled the backend 400s and the page shows `.error`
		// (emailSent stays false → no code field). With MFA enabled the 204
		// flips `emailSent` → the code field appears. Assert on the branch the
		// actual response dictates so the test is deterministic in both regimes
		// rather than masking either.
		if (resp.status() === 204) {
			await expect(page.locator('input[autocomplete="one-time-code"]')).toBeVisible({
				timeout: 5_000
			});
		} else {
			await expect(page.locator('.error')).toBeVisible({ timeout: 5_000 });
		}
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 2. /profile MFA section — logged-in worker admin (MFA off: error-path only)
// ───────────────────────────────────────────────────────────────────────────

test.describe('/profile — two-factor section', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/profile?section=mfa');
	});

	test('renders the Two-factor authentication card', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Profile & Security' })).toBeVisible();
		await expect(
			page.getByRole('heading', { name: 'Two-factor authentication' })
		).toBeVisible();
	});

	test('shows "Not configured" + a setup button for a non-enrolled user', async ({ page }) => {
		// Seeded e2e users are not MFA-enrolled, so the section is in the
		// not-configured state with a "Set up two-factor" call to action.
		await expect(page.getByText('Not configured')).toBeVisible();
		await expect(page.getByRole('button', { name: /Set up two-factor/ })).toBeVisible();
	});

	test('starting enrollment with MFA disabled surfaces the disabled error', async ({ page }) => {
		// This is the only enrollment behaviour reachable when the backend has
		// MFA off: `POST /api/auth/mfa/enroll` 400s ("MFA is disabled on this
		// deployment") and the page toasts the error rather than showing a QR.
		// When MFA is on, the same click yields a QR — covered in the
		// real-enroll suite below. Branch on the response so both regimes are
		// deterministic.
		test.skip(
			mfaBackendEnabled(),
			'MFA enabled — the QR path is covered by the real-enroll suite'
		);

		const enrollResp = page.waitForResponse((r) => r.url().includes('/api/auth/mfa/enroll'));
		await page.getByRole('button', { name: /Set up two-factor/ }).click();
		const resp = await enrollResp;

		expect(resp.status()).toBe(400);
		// The error surfaces as a Toast (`.toast.error`); no QR is rendered.
		await expect(page.locator('.toast.error')).toBeVisible({ timeout: 5_000 });
		await expect(page.locator('img[alt="MFA QR code"]')).toHaveCount(0);
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 3. Real enrollment on /profile — requires an MFA-enabled backend
// ───────────────────────────────────────────────────────────────────────────

test.describe('/profile — real TOTP enrollment (MFA-enabled backend)', () => {
	test.beforeEach(async () => {
		test.skip(
			!mfaBackendEnabled(),
			'Set FEOH_E2E_MFA_ENABLED=true with an FEOH_MFA_ENABLED backend to run the MFA happy path'
		);
	});

	test('enrolls TOTP end to end (QR → verify → Enabled)', async ({ page }) => {
		await page.goto('/profile?section=mfa');

		// Start enrollment → backend mints the secret + QR. Capture the secret
		// from the enroll response so we can compute a matching TOTP code.
		const enrollResp = page.waitForResponse(
			(r) => r.url().includes('/api/auth/mfa/enroll') && !r.url().includes('verify')
		);
		await page.getByRole('button', { name: /Set up two-factor/ }).click();
		const secret = (await (await enrollResp).json()).secret as string;

		// QR image renders for step 1.
		await expect(page.locator('img[alt="MFA QR code"]')).toBeVisible({ timeout: 5_000 });

		// Step 2: enter a freshly-computed code and verify.
		await page.locator('input[autocomplete="one-time-code"]').fill(totpNow(secret));
		await page.getByRole('button', { name: /Verify and enable/ }).click();

		// Success → the section flips to "Enabled" and a success toast fires.
		await expect(page.getByText('Enabled', { exact: true })).toBeVisible({ timeout: 5_000 });
		await expect(page.locator('.toast.success')).toBeVisible();

		// Cleanup: disable MFA so the next run / sibling spec starts clean.
		// (The seeded e2e user's password is 'demo'.)
		await page
			.locator('input[autocomplete="current-password"]')
			.fill('demo');
		await page.getByRole('button', { name: /Disable two-factor/ }).click();
		await expect(page.getByText('Not configured')).toBeVisible({ timeout: 5_000 });
	});
});

// ───────────────────────────────────────────────────────────────────────────
// 4. Real login challenge → TOTP verify — requires an MFA-enabled backend
// ───────────────────────────────────────────────────────────────────────────

test.describe('/login/mfa — real TOTP verification (MFA-enabled backend)', () => {
	// Unauthenticated: this exercises the full password → challenge → verify
	// chain, so start with no stored session.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async () => {
		test.skip(
			!mfaBackendEnabled(),
			'Set FEOH_E2E_MFA_ENABLED=true with an FEOH_MFA_ENABLED backend to run the MFA happy path'
		);
	});

	test('password login → TOTP challenge → verified session', async ({ page, tenantAdmin }) => {
		const slug = currentTenantSlug();

		// Pre-req: the worker admin must be TOTP-enrolled on the backend. Enroll
		// directly via the API so the login below produces a 'totp' challenge.
		// (We can't compute a code without the secret, and the API returns it.)
		const loginRes = await page.request.post(`${API_BASE}/api/auth/login`, {
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		// If the admin is already mid-challenge this returns 200 with a
		// challenge; otherwise a token. Use a fresh API enroll either way.
		const loginJson = await loginRes.json();

		// Obtain a real access token (no challenge yet — admin not enrolled).
		const token = loginJson.access_token as string | undefined;
		test.skip(
			!token,
			'worker admin already MFA-enrolled — cannot derive secret; run against a clean tenant'
		);

		const enroll = await page.request.post(`${API_BASE}/api/auth/mfa/enroll`, {
			headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': slug },
			data: {}
		});
		const secret = (await enroll.json()).secret as string;
		await page.request.post(`${API_BASE}/api/auth/mfa/enroll/verify`, {
			headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': slug },
			data: { code: totpNow(secret) }
		});

		// Now a real UI login must route to /login/mfa.
		await signIn(page, tenantAdmin);
		await page.waitForURL(/\/login\/mfa$/, { timeout: 15_000 });
		await expect(page.locator('input[autocomplete="one-time-code"]')).toBeVisible();

		// Enter a fresh code → verify → land on the dashboard.
		await page.locator('input[autocomplete="one-time-code"]').fill(totpNow(secret));
		await page.getByRole('button', { name: /Verify/ }).click();
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });

		// Cleanup: disable MFA on this admin so the worker's other specs (which
		// reuse the same seeded admin) aren't suddenly MFA-gated.
		const after = await (
			await page.request.post(`${API_BASE}/api/auth/login`, {
				data: { email: tenantAdmin.email, password: tenantAdmin.password }
			})
		).json();
		// `after` is a challenge now; verify to get a token, then disable.
		const verified = await (
			await page.request.post(`${API_BASE}/api/auth/mfa/verify`, {
				data: {
					challenge_token: after.mfa_challenge_token,
					code: totpNow(secret),
					method: 'totp'
				}
			})
		).json();
		await page.request.post(`${API_BASE}/api/auth/mfa/disable`, {
			headers: { Authorization: `Bearer ${verified.access_token}`, 'X-Tenant-Slug': slug },
			data: { password: tenantAdmin.password }
		});
	});
});

/**
 * ── Running the MFA happy-path suite ───────────────────────────────────────
 *
 * The describe blocks (3) and (4) are skipped unless `FEOH_E2E_MFA_ENABLED=true`
 * AND the backend was booted with MFA on. To run them locally:
 *
 *   1. Start the backend with MFA enabled (overrides the committed default):
 *        cd backend && FEOH_MFA_ENABLED=true python main.py
 *   2. Seed if not already:  python scripts/seed.py
 *   3. Run just the MFA spec with the flag set:
 *        cd frontend && FEOH_E2E_MFA_ENABLED=true pnpm exec playwright test \
 *          tests-e2e/auth/mfa.spec.ts
 *
 * Without those, the sessionStorage-seeded challenge-UI block (1) and the
 * /profile render + disabled-error block (2) still run and provide the bulk of
 * the coverage — they need no MFA-enabled backend.
 *
 * NOTE on org enforcement: the email-only / must_enroll challenge is covered
 * via a synthetic sessionStorage challenge in block (1). Exercising the *real*
 * org-enforced path end to end additionally needs the worker tenant's
 * `Organization.settings.mfa.required` flipped on (e.g. a `tenantPsql` UPDATE
 * in a beforeAll, or a seed flag) — left as a documented follow-up rather than
 * mutating shared per-worker tenant state inside this spec.
 */
