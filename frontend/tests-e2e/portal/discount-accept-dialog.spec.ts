import type { Page } from '@playwright/test';

import { acceptConsent, expect, test } from '../fixtures/helpers';

/**
 * /portal/discount-offers — the Accept dialog runs on the shared `ui/Modal`,
 * so it owes the supplier what every other dialog does: Esc closes it and
 * focus goes back to the row's Accept button that opened it (not to <body>,
 * which drops a keyboard user at the top of the page), and a refused accept
 * is reported INSIDE the dialog, where the supplier is looking, with the
 * dialog left open and their tier choice intact.
 *
 * Sign-in is real (the seeded `supplier@portal.test` VendorUser, as in
 * portal.spec.ts); the offer list and the accept call are stubbed, so the
 * spec neither depends on nor mutates seeded discount offers.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

test.use({ storageState: { cookies: [], origins: [] } });

const tiers = [
	{ days: 10, percent: 2, savings: 20 },
	{ days: 20, percent: 1, savings: 10 }
];

function offer(id: string, invoiceNumber: string) {
	return {
		id,
		status: 'offered',
		scope: 'invoice',
		invoice_id: null,
		invoice_number: invoiceNumber,
		base_amount: 1000,
		currency: 'USD',
		tiers,
		best_tier: tiers[0],
		valid_from: '2026-01-01',
		valid_until: '2099-12-31',
		accepted_tier: null,
		accepted_at: null,
		captured_amount: null,
		captured_at: null,
		notes: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

const OFFERS = [
	offer('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', 'DISC-STUB-1'),
	offer('bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', 'DISC-STUB-2')
];

async function openOffers(page: Page) {
	await page.route(
		(url) => url.pathname === '/api/portal/discount-offers',
		(route) =>
			route.request().method() === 'GET'
				? route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({ items: OFFERS, total: OFFERS.length, page: 1, page_size: 20 })
					})
				: route.fallback()
	);
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
	await page.goto('/portal/discount-offers');
	await expect(page.locator('table tbody tr', { hasText: 'DISC-STUB-2' })).toBeVisible();
}

test.describe('/portal/discount-offers accept dialog', () => {
	test('Esc closes the dialog and returns focus to the row’s Accept button', async ({ page }) => {
		await openOffers(page);

		// The SECOND row, so a focus restore that lands on the first Accept
		// (or anywhere else) fails rather than passing by coincidence.
		const accept = page.getByRole('button', { name: 'Accept for Invoice DISC-STUB-2' });
		await accept.click();

		const dialog = page.getByRole('dialog', { name: 'Accept early-payment discount' });
		await expect(dialog).toBeVisible();
		await dialog.getByLabel('Choose a tier').selectOption('20');
		await expect(dialog.getByText('You save')).toBeVisible();

		await page.keyboard.press('Escape');
		await expect(dialog).toBeHidden();
		await expect(accept).toBeFocused();
	});

	test('a refused accept is reported inside the dialog, which stays open', async ({ page }) => {
		await page.route(
			(url) => /^\/api\/portal\/discount-offers\/[^/]+\/accept$/.test(url.pathname),
			(route) =>
				route.fulfill({
					status: 409,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'This offer has expired.' })
				})
		);
		await openOffers(page);

		await page.getByRole('button', { name: 'Accept for Invoice DISC-STUB-1' }).click();
		const dialog = page.getByRole('dialog', { name: 'Accept early-payment discount' });
		await dialog.getByLabel('Choose a tier').selectOption('20');
		await dialog.getByRole('button', { name: 'Accept discount' }).click();

		await expect(dialog.getByRole('alert')).toHaveText('This offer has expired.');
		await expect(dialog).toBeVisible();
		// The tier the supplier chose survives the failure.
		await expect(dialog.getByLabel('Choose a tier')).toHaveValue('20');
		// The button is usable again for a retry, not stuck on "Accepting…".
		await expect(dialog.getByRole('button', { name: 'Accept discount' })).toBeEnabled();
	});
});
