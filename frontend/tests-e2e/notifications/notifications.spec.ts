import type { NotificationListResponse } from '#lib/types/notification.ts';

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

	test('over 99 unread, the bell names what its badge shows (WCAG 2.5.3)', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		const org = orgId();
		clearNotifications(me);
		seedNotifications(me, org, 120);

		await page.goto('/notifications');
		await expect(page.locator('.bell-badge')).toHaveText('99+', { timeout: 15_000 });
		// The accessible name carries the visible text, as label-in-name requires;
		// it used to say "120 unread" beside a badge reading "99+".
		await expect(page.locator('.bell-btn')).toHaveAccessibleName('Notifications, 99+ unread');

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

	test('the Unread filter round-trips through the URL', async ({ page }) => {
		await signInAndWait(page);
		const me = await currentUserId(page);
		clearNotifications(me);

		await page.goto('/notifications');
		await expect(page.getByText('No notifications yet.')).toBeVisible();

		// Pressing the chip writes the view into the address bar...
		await page.getByRole('button', { name: /^Unread/ }).click();
		await expect(page).toHaveURL(/[?&]filter=unread\b/);

		// ...so a reload lands back on it instead of resetting to All.
		await page.reload();
		await expect(page.getByText('No unread notifications.')).toBeVisible();
		await expect(page.getByRole('button', { name: /^Unread/ })).toHaveAttribute('aria-pressed', 'true');

		// And All clears it again.
		await page.getByRole('button', { name: /^All/ }).click();
		await expect(page).not.toHaveURL(/filter=/);
		await expect(page.getByText('No notifications yet.')).toBeVisible();
	});

	test('an unread row with nowhere to go can be marked read on its own', async ({ page }) => {
		const id = '00000000-0000-0000-0000-0000000ca5f3';
		const list: NotificationListResponse = {
			items: [
				{
					id,
					event_type: 'contract_renewal_due',
					entity_type: 'contract',
					entity_id: null,
					title: 'Unread orphan spec row',
					body: null,
					read_at: null,
					created_at: '2026-01-01T00:00:00Z'
				}
			],
			total: 1,
			unread: 1,
			page: 1,
			page_size: 20
		};
		await signInAndWait(page);
		await page.route(
			(url) => url.pathname === '/api/notifications',
			(route) => route.fulfill({ json: list })
		);
		await page.route(
			(url) => url.pathname === `/api/notifications/${id}/read`,
			(route) => route.fulfill({ json: {} })
		);
		await page.goto('/notifications');

		const row = page.locator('tbody tr', { hasText: 'Unread orphan spec row' });
		await expect(row).toHaveClass(/unread-row/);
		const marked = page.waitForRequest(
			(r) => r.method() === 'POST' && new URL(r.url()).pathname === `/api/notifications/${id}/read`
		);
		await page.getByRole('button', { name: 'Mark read: Unread orphan spec row' }).click();
		await marked;
		await expect(row).not.toHaveClass(/unread-row/);
		await expect(row.getByRole('button', { name: /^Mark read:/ })).toHaveCount(0);
	});

	test('a non-invoice row opens its own screen; a row with nowhere to go is not a link', async ({
		page
	}) => {
		// Stubbed by EXACT pathname (tests-e2e/README.md § Stubbing an API route)
		// so `/api/notifications/unread-count` still reaches the backend.
		const read = '2026-01-01T00:00:00Z';
		const list: NotificationListResponse = {
			items: [
				{
					id: '00000000-0000-0000-0000-0000000ca5f1',
					event_type: 'cash_shortfall_projected',
					entity_type: 'cash_position',
					entity_id: null,
					title: 'Projected shortfall spec row',
					body: null,
					read_at: read,
					created_at: read
				},
				{
					id: '00000000-0000-0000-0000-0000000ca5f2',
					event_type: 'contract_renewal_due',
					entity_type: 'contract',
					entity_id: null,
					title: 'Orphaned contract spec row',
					body: null,
					read_at: read,
					created_at: read
				}
			],
			total: 2,
			unread: 0,
			page: 1,
			page_size: 20
		};
		await signInAndWait(page);
		await page.route(
			(url) => url.pathname === '/api/notifications',
			(route) => route.fulfill({ json: list })
		);
		await page.goto('/notifications');

		// The contract row has no id, so no route: plain text, no open control.
		await expect(page.getByText('Orphaned contract spec row')).toBeVisible();
		await expect(page.getByRole('button', { name: /^Open Orphaned contract spec row/ })).toHaveCount(0);
		await expect(page.locator('tr.clickable', { hasText: 'Orphaned contract spec row' })).toHaveCount(0);

		// Both rows are read, so neither offers a per-row Mark read.
		await expect(page.getByRole('button', { name: /^Mark read:/ })).toHaveCount(0);

		// The shortfall alert is org-wide (entity_id NULL) and opens the forecast.
		await page.getByRole('button', { name: /^Open Projected shortfall spec row/ }).click();
		await expect(page).toHaveURL(/\/cash-flow$/);
	});
});
