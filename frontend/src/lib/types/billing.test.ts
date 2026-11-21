import { describe, expect, it } from 'vitest';

import {
	aiUsagePercent,
	aiUsageTone,
	cardFormMode,
	parseSpendCapInput,
	planChangeCardAction,
	rebateMeterGroups,
	type BillingAiUsage,
	type BillingUsage
} from './billing';

function usage(extra: Record<string, string> = {}): BillingUsage {
	return { extractions: '3', extractions_platform: '2', ...extra };
}

describe('rebateMeterGroups', () => {
	it('reads one group per currency meter', () => {
		expect(
			rebateMeterGroups(
				usage({ 'card_rebate_total.USD': '15.00', 'card_rebate_total.EUR': '7.00' })
			)
		).toEqual([
			{ currency: 'EUR', total: '7.00' },
			{ currency: 'USD', total: '15.00' }
		]);
	});

	it('is empty when the org accrued no rebates', () => {
		// The backend emits no rebate key at all rather than a zero in an
		// unstated currency, so the page renders nothing instead of a `$0.00`
		// whose currency it invented.
		expect(rebateMeterGroups(usage())).toEqual([]);
		expect(rebateMeterGroups(null)).toEqual([]);
		expect(rebateMeterGroups(undefined)).toEqual([]);
	});

	it('never returns a bare cross-currency total', () => {
		// `card_rebate_total` with no currency suffix is the pre-fix shape: a
		// figure in no currency. It must not be rendered under a guessed code.
		expect(rebateMeterGroups(usage({ card_rebate_total: '22.00' }))).toEqual([]);
	});

	it('ignores a malformed currency suffix rather than fabricating a code', () => {
		expect(
			rebateMeterGroups(
				usage({
					'card_rebate_total.': '1.00',
					'card_rebate_total.US': '2.00',
					'card_rebate_total.DOLLARS': '3.00',
					'card_rebate_total.gbp': '4.00'
				})
			)
		).toEqual([{ currency: 'GBP', total: '4.00' }]);
	});

	it('leaves other meters alone', () => {
		const groups = rebateMeterGroups(
			usage({ extractions_lambda: '9', 'card_rebate_total.USD': '1.00' })
		);
		expect(groups).toEqual([{ currency: 'USD', total: '1.00' }]);
	});

	it('keeps the amount an exact string', () => {
		// Money never round-trips through a float on the way to the screen.
		const [g] = rebateMeterGroups(usage({ 'card_rebate_total.JPY': '12345678901234.56' }));
		expect(g.total).toBe('12345678901234.56');
	});
});

function ai(extra: Partial<BillingAiUsage> = {}): BillingAiUsage {
	return {
		period: '2026-10',
		used: 0,
		included: 100,
		overage_unit_price: null,
		currency: 'USD',
		overage_units: 0,
		overage_amount: '0.00',
		projected_overage_amount: '0.00',
		spend_cap: null,
		paused: false,
		pause_reason: null,
		...extra
	};
}

describe('aiUsagePercent / aiUsageTone', () => {
	it('reads how full the allowance is', () => {
		expect(aiUsagePercent(ai({ used: 40 }))).toBe(40);
		expect(aiUsageTone(ai({ used: 79 }))).toBe('ok');
		expect(aiUsageTone(ai({ used: 80 }))).toBe('warning');
		expect(aiUsageTone(ai({ used: 100 }))).toBe('danger');
	});

	it('clamps past the allowance (paid overage) at 100%', () => {
		expect(aiUsagePercent(ai({ used: 650, included: 500 }))).toBe(100);
	});

	it('is null on an unmetered plan, and red whenever reading is paused', () => {
		expect(aiUsagePercent(ai({ included: null, used: 9 }))).toBeNull();
		expect(aiUsageTone(ai({ used: 10, paused: true, pause_reason: 'spend_cap_reached' }))).toBe(
			'danger'
		);
		expect(aiUsagePercent(null)).toBeNull();
	});

	it('treats a zero allowance as full once anything is read', () => {
		expect(aiUsagePercent(ai({ included: 0, used: 0 }))).toBe(0);
		expect(aiUsagePercent(ai({ included: 0, used: 1 }))).toBe(100);
	});
});

describe('parseSpendCapInput', () => {
	it('keeps the typed decimal string exactly (no float round trip)', () => {
		expect(parseSpendCapInput(' 25.5 ')).toEqual({ ok: true, value: '25.5' });
		expect(parseSpendCapInput('0')).toEqual({ ok: true, value: '0' });
		expect(parseSpendCapInput('1000000.00')).toEqual({ ok: true, value: '1000000.00' });
	});

	it('reads blank as "no cap"', () => {
		expect(parseSpendCapInput('   ')).toEqual({ ok: true, value: null });
	});

	it('refuses what the backend refuses', () => {
		for (const bad of ['-1', '1.005', '1000000.01', 'abc', '1,000', '1e3', '.5']) {
			expect(parseSpendCapInput(bad), bad).toEqual({ ok: false });
		}
	});
});

describe('cardFormMode', () => {
	const stripe = {
		provider: 'stripe_billing',
		configured: true,
		client_secret: 'seti_x_secret_y',
		setup_intent_id: 'seti_x'
	};
	const mock = {
		provider: 'mock',
		configured: true,
		client_secret: 'mock_seti_cus_1_secret',
		setup_intent_id: 'mock_seti_cus_1'
	};

	it('mounts Stripe Elements only for stripe_billing with a publishable key', () => {
		expect(cardFormMode(stripe, 'pk_test_abc')).toBe('stripe');
	});

	it('names the operator gap when the backend runs Stripe but the build has no key', () => {
		// Stripe.js must never load without a key; this is a config gap, not
		// the "billing not configured" state the org can do nothing about.
		expect(cardFormMode(stripe, '')).toBe('stripe_key_missing');
		expect(cardFormMode(stripe, '   ')).toBe('stripe_key_missing');
		expect(cardFormMode(stripe, undefined)).toBe('stripe_key_missing');
	});

	it('uses the local test path for the mock provider, key or no key', () => {
		expect(cardFormMode(mock, '')).toBe('mock');
		expect(cardFormMode(mock, 'pk_test_abc')).toBe('mock');
	});

	it('is not_configured when no SetupIntent was started', () => {
		const off = { provider: 'stripe_billing', configured: false, client_secret: null, setup_intent_id: null };
		expect(cardFormMode(off, 'pk_test_abc')).toBe('not_configured');
		expect(cardFormMode({ ...mock, configured: false }, '')).toBe('not_configured');
		expect(cardFormMode({ ...mock, setup_intent_id: null }, '')).toBe('not_configured');
	});

	it('is not_configured for Stripe without a client secret, or an adapter with no card form', () => {
		expect(cardFormMode({ ...stripe, client_secret: null }, 'pk_test_abc')).toBe('not_configured');
		expect(cardFormMode({ ...stripe, provider: 'paddle' }, 'pk_test_abc')).toBe('not_configured');
	});
});

describe('planChangeCardAction', () => {
	it('offers to add a card when the org has none', () => {
		expect(planChangeCardAction('payment_method_required')).toBe('add_card');
	});

	it('offers to update the card when the upgrade charge was declined', () => {
		expect(planChangeCardAction('billing_payment_declined')).toBe('update_card');
	});

	it('offers nothing for any other refusal', () => {
		expect(planChangeCardAction('billing_not_configured')).toBeNull();
		expect(planChangeCardAction('plan_change_in_progress')).toBeNull();
		expect(planChangeCardAction('plan_feature_required')).toBeNull();
		expect(planChangeCardAction(null)).toBeNull();
		expect(planChangeCardAction(undefined)).toBeNull();
	});
});
