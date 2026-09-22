import { expect, signInAndWait, test } from '../fixtures/helpers';

type Page = import('@playwright/test').Page;

/** The In-app + Email checkboxes for a given event row on /profile. */
function prefRow(page: Page, eventLabel: string) {
	return {
		inApp: page.getByRole('checkbox', { name: `In-app notifications for ${eventLabel}` }),
		email: page.getByRole('checkbox', { name: `Email notifications for ${eventLabel}` })
	};
}

test.describe('notification preferences', () => {
	test('toggling a preference persists across reload', async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/profile?section=notifications');

		const row = prefRow(page, 'Invoice paid');
		// Defaults: both channels on.
		await expect(row.email).toBeChecked();
		await expect(row.inApp).toBeChecked();

		// Turn email off; the PATCH fires on change.
		await row.email.uncheck();
		await expect(row.email).not.toBeChecked();

		// Reload — the off state persisted server-side.
		await page.reload();
		const reloaded = prefRow(page, 'Invoice paid');
		await expect(reloaded.email).not.toBeChecked();
		// Other channel untouched.
		await expect(reloaded.inApp).toBeChecked();

		// Restore default so the shared seeded user isn't left mutated.
		await reloaded.email.check();
		await expect(reloaded.email).toBeChecked();
	});

	test('a sibling event is unaffected by another event toggle', async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/profile?section=notifications');

		const paid = prefRow(page, 'Invoice paid');
		const approved = prefRow(page, 'Invoice approved');

		await paid.inApp.uncheck();
		await expect(paid.inApp).not.toBeChecked();
		// invoice_approved stays at its default.
		await expect(approved.inApp).toBeChecked();
		await expect(approved.email).toBeChecked();

		// Restore.
		await paid.inApp.check();
	});
});
