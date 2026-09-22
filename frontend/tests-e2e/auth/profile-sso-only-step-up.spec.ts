import type { Locator, Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` in a tenant that has closed password sign-in (`sso_only`).
 *
 * Every factor change — disabling TOTP, adding or removing a passkey — asks for
 * a step-up, and the server drops an offered password from it when the org is
 * SSO-only (docs/decisions.md §191). The page used to not know: it rendered
 * "Confirm your password" and PREFERRED a typed password over the passkey, so a
 * member who typed one collected a 400 toast instead of never being asked.
 *
 * The page now reads `password_sign_in_closed` off `GET /api/auth/me` — the
 * server's own predicate, not the login page's `/config` echo (§201) — and asks
 * for a current authenticator code or runs the passkey ceremony instead.
 *
 * The e2e backend runs with MFA off and seeds no factors, so the account's
 * factor state is stubbed: `/api/auth/me` is the real response with the flags
 * under test overridden, and the passkey list is faked. The mutating calls are
 * intercepted and answered 400, because what is under test is the proof the
 * page SENDS — the server's verdict on it is pytest's (`test_sso_only.py`).
 */

interface FactorState {
	passwordClosed: boolean;
	totp: boolean;
	passkeys: number;
}

const PASSKEY = {
	id: '00000000-0000-4000-8000-000000000001',
	name: 'Laptop',
	transports: 'internal',
	created_at: '2026-09-01T10:00:00Z',
	last_used_at: null,
	rp_id: 'localhost',
	usable_here: true
};

/** Serve `/api/auth/me` and the passkey list for the given factor state. */
async function stubFactors(page: Page, state: FactorState): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/auth/me',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			await route.fulfill({
				response: real,
				json: {
					...body,
					mfa_enabled: state.totp,
					mfa_required_by_org: false,
					password_sign_in_closed: state.passwordClosed
				}
			});
		}
	);
	await page.route(
		(url) => url.pathname === '/api/auth/mfa/passkey',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			await route.fulfill({
				json: Array.from({ length: state.passkeys }, (_, i) => ({
					...PASSKEY,
					id: PASSKEY.id.replace(/1$/, String(i + 1)),
					name: `${PASSKEY.name} ${i + 1}`
				}))
			});
		}
	);
}

/** Answer a mutating factor call with a 400 — nothing may actually change. */
async function refuse(page: Page, method: string, pathname: string): Promise<void> {
	await page.route(
		(url) => url.pathname === pathname,
		async (route) => {
			if (route.request().method() !== method) return route.fallback();
			await route.fulfill({ status: 400, json: { detail: 'stubbed refusal' } });
		}
	);
}

/** Click `button` and return the JSON body of the `method pathname` it sends. */
async function bodySentBy(
	page: Page,
	button: Locator,
	method: string,
	pathname: string
): Promise<unknown> {
	const [request] = await Promise.all([
		page.waitForRequest(
			(r) => r.method() === method && new URL(r.url()).pathname === pathname
		),
		button.click()
	]);
	return request.postDataJSON();
}

function card(page: Page, heading: string): Locator {
	return page.locator('section.card').filter({ has: page.getByRole('heading', { name: heading }) });
}

/** The panel each card now lives in. */
const PANEL_OF: Record<string, string> = {
	'Two-factor authentication': 'mfa',
	Passkeys: 'passkeys'
};

/**
 * Open the panel that owns `heading`, and return its card.
 *
 * `/profile` shows one panel at a time (`docs/decisions.md` §205), so a card is
 * in the document only while its own panel is showing — two cards can no longer
 * be asserted against a single page load, which is what these tests used to do.
 * Every stub here is registered with `page.route` before this navigates, and a
 * route handler outlives a navigation, so moving between panels costs the test
 * nothing and changes none of its assertions.
 */
async function openCard(page: Page, heading: string): Promise<Locator> {
	const slug = PANEL_OF[heading];
	if (!slug) throw new Error(`no panel recorded for the "${heading}" card`);
	await page.goto(`/profile?section=${slug}`);
	const located = card(page, heading);
	await expect(located).toBeVisible();
	return located;
}

test.describe('/profile — step-up in an SSO-only tenant', () => {
	test('TOTP + passkey: both cards ask for the code, never the password', async ({ page }) => {
		await stubFactors(page, { passwordClosed: true, totp: true, passkeys: 1 });
		await refuse(page, 'POST', '/api/auth/mfa/disable');
		await refuse(page, 'POST', '/api/auth/mfa/passkey/register');

		// --- Two-factor card: the disable form ---
		const mfa = await openCard(page, 'Two-factor authentication');
		const disableCode = mfa.getByLabel('Enter a current authenticator code to disable MFA');
		await expect(disableCode).toBeVisible();
		await expect(mfa.locator('input[type="password"]')).toHaveCount(0);
		await expect(
			mfa.getByText("Your organization signs in with single sign-on, so your password can't")
		).toBeVisible();
		await expect(mfa.getByRole('button', { name: 'Confirm with a passkey' })).toBeVisible();

		// A half-typed code is not a proof: the server's floor is six digits.
		const disableButton = mfa.getByRole('button', { name: 'Disable two-factor' });
		await disableCode.fill('12345');
		await expect(disableButton).toBeDisabled();
		await disableCode.fill('123456');
		await expect(disableButton).toBeEnabled();
		expect(await bodySentBy(page, disableButton, 'POST', '/api/auth/mfa/disable')).toEqual({
			code: '123456'
		});

		// --- Passkeys card: one step-up field for add and remove ---
		const passkeys = await openCard(page, 'Passkeys');
		const stepUpCode = passkeys.getByLabel(
			'Enter a current authenticator code to add or remove a passkey'
		);
		await expect(stepUpCode).toBeVisible();
		await expect(passkeys.locator('input[type="password"]')).toHaveCount(0);
		await expect(
			passkeys.getByText('Leave this blank to confirm with one of your existing passkeys instead.')
		).toBeVisible();

		await stepUpCode.fill('654321');
		const add = passkeys.getByRole('button', { name: 'Add a passkey' });
		expect(await bodySentBy(page, add, 'POST', '/api/auth/mfa/passkey/register')).toEqual({
			code: '654321'
		});
	});

	test('passkey only: no field at all — the passkey ceremony is the proof', async ({ page }) => {
		await stubFactors(page, { passwordClosed: true, totp: false, passkeys: 1 });
		await refuse(page, 'POST', '/api/auth/mfa/step-up/passkey');

		const passkeys = await openCard(page, 'Passkeys');
		await expect(
			passkeys.getByText("You'll confirm with one of your existing passkeys.")
		).toBeVisible();
		await expect(passkeys.getByText('Laptop 1')).toBeVisible();
		await expect(passkeys.locator('input[type="password"]')).toHaveCount(0);
		await expect(passkeys.locator('input[autocomplete="one-time-code"]')).toHaveCount(0);

		const add = passkeys.getByRole('button', { name: 'Add a passkey' });
		await expect(add).toBeEnabled();
		// The card goes straight to minting a step-up challenge for this operation.
		expect(await bodySentBy(page, add, 'POST', '/api/auth/mfa/step-up/passkey')).toEqual({
			operation: 'passkey_register'
		});
	});

	test('no factor yet: nothing to step up from, so no proof is asked for', async ({ page }) => {
		// A first factor needs no step-up in any tenant — the server skips the
		// gate when nothing is live — so an SSO-only member with neither TOTP nor
		// a passkey can add either, and the page must not invent a prompt.
		await stubFactors(page, { passwordClosed: true, totp: false, passkeys: 0 });
		await refuse(page, 'POST', '/api/auth/mfa/passkey/register');

		const passkeys = await openCard(page, 'Passkeys');
		await expect(passkeys.getByText('No passkeys yet')).toBeVisible();
		await expect(passkeys.locator('input[type="password"]')).toHaveCount(0);
		await expect(passkeys.locator('input[autocomplete="one-time-code"]')).toHaveCount(0);
		await expect(passkeys.getByText('confirm with one of your existing passkeys')).toHaveCount(0);
		await expect(passkeys.getByText('signs in with single sign-on')).toHaveCount(0);

		const add = passkeys.getByRole('button', { name: 'Add a passkey' });
		expect(await bodySentBy(page, add, 'POST', '/api/auth/mfa/passkey/register')).toEqual({});

		// Read last, not in the middle: this one lives in another panel, and
		// leaving the Passkeys panel before the click above would have taken the
		// button being clicked out of the document. Order carries no meaning
		// here — both are independent reads of one stubbed state.
		const mfa = await openCard(page, 'Two-factor authentication');
		await expect(mfa.getByRole('button', { name: /Set up two-factor/ })).toBeVisible();
	});
});

test.describe('/profile — step-up where the password is still a proof', () => {
	test('TOTP + passkey: both cards keep the password field', async ({ page }) => {
		await stubFactors(page, { passwordClosed: false, totp: true, passkeys: 1 });
		await refuse(page, 'POST', '/api/auth/mfa/disable');

		// Each card is finished with before the other panel is opened: a locator
		// from a panel that is no longer showing resolves to nothing.
		const mfa = await openCard(page, 'Two-factor authentication');
		const disablePassword = mfa.getByLabel('Enter your password to disable MFA');
		await expect(disablePassword).toHaveAttribute('type', 'password');
		await expect(mfa.getByText('signs in with single sign-on')).toHaveCount(0);

		await disablePassword.fill('not-the-password');
		const disable = mfa.getByRole('button', { name: 'Disable two-factor' });
		expect(await bodySentBy(page, disable, 'POST', '/api/auth/mfa/disable')).toEqual({
			password: 'not-the-password'
		});

		const passkeys = await openCard(page, 'Passkeys');
		await expect(
			passkeys.getByLabel('Confirm your password to add or remove a passkey')
		).toHaveAttribute('type', 'password');
		await expect(passkeys.locator('input[autocomplete="one-time-code"]')).toHaveCount(0);
	});
});
