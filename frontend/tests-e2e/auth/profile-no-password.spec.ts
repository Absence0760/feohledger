import type { Locator, Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * `/profile`'s Password card for an account that has no password at all —
 * every member JIT-provisioned by OIDC/SAML or created by SCIM
 * (`identity_provisioning.py`, `api/scim.py`; `User.hashed_password` is
 * `None`). `PATCH /api/auth/me` refused such an account's "current password"
 * with the same "Current password is incorrect" a WRONG password gets, so
 * the form was a control that could never succeed — see docs/followups.md.
 *
 * `/auth/me` now carries `has_password`, a property of the ACCOUNT rather
 * than the org, and the page reads it to replace the form with a sentence.
 * This is distinct from `password_sign_in_closed` (the org has closed
 * password sign-in) covered by `profile-sso-only-step-up.spec.ts`: an
 * account can have no password in an org that has NOT closed password
 * sign-in, and an account CAN have a password in an org that has —
 * rotation stays available there, with a note, rather than disappearing.
 *
 * `/api/auth/me` is stubbed the same way as the sibling spec: the real
 * response, with just the fields under test overridden.
 */

async function stubMe(page: Page, overrides: Record<string, unknown>): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/auth/me',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			await route.fulfill({ response: real, json: { ...body, ...overrides } });
		}
	);
}

function passwordCard(page: Page): Locator {
	return page
		.locator('section.card')
		.filter({ has: page.getByRole('heading', { name: 'Password' }) });
}

async function openPasswordSection(page: Page): Promise<Locator> {
	await page.goto('/profile?section=password');
	const card = passwordCard(page);
	await expect(card).toBeVisible();
	return card;
}

test.describe('/profile — Password card, an account with no password', () => {
	test('the form is replaced by a sentence', async ({ page }) => {
		await stubMe(page, { has_password: false, password_sign_in_closed: false });

		const card = await openPasswordSection(page);

		await expect(
			card.getByText('This account signs in with single sign-on and has no password to change.')
		).toBeVisible();
		await expect(card.locator('form')).toHaveCount(0);
		await expect(card.locator('input[type="password"]')).toHaveCount(0);
		// The note for the OTHER case (has a password, but the org has closed
		// sign-in) must not also render here — the two are distinct states.
		await expect(card.getByText("isn't used to sign in")).toHaveCount(0);
	});

	test('no password AND the org has closed password sign-in: still just the sentence', async ({
		page
	}) => {
		// Both conditions true at once is a real shape (an SSO/SCIM-provisioned
		// member of an SSO-only org) — the account-level sentence wins, since a
		// dead form is dead regardless of why.
		await stubMe(page, { has_password: false, password_sign_in_closed: true });

		const card = await openPasswordSection(page);

		await expect(
			card.getByText('This account signs in with single sign-on and has no password to change.')
		).toBeVisible();
		await expect(card.locator('form')).toHaveCount(0);
	});
});

test.describe('/profile — Password card, a password the org no longer uses for sign-in', () => {
	test('rotation stays available, with a note', async ({ page }) => {
		await stubMe(page, { has_password: true, password_sign_in_closed: true });

		const card = await openPasswordSection(page);

		await expect(
			card.getByText(
				"Your organization currently requires single sign-on, so this password isn't used to sign in."
			)
		).toBeVisible();
		await expect(card.locator('form')).toHaveCount(1);
		await expect(card.locator('input[type="password"]')).toHaveCount(3);

		// Submit is enabled whatever is typed: a submit says what is wrong.
		await expect(card.getByRole('button', { name: 'Change password' })).toBeEnabled();
	});
});

test.describe('/profile — Password card, the ordinary case', () => {
	test('an ordinary account gets the plain form and no SSO sentence', async ({ page }) => {
		await stubMe(page, { has_password: true, password_sign_in_closed: false });

		const card = await openPasswordSection(page);

		await expect(card.locator('form')).toHaveCount(1);
		await expect(card.locator('input[type="password"]')).toHaveCount(3);
		await expect(card.getByText('single sign-on')).toHaveCount(0);
	});
});

test.describe('/profile — Password card, field-level errors and the toggle', () => {
	// #635: the card uses the change-password page's PasswordInput +
	// NewPasswordFields — Submit stays enabled, each error is beside (and read
	// with) its field, and the server's refusal lands on the field it is about.
	test('a submit marks each bad field, and nothing is sent', async ({ page }) => {
		await stubMe(page, { has_password: true, password_sign_in_closed: false });
		const card = await openPasswordSection(page);

		const current = card.getByLabel('Current password', { exact: true });
		const newPw = card.getByLabel('New password', { exact: true });
		const confirm = card.getByLabel('Confirm new password', { exact: true });
		const submit = card.getByRole('button', { name: 'Change password' });

		let patched = 0;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname === '/api/auth/me' && r.method() === 'PATCH') patched += 1;
		});

		await submit.click();
		await expect(current).toBeFocused();
		await expect(current).toHaveAttribute('aria-invalid', 'true');
		await expect(current).toHaveAccessibleDescription('Enter your current password.');
		await expect(newPw).toHaveAttribute('aria-invalid', 'true');
		await expect(confirm).toHaveAttribute('aria-invalid', 'true');

		// The old form took six characters; the server has always wanted the
		// complexity rules, which the checklist now states up front.
		await current.fill('whatever-it-still-is');
		await newPw.fill('a-new-password-1');
		await submit.click();
		await expect(newPw).toBeFocused();
		await expect(card.getByText('Password does not meet the complexity requirements.')).toBeVisible();
		await expect(card.locator('ul.strength li')).toHaveCount(4);

		await newPw.fill('A-New-Password-1');
		await confirm.fill('A-New-Password-2');
		await submit.click();
		await expect(confirm).toBeFocused();
		await expect(confirm).toHaveAccessibleDescription('Passwords do not match.');
		expect(patched).toBe(0);
	});

	test('a wrong current password is named beside its field', async ({ page }) => {
		// A real 400 from `PATCH /api/auth/me`: it changes nothing and that
		// route keeps no failure budget, so the worker's admin is unaffected.
		await stubMe(page, { has_password: true, password_sign_in_closed: false });
		const card = await openPasswordSection(page);

		const current = card.getByLabel('Current password', { exact: true });
		await current.fill('not-the-current-password');
		await card.getByLabel('New password', { exact: true }).fill('A-New-Password-1');
		await card.getByLabel('Confirm new password', { exact: true }).fill('A-New-Password-1');
		const refused = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/auth/me' && r.request().method() === 'PATCH'
		);
		await card.getByRole('button', { name: 'Change password' }).click();
		expect((await refused).status()).toBe(400);

		await expect(current).toBeFocused();
		await expect(current).toHaveAttribute('aria-invalid', 'true');
		await expect(current).toHaveAccessibleDescription("That isn't your current password.");
		await current.fill('typing-again');
		await expect(current).not.toHaveAttribute('aria-invalid', 'true');
	});

	test('each field has the show-password toggle', async ({ page }) => {
		await stubMe(page, { has_password: true, password_sign_in_closed: false });
		const card = await openPasswordSection(page);

		const toggles = card.getByRole('button', { name: 'Show password', exact: true });
		await expect(toggles).toHaveCount(3);
		const current = card.getByLabel('Current password', { exact: true });
		await current.fill('something');
		await toggles.first().click();
		await expect(toggles.first()).toHaveAttribute('aria-pressed', 'true');
		await expect(current).toHaveAttribute('type', 'text');
		await expect(current).toHaveAttribute('autocomplete', 'current-password');
	});
});
