import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * `/discounts` — Accept and Decline read the row's server verdict
 * (`actions.{accept,decline}`, backend `api/discounts.accept_refusal` /
 * `.decline_refusal`), never a restated `status === 'offered'` + role rule
 * (frontend/docs/ui-patterns.md § Whether to offer an action). And the accept
 * dialog offers only `capturable_tier_days` — the tiers `POST /accept` takes by
 * name TODAY — so a tier whose pay-by window has closed is not a choice that
 * can only 409.
 *
 * The page explains no refusal here (each is about the offer's state or the
 * reader's role): a refused control is hidden, and a row whose verdict has not
 * arrived offers neither (fail closed).
 *
 * Stubbed, exact-pathname guarded.
 */

const OFFERS = '/api/discounts/offers';
const DASHBOARD = '/api/discounts/dashboard';

const DASHBOARD_BODY = {
	captured_count: 0,
	captured_amount: 0,
	missed_count: 0,
	missed_amount: 0,
	capture_rate_pct: 0,
	open_offer_count: 1,
	projected_savings: 0,
	currency: 'USD',
	unconvertible_offer_count: 0,
	excluded_captured_count: 0,
	excluded_missed_count: 0
};

function refused(code: string, params: Record<string, unknown> = {}) {
	return { code, message: `Refused: ${code}`, params };
}

function offer(vendor: string, overrides: Record<string, unknown> = {}) {
	return {
		id: `d0000000-0000-4000-8000-${vendor.replace(/\D/g, '').padStart(12, '0')}`,
		scope: 'invoice',
		invoice_id: null,
		vendor_id: null,
		source: 'supplier',
		status: 'offered',
		// The 10-day tier is the richest, and its window has closed.
		tiers: [
			{ days: 10, percent: 2 },
			{ days: 20, percent: 1 },
			{ days: 30, percent: 0.5 }
		],
		base_amount: 10000,
		currency: 'USD',
		valid_from: null,
		valid_until: null,
		accepted_tier: null,
		accepted_at: null,
		captured_amount: null,
		captured_at: null,
		financing_provider: null,
		notes: null,
		created_at: '2026-11-01T10:00:00+00:00',
		updated_at: '2026-11-01T10:00:00+00:00',
		vendor_name: vendor,
		invoice_number: null,
		...overrides
	};
}

function json(route: Route, body: unknown) {
	return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stubDiscounts(page: Page, items: unknown[]) {
	await page.route('**/api/discounts/**', async (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname;
		if (req.method() === 'GET' && path === OFFERS) {
			return json(route, { items, total: items.length, page: 1, page_size: 20 });
		}
		if (req.method() === 'GET' && path === DASHBOARD) return json(route, DASHBOARD_BODY);
		return route.continue();
	});
}

function row(page: Page, vendor: string) {
	return page.locator('tbody tr', { hasText: vendor });
}

test.describe('/discounts — Accept / Decline follow the server verdict', () => {
	test('an allowed verdict offers Accept and Decline', async ({ page }) => {
		await stubDiscounts(page, [
			offer('Verdict Vendor 0001', {
				actions: { accept: null, decline: null },
				capturable_tier_days: [10, 20, 30]
			})
		]);
		await page.goto('/discounts');

		const r = row(page, 'Verdict Vendor 0001');
		await expect(r.getByRole('button', { name: /^Accept/ })).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Decline/ })).toBeVisible();
	});

	test('a refused Accept is hidden while an allowed Decline stays', async ({ page }) => {
		await stubDiscounts(page, [
			offer('Verdict Vendor 0002', {
				actions: { accept: refused('discount_offer_tiers_closed'), decline: null },
				capturable_tier_days: []
			})
		]);
		await page.goto('/discounts');

		const r = row(page, 'Verdict Vendor 0002');
		await expect(r.getByRole('button', { name: /^Decline/ })).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Accept/ })).toHaveCount(0);
		await expect(r).not.toContainText('Refused:');
	});

	test('a role refusal on an offered row hides both, where the old rule offered them', async ({
		page
	}) => {
		await stubDiscounts(page, [
			offer('Verdict Vendor 0003', {
				status: 'offered',
				actions: { accept: refused('role_not_permitted'), decline: refused('role_not_permitted') },
				capturable_tier_days: []
			})
		]);
		await page.goto('/discounts');

		const r = row(page, 'Verdict Vendor 0003');
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Accept/ })).toHaveCount(0);
		await expect(r.getByRole('button', { name: /^Decline/ })).toHaveCount(0);
	});

	test('a row whose verdict has not arrived offers neither (fail closed)', async ({ page }) => {
		await stubDiscounts(page, [offer('Verdict Vendor 0004')]);
		await page.goto('/discounts');

		const r = row(page, 'Verdict Vendor 0004');
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(r.getByRole('button', { name: /^Accept/ })).toHaveCount(0);
		await expect(r.getByRole('button', { name: /^Decline/ })).toHaveCount(0);
	});

	test('the accept dialog offers only the capturable tiers and sends the one picked', async ({
		page
	}) => {
		const target = offer('Verdict Vendor 0005', {
			actions: { accept: null, decline: null },
			capturable_tier_days: [20, 30]
		});
		await stubDiscounts(page, [target]);

		let acceptBody: unknown = null;
		await page.route(`**${OFFERS}/${target.id}/accept`, async (route) => {
			acceptBody = route.request().postDataJSON();
			return json(route, {
				...target,
				status: 'accepted',
				accepted_tier: { days: 20, percent: 1 },
				actions: { accept: refused('discount_offer_not_open', { status: 'accepted' }), decline: null },
				capturable_tier_days: []
			});
		});

		await page.goto('/discounts');
		await row(page, 'Verdict Vendor 0005')
			.getByRole('button', { name: /^Accept/ })
			.click({ timeout: 15_000 });

		const dialog = page.getByRole('dialog');
		const radios = dialog.locator('input[type="radio"][name="tier"]');
		await expect(radios).toHaveCount(2);
		await expect(dialog.locator('input[name="tier"][value="10"]')).toHaveCount(0);
		// Defaults to the richest tier the server will still take, not the
		// richest overall (the closed 10-day one).
		await expect(dialog.locator('input[name="tier"][value="20"]')).toBeChecked();

		await dialog.getByRole('button', { name: 'Accept offer' }).click();
		await expect.poll(() => acceptBody).toEqual({ tier_days: 20 });
	});

	test('a flat offer (no tiers) accepts as-is, with no tier picker', async ({ page }) => {
		await stubDiscounts(page, [
			offer('Verdict Vendor 0006', {
				tiers: [],
				actions: { accept: null, decline: null },
				capturable_tier_days: []
			})
		]);
		await page.goto('/discounts');
		await row(page, 'Verdict Vendor 0006')
			.getByRole('button', { name: /^Accept/ })
			.click({ timeout: 15_000 });

		const dialog = page.getByRole('dialog');
		await expect(dialog.locator('input[name="tier"]')).toHaveCount(0);
		await expect(dialog).toContainText('no sliding-scale tiers');
	});
});
