import type { Page } from '@playwright/test';

import type { DashboardDiscountCapture } from '#lib/types/analytics.ts';

import { expect, test } from '../fixtures/helpers';
import { dashboardResponse } from './fixture';

/**
 * Dashboard — early-payment discount capture.
 *
 * `GET /api/dashboard`'s `discount_capture` block has carried a three-way
 * captured / missed / PENDING fold plus `*_amount_reporting` +
 * `unconverted_count` since round 16 with NO frontend consumer at all. These
 * specs pin the two things that make surfacing it honest rather than harmful:
 *
 *   1. a still-open discount window renders as capturable, never as a miss;
 *   2. a figure that could not convert some rows SAYS so where it is read.
 *
 * The dashboard response is stubbed so both states are actually on screen —
 * a seeded tenant reliably produces neither.
 *
 * The payload itself lives in `./fixture.ts`, typed `satisfies DashboardData`.
 * The copy this spec used to hand-maintain omitted `aging_reporting`'s
 * `unconverted_count` and nothing noticed, because `pnpm check` does not
 * typecheck `tests-e2e/`; `pnpm check:e2e` does. See that module's header.
 */

/**
 * Both chart series are left EMPTY on purpose: this spec owns the discount
 * card, and `unconverted-disclosures.spec.ts` owns the three chart notices.
 * Every other count in the fixture is zero, so neither those notices nor the
 * KPI-row rollup banner can fire and be mistaken for this card's own.
 */
async function stubDashboard(page: Page, discount: Partial<DashboardDiscountCapture>) {
	await page.route('**/api/dashboard*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				dashboardResponse({
					vendorSpend: [],
					monthlyTrend: [],
					discount: {
						eligible_count: 3,
						captured_count: 1,
						missed_count: 1,
						pending_count: 1,
						captured_amount: 100,
						missed_amount: 50,
						pending_amount: 25,
						captured_amount_reporting: 100,
						missed_amount_reporting: 50,
						pending_amount_reporting: 25,
						capture_rate_pct: 50,
						insufficient_data: false,
						...discount
					}
				})
			)
		})
	);
}

test('renders the three-way fold — a still-open window is capturable, not missed', async ({
	page
}) => {
	await stubDashboard(page, {});
	await page.goto('/');

	const card = page.getByTestId('discount-capture');
	await expect(card).toBeVisible({ timeout: 15_000 });

	// Pending is its OWN bucket. Folding it into "missed" would report live
	// opportunity as forgone savings.
	await expect(card).toContainText('Captured');
	await expect(card).toContainText('Missed');
	await expect(card).toContainText('Still capturable');
	await expect(card.locator('.discount-row')).toHaveCount(3);
	await expect(page.getByTestId('discount-capture-rate')).toContainText('50%');
});

test('a partial figure says it is partial, at the point of reading', async ({ page }) => {
	await stubDashboard(page, { unconverted_count: 2 });
	await page.goto('/');

	const notice = page.getByTestId('discount-capture-unconverted');
	await expect(notice).toBeVisible({ timeout: 15_000 });
	await expect(notice).toHaveAttribute('role', 'alert');
	await expect(notice).toContainText('2');
	await expect(notice).toContainText('USD');

	// The KPI card's qualifier line carries the same disclosure — it outranks
	// the capture rate there, because a rate is context while an unconverted
	// count means the headline mixes currencies.
	const kpi = page.locator('.kpi', { hasText: 'Discounts captured' });
	await expect(kpi.locator('.kpi-sub')).toContainText('face value');
});

test('nothing decided yet reports no rate, never 0%', async ({ page }) => {
	await stubDashboard(page, {
		captured_count: 0,
		missed_count: 0,
		pending_count: 3,
		captured_amount_reporting: 0,
		missed_amount_reporting: 0,
		capture_rate_pct: null,
		insufficient_data: true
	});
	await page.goto('/');

	// "We have not missed a discount yet" and "we captured none of the
	// discounts we could have" are opposite facts; 0% reads as the bad one.
	const rate = page.getByTestId('discount-capture-rate');
	await expect(rate).toBeVisible({ timeout: 15_000 });
	await expect(rate).toContainText('No discount window has closed yet');
	await expect(rate).not.toContainText('0%');
});

test('no eligible invoices renders the empty state, not a row of zeros', async ({ page }) => {
	await stubDashboard(page, {
		eligible_count: 0,
		captured_count: 0,
		missed_count: 0,
		pending_count: 0,
		captured_amount_reporting: 0,
		missed_amount_reporting: 0,
		pending_amount_reporting: 0,
		capture_rate_pct: null,
		insufficient_data: true
	});
	await page.goto('/');

	const card = page.getByTestId('discount-capture');
	await expect(card).toContainText('No invoices carried an early-payment discount.', {
		timeout: 15_000
	});
	await expect(card.locator('.discount-row')).toHaveCount(0);
	// And no KPI card claiming a captured figure of zero.
	await expect(page.locator('.kpi', { hasText: 'Discounts captured' })).toHaveCount(0);
});
