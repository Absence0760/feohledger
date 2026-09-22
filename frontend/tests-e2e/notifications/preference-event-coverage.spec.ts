import { expect, signInAndWait, test } from '../fixtures/helpers';

type Page = import('@playwright/test').Page;

/**
 * The `/profile` preference grid must offer a toggle for EVERY notifiable
 * event, not just the four `invoice_*` ones.
 *
 * `notification_dispatch.resolve_prefs` defaults a *missing* preference key to
 * **on**, so an event with no row is an event the user is subscribed to and
 * cannot unsubscribe from. `chat_message` is the one that bit: it emails the
 * AP team on every supplier-portal message.
 *
 * Companion guards:
 *   - `src/lib/types/notification.roster.test.ts` — the frontend list vs the
 *     backend's `NOTIFICATION_EVENT_TYPES` (reads the Python source).
 *   - `preferences.spec.ts` — round-trip persistence, on `invoice_paid`.
 */

/** The In-app + Email checkboxes for one event row on /profile. */
function prefRow(page: Page, eventLabel: string) {
	return {
		inApp: page.getByRole('checkbox', { name: `In-app notifications for ${eventLabel}` }),
		email: page.getByRole('checkbox', { name: `Email notifications for ${eventLabel}` })
	};
}

/** The three events that shipped server-side with no UI toggle. */
const NEW_EVENT_LABELS = [
	'Contract renewal due',
	'New supplier chat message',
	'Projected cash shortfall'
];

test.describe('notification preference event coverage', () => {
	test('every notifiable event has a labelled in-app + email toggle', async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/profile?section=notifications');

		// The four that always shipped, unchanged.
		for (const label of [
			'Invoice assigned to me',
			'Invoice approved',
			'Invoice rejected',
			'Invoice paid'
		]) {
			const row = prefRow(page, label);
			await expect(row.inApp, `${label} in-app toggle`).toBeVisible();
			await expect(row.email, `${label} email toggle`).toBeVisible();
		}

		// The three that were missing.
		for (const label of NEW_EVENT_LABELS) {
			const row = prefRow(page, label);
			await expect(row.inApp, `${label} in-app toggle`).toBeVisible();
			await expect(row.email, `${label} email toggle`).toBeVisible();
			// Opt-out, not opt-in: both channels start on, matching resolve_prefs.
			await expect(row.inApp, `${label} in-app default`).toBeChecked();
			await expect(row.email, `${label} email default`).toBeChecked();
		}
	});

	test('muting supplier chat email sends that event key to the preferences API', async ({
		page
	}) => {
		await signInAndWait(page);
		await page.goto('/profile?section=notifications');

		const chat = prefRow(page, 'New supplier chat message');
		await expect(chat.email).toBeChecked();

		// Arm before clicking so the PATCH can't be missed. This asserts the
		// FRONTEND contract — that the grid now transmits a mute for
		// `chat_message` at all — which is exactly what was impossible before,
		// and is the half this workspace owns.
		const patch = page.waitForRequest(
			(r) =>
				r.method() === 'PATCH' &&
				new URL(r.url()).pathname === '/api/notifications/preferences',
			{ timeout: 15_000 }
		);

		// `.click()`, not `.uncheck()`: uncheck asserts the box is unchecked the
		// moment it returns, which races the re-render the PATCH response
		// triggers. The request body below is the deterministic signal.
		await chat.email.click();

		const request = await patch;
		const body = request.postDataJSON() as Record<string, { email: boolean; in_app: boolean }>;
		expect(Object.keys(body), 'the PATCH targets only the toggled event').toEqual([
			'chat_message'
		]);
		expect(body.chat_message).toEqual({ email: false, in_app: true });
		await request.response();

		// The card survives the round-trip — the grid renders from a normalised
		// map, so a response that omits newer events degrades to "shown as on"
		// instead of throwing on an undefined row and blanking the whole
		// Notifications section.
		await expect(chat.inApp).toBeVisible();
		await expect(prefRow(page, 'Invoice paid').email).toBeVisible();

		// Restore the shared seeded user's default. Reload first so the state
		// read is settled rather than mid-re-render; a no-op when the server
		// didn't store the change.
		await page.reload();
		const restored = prefRow(page, 'New supplier chat message');
		await expect(restored.email).toBeVisible();
		if (!(await restored.email.isChecked())) {
			const restorePatch = page.waitForRequest(
				(r) =>
					r.method() === 'PATCH' &&
					new URL(r.url()).pathname === '/api/notifications/preferences',
				{ timeout: 15_000 }
			);
			await restored.email.click();
			await (await restorePatch).response();
		}
	});
});
