import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` → Passkeys, when the passkey list can't be loaded.
 *
 * `GET /api/auth/mfa/passkey` failing used to be swallowed into
 * `passkeys = []`, which is a *claim*: the card said "No passkeys yet", and —
 * worse — the step-up decision is built on that same array. With
 * `needsPasskeyStepUp` reading false, Add/Remove submitted with no proof at
 * all, and `POST /auth/mfa/passkey/register` answered the opaque step-up 400
 * for any account that actually does hold a factor.
 *
 * The unknown list now stays unknown: the card says so and offers a retry, and
 * the step-up requirement fails CLOSED (a proof is asked for, and the passkey
 * ceremony stays on offer because the server — not this list — owns the
 * credential set).
 */
test.describe('/profile — passkey list failure', () => {
	test('an unloadable passkey list reports itself instead of claiming "none"', async ({
		page
	}) => {
		let fail = true;
		await page.route('**/api/auth/mfa/passkey', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			if (fail) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify([])
			});
		});

		await page.goto('/profile?section=passkeys');

		const card = page.locator('section.card', { hasText: 'Passkeys' }).first();
		await expect(card.getByText(/Couldn't load your passkeys/)).toBeVisible();
		await expect(card.getByText('No passkeys yet')).toHaveCount(0);

		// Retry succeeds → the honest empty state finally appears.
		fail = false;
		await card.getByRole('button', { name: 'Try again' }).click();
		await expect(card.getByText('No passkeys yet')).toBeVisible();
		await expect(card.getByText(/Couldn't load your passkeys/)).toHaveCount(0);
	});
});
