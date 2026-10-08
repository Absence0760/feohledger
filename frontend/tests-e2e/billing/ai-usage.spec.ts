import type {
	BillingAiUsage,
	BillingSpendCapResponse,
	BillingSubscriptionResponse
} from '#lib/types/billing.ts';

import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /billing — the AI-read invoice meter (decisions §253).
 *
 * The first test reads the REAL backend: an e2e tenant is seeded on `scale`
 * (every plan-gated surface the suite drives needs it — decisions §258) and
 * extracts through the `mock` reader, which is deliberately not a billable
 * provider — so its meter must read 0 of 3,000 no matter how many invoices the
 * rest of the suite has extracted. If `mock` ever started counting, the suite
 * would run the meter up and, on a Free tenant, trip the limit; this is the
 * canary.
 *
 * The other tests stub `GET /api/billing/subscription` (Free, and a paid plan
 * at its cap, are not states a seeded tenant is in) and the cap PUT, and
 * assert both what is drawn and the exact string the form sends.
 */

test.use({ storageState: { cookies: [], origins: [] } });

const isSubscription = (url: URL) => url.pathname === '/api/billing/subscription';
const isSpendCap = (url: URL) => url.pathname === '/api/billing/spending-cap';

function paidUsage(extra: Partial<BillingAiUsage> = {}): BillingAiUsage {
	return {
		period: '2026-10',
		used: 530,
		included: 500,
		overage_unit_price: '0.10',
		currency: 'USD',
		overage_units: 25,
		overage_amount: '2.50',
		projected_overage_amount: '2.50',
		spend_cap: '2.50',
		paused: true,
		pause_reason: 'spend_cap_reached',
		...extra
	} satisfies BillingAiUsage;
}

function freeUsage(): BillingAiUsage {
	return paidUsage({
		used: 0,
		included: 100,
		overage_unit_price: null,
		overage_units: 0,
		overage_amount: '0.00',
		projected_overage_amount: '0.00',
		spend_cap: null,
		paused: false,
		pause_reason: null
	});
}

function paidSubscription(
	ai: BillingAiUsage,
	plan: BillingSubscriptionResponse['plan'] = {
		code: 'growth',
		name: 'Growth',
		monthly_price: '49.00',
		currency: 'USD',
		entitlements: {},
		trial_days: 14
	}
): BillingSubscriptionResponse {
	return {
		provider: 'mock',
		plan,
		subscription: {
			status: 'active',
			current_period_start: '2026-10-01T00:00:00Z',
			current_period_end: '2026-11-01T00:00:00Z',
			trial_end: null,
			externally_managed: false
		},
		period: '2026-10',
		usage: { extractions: '530', extractions_platform: '530', ai_invoices: '530' },
		ai_usage: ai
	} satisfies BillingSubscriptionResponse;
}

test.beforeEach(async ({ page }) => {
	await signInAndWait(page);
});

test('the e2e tenant reads 0 of 3,000 — the mock reader never counts', async ({ page }) => {
	await page.goto('/billing');
	const panel = page.getByTestId('billing-ai-usage');
	await expect(panel.getByTestId('billing-ai-used')).toHaveText('0 of 3,000 used');
	await expect(panel.getByRole('meter', { name: 'AI-read invoices used this month' })).toBeVisible();
	await expect(panel.getByTestId('billing-ai-paused')).toHaveCount(0);
});

test('Free pauses at the allowance and offers no cap', async ({ page }) => {
	await page.route(isSubscription, (route) =>
		route.fulfill({
			json: paidSubscription(freeUsage(), {
				code: 'free',
				name: 'Free',
				monthly_price: '0.00',
				currency: 'USD',
				entitlements: {},
				trial_days: 0
			})
		})
	);
	await page.goto('/billing');
	const panel = page.getByTestId('billing-ai-usage');
	await expect(panel.getByTestId('billing-ai-used')).toHaveText('0 of 100 used');
	await expect(panel.getByRole('meter', { name: 'AI-read invoices used this month' })).toBeVisible();
	await expect(panel.getByText(/pauses AI reading at the allowance/)).toBeVisible();
	// Free bills no overage, so there is no cap to set.
	await expect(panel.getByTestId('billing-ai-cap-input')).toHaveCount(0);
	await expect(panel.getByTestId('billing-ai-paused')).toHaveCount(0);
});

test('a paid tier at its cap shows the pause, the overage and the cap', async ({ page }) => {
	await page.route(isSubscription, (route) =>
		route.fulfill({ json: paidSubscription(paidUsage()) })
	);
	await page.goto('/billing');

	const panel = page.getByTestId('billing-ai-usage');
	await expect(panel.getByTestId('billing-ai-used')).toHaveText('530 of 500 used');
	await expect(panel.getByTestId('billing-ai-paused')).toContainText(
		"This month's spending cap is reached"
	);
	await expect(panel.getByText('each AI-read invoice is billed at $0.10')).toBeVisible();
	await expect(panel.getByText('25 invoices over the allowance')).toBeVisible();
	await expect(panel.getByTestId('billing-ai-cap-input')).toHaveValue('2.50');
});

test('saving the cap sends the exact decimal string and redraws from the answer', async ({
	page
}) => {
	await page.route(isSubscription, (route) =>
		route.fulfill({ json: paidSubscription(paidUsage()) })
	);
	let sent: unknown = undefined;
	await page.route(isSpendCap, async (route) => {
		sent = route.request().postDataJSON();
		const answer: BillingSpendCapResponse = {
			monthly_spend_cap: '5.10',
			ai_usage: paidUsage({
				spend_cap: '5.10',
				overage_units: 30,
				overage_amount: '3.00',
				paused: false,
				pause_reason: null
			})
		};
		await route.fulfill({ json: answer });
	});
	await page.goto('/billing');

	const panel = page.getByTestId('billing-ai-usage');
	await panel.getByTestId('billing-ai-cap-input').fill('5.1');
	await panel.getByTestId('billing-ai-cap-save').click();

	await expect(panel.getByText('Spending cap saved.')).toBeVisible();
	expect(sent).toEqual({ monthly_spend_cap: '5.1' });
	await expect(panel.getByTestId('billing-ai-paused')).toHaveCount(0);
	await expect(panel.getByTestId('billing-ai-cap-input')).toHaveValue('5.10');
});

test('an invalid cap is refused before any request', async ({ page }) => {
	await page.route(isSubscription, (route) =>
		route.fulfill({ json: paidSubscription(paidUsage()) })
	);
	let requests = 0;
	await page.route(isSpendCap, (route) => {
		requests += 1;
		return route.abort();
	});
	await page.goto('/billing');

	const panel = page.getByTestId('billing-ai-usage');
	await panel.getByTestId('billing-ai-cap-input').fill('1.005');
	await panel.getByTestId('billing-ai-cap-save').click();
	await expect(panel.getByRole('alert')).toContainText('at most two decimal places');
	expect(requests).toBe(0);
});
