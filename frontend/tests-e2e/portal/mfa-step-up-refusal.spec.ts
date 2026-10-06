import type { Page } from '@playwright/test';

import { acceptConsent, expect, test } from '../fixtures/helpers';

/**
 * Supplier portal → Company → Two-factor: a refused step-up is stated in the
 * supplier's language.
 *
 * `POST /api/portal/auth/mfa/enroll` refuses a re-enrollment over a live factor
 * with a CODED 400 (`backend/app/api/portal_auth.py`,
 * `{code: "portal_step_up_failed", message, params}`). Two things used to go
 * wrong on the way to the screen: `portalApi.ts` threw
 * `new Error(body.detail || …)`, so an object detail rendered as
 * "[object Object]"; and even a string detail was the server's English inside a
 * German page. The e2e backend runs with MFA off, so the refusal is stubbed —
 * what is under test is how the page renders it; the server's verdict is
 * pytest's (`test_portal_mfa.py`).
 *
 * Auth mirrors `home.spec.ts`.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

const ENGLISH =
	'Confirm your password or a current authenticator code to change your two-factor settings.';

test.use({ storageState: { cookies: [], origins: [] } });

async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
}

async function refuseEnroll(page: Page, code: string, message: string): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/portal/auth/mfa/enroll',
		async (route) => {
			if (route.request().method() !== 'POST') return route.fallback();
			await route.fulfill({ status: 400, json: { detail: { code, message, params: {} } } });
		}
	);
}

test.describe('/portal/company — step-up refusal in the supplier’s language', () => {
	test.beforeEach(async ({ page }) => {
		// A returning German supplier: the picker's own storage key, set before
		// any app code runs.
		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await portalSignIn(page);
	});

	test('a coded refusal renders the German sentence — never English, never [object Object]', async ({
		page
	}) => {
		await refuseEnroll(page, 'portal_step_up_failed', ENGLISH);
		await page.goto('/portal/company');
		await page.getByRole('button', { name: 'Zwei-Faktor einrichten' }).click();

		const alert = page.getByRole('alert');
		await expect(alert).toContainText(
			'Bestätigen Sie Ihr Passwort oder einen aktuellen Code aus der Authenticator-App'
		);
		await expect(alert).not.toContainText('Confirm your password');
		await expect(alert).not.toContainText('object Object');
	});

	test("a code this build predates falls back to the server's English", async ({ page }) => {
		await refuseEnroll(page, 'portal_step_up_from_a_newer_backend', 'A newer English sentence.');
		await page.goto('/portal/company');
		await page.getByRole('button', { name: 'Zwei-Faktor einrichten' }).click();

		await expect(page.getByRole('alert')).toContainText('A newer English sentence.');
	});
});
