import { expect, signInAndWait, test } from '../fixtures/helpers';
import { clearNotifications, currentUserId, orgId, seedNotifications } from './seed';

test.describe('notification center', () => {
	test('badge shows unread count and the center lists notifications', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		const org = orgId();
		clearNotifications(me);
		seedNotifications(me, org, 3);

		await page.goto('/notifications');
		// The center lists the seeded rows.
		await expect(page.getByText('E2E notification').first()).toBeVisible();

		// Sidebar badge reflects unread count (poll fires on mount).
		const badge = page.locator('.bell-badge');
		await expect(badge).toHaveText('3', { timeout: 15_000 });

		clearNotifications(me);
	});

	test('mark one read decrements the badge and updates the row', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		const org = orgId();
		clearNotifications(me);
		seedNotifications(me, org, 2);

		await page.goto('/notifications');
		await expect(page.locator('.bell-badge')).toHaveText('2', { timeout: 15_000 });

		// Open the first notification's RowLink — marks read, then navigates to the invoice.
		await page.getByRole('button', { name: /^Open E2E notification/ }).first().click();
		await expect(page).toHaveURL(/\/invoices\?id=/);

		// Back on the center, badge is now 1.
		await page.goto('/notifications');
		await expect(page.locator('.bell-badge')).toHaveText('1', { timeout: 15_000 });

		clearNotifications(me);
	});

	test('mark all read clears the badge', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		const org = orgId();
		clearNotifications(me);
		seedNotifications(me, org, 4);

		await page.goto('/notifications');
		await expect(page.locator('.bell-badge')).toHaveText('4', { timeout: 15_000 });

		await page.getByRole('button', { name: 'Mark all read' }).click();
		// Badge disappears (unread === 0 renders no badge).
		await expect(page.locator('.bell-badge')).toHaveCount(0, { timeout: 15_000 });

		clearNotifications(me);
	});

	test('unread filter and empty state', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		clearNotifications(me);

		await page.goto('/notifications');
		// No notifications → empty state.
		await expect(page.getByText('No notifications yet.')).toBeVisible();

		// Unread filter on an empty inbox shows its own empty message.
		await page.getByRole('button', { name: /^Unread/ }).click();
		await expect(page.getByText('No unread notifications.')).toBeVisible();
	});
});
