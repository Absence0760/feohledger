import type { NotificationListResponse } from '#lib/types/notification.ts';

import { expect, test } from '../fixtures/helpers';

/**
 * The sidebar notification bell's popover (`layout/NotificationBell.svelte`),
 * issue #635 finding 22:
 *
 * - a failed list load said "No notifications yet." — an empty inbox and a
 *   broken fetch read the same;
 * - the popover is `role="dialog"` but focus stayed on the bell, so a keyboard
 *   or screen-reader user opened a dialog they were not in;
 * - every string was an English literal although the catalogue had the keys.
 *
 * The list endpoint is stubbed by EXACT pathname (tests-e2e/README.md §
 * Stubbing an API route) — `/api/notifications/unread-count` and the module
 * `#lib/stores/notifications.svelte.ts` must not be answered by it.
 */

const LIST = (url: URL) => url.pathname === '/api/notifications';

/** One already-read contract notification — it navigates nowhere when opened. */
const CONTRACT_ITEM: NotificationListResponse = {
	items: [
		{
			id: '00000000-0000-0000-0000-00000000b311',
			event_type: 'contract_renewal_due',
			entity_type: 'contract',
			entity_id: null,
			title: 'Renewal due: bell spec contract',
			body: null,
			read_at: '2026-01-01T00:00:00Z',
			created_at: '2026-01-01T00:00:00Z'
		}
	],
	total: 1,
	unread: 0,
	page: 1,
	page_size: 20
};

test.describe('notification bell popover', () => {
	test('a failed load says so and offers Retry, not "No notifications yet"', async ({ page }) => {
		let fail = true;
		await page.route(LIST, async (route) => {
			if (fail) return route.fulfill({ status: 500, json: { detail: 'boom' } });
			return route.fallback();
		});
		await page.goto('/');
		await page.locator('.bell-btn').click();

		const popover = page.getByRole('dialog', { name: 'Notifications' });
		const error = popover.getByTestId('bell-error');
		await expect(error).toContainText('Could not load notifications. Try again.');
		await expect(popover.getByText('No notifications yet.')).toHaveCount(0);

		// Retry reaches the real endpoint and the error goes.
		fail = false;
		const reloaded = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/notifications' && r.ok()
		);
		await error.getByRole('button', { name: 'Retry' }).click();
		await reloaded;
		await expect(popover.getByTestId('bell-error')).toHaveCount(0);
		// Focus did not fall to <body> when the Retry button unmounted.
		await expect(popover.getByRole('heading', { name: 'Notifications' })).toBeFocused();
	});

	test('opening moves focus into the dialog; Escape and the backdrop return it', async ({
		page
	}) => {
		await page.goto('/');
		const bell = page.locator('.bell-btn');
		await bell.click();

		const popover = page.getByRole('dialog', { name: 'Notifications' });
		await expect(popover.getByRole('heading', { name: 'Notifications' })).toBeFocused();

		await page.keyboard.press('Escape');
		await expect(popover).toHaveCount(0);
		await expect(bell).toBeFocused();

		await bell.click();
		await expect(popover.getByRole('heading', { name: 'Notifications' })).toBeFocused();
		await page.locator('.bell-backdrop').click({ position: { x: 600, y: 400 } });
		await expect(popover).toHaveCount(0);
		await expect(bell).toBeFocused();
	});

	test('renders in the reader’s locale (de)', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
		await page.route(LIST, (route) => route.fulfill({ status: 500, json: { detail: 'boom' } }));
		await page.goto('/');
		await page.locator('.bell-btn').click();

		// German values from i18n/locales/de.ts. Asserted first — the
		// auto-waiting signal that the catalogue landed — before any English
		// is asserted absent.
		const popover = page.getByRole('dialog', { name: 'Benachrichtigungen' });
		await expect(popover.getByTestId('bell-error')).toContainText(
			'Benachrichtigungen konnten nicht geladen werden'
		);
		await expect(popover.getByRole('button', { name: 'Erneut versuchen' })).toBeVisible();
		await expect(popover.getByRole('button', { name: 'Alle als gelesen markieren' })).toBeVisible();
		await expect(popover.getByRole('link', { name: 'Alle anzeigen' })).toBeVisible();
		await expect(popover.getByText('Mark all read')).toHaveCount(0);
		await expect(popover.getByText('View all')).toHaveCount(0);
	});

	test('a failed refresh over earlier rows keeps them and says they are not current', async ({
		page
	}) => {
		let fail = false;
		await page.route(LIST, (route) =>
			fail
				? route.fulfill({ status: 500, json: { detail: 'boom' } })
				: route.fulfill({ json: CONTRACT_ITEM })
		);
		await page.goto('/');
		const bell = page.locator('.bell-btn');
		const popover = page.getByRole('dialog', { name: 'Notifications' });

		await bell.click();
		await expect(popover.getByText('Renewal due: bell spec contract')).toBeVisible();
		await page.keyboard.press('Escape');

		fail = true;
		await bell.click();
		await expect(popover.getByTestId('bell-error')).toBeVisible();
		await expect(popover.getByText('Renewal due: bell spec contract')).toBeVisible();
	});

	test('opening an item that navigates nowhere returns focus to the bell', async ({ page }) => {
		await page.route(LIST, (route) => route.fulfill({ json: CONTRACT_ITEM }));
		await page.goto('/');
		const bell = page.locator('.bell-btn');
		await bell.click();
		await page.getByRole('button', { name: /Renewal due: bell spec contract/ }).click();
		await expect(page.getByRole('dialog', { name: 'Notifications' })).toHaveCount(0);
		await expect(bell).toBeFocused();
	});

	test('tabbing out of the popover closes it', async ({ page }) => {
		await page.route(LIST, (route) => route.fulfill({ json: CONTRACT_ITEM }));
		await page.goto('/');
		await page.locator('.bell-btn').click();
		const popover = page.getByRole('dialog', { name: 'Notifications' });
		const viewAll = popover.getByRole('link', { name: 'View all', exact: true });
		await expect(popover.getByText('Renewal due: bell spec contract')).toBeVisible();
		await viewAll.focus();
		// The next stop after "View all" is the bell itself (the popover precedes
		// it in the DOM), so step past the bell to leave both.
		await page.keyboard.press('Tab');
		await expect(page.locator('.bell-btn')).toBeFocused();
		await expect(popover).toBeVisible();
		await page.keyboard.press('Tab');
		await expect(popover).toHaveCount(0);
	});
});
