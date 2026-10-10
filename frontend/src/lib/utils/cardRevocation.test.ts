import { describe, expect, it } from 'vitest';
import { cardRevocationWarnings } from './cardRevocation';
import type { CardRevocationItem, VendorCardRevocation } from '#lib/types/vendor.ts';

const t = (key: string, params?: Record<string, string | number>) => `${key}:${params?.n}`;

const item = (id: string, outcome: string, payment_id: string | null = null): CardRevocationItem => ({
	card_id: id,
	last_four: '4242',
	outcome,
	payment_id
});

const rev = (over: Partial<VendorCardRevocation> = {}): VendorCardRevocation => ({
	vendor_id: 'v1',
	cancelled: 0,
	not_closed: [],
	requires_payment_void: [],
	...over
});

describe('cardRevocationWarnings', () => {
	it('says nothing when every card closed, or nothing was revoked', () => {
		expect(cardRevocationWarnings([rev({ cancelled: 3 })], t)).toEqual([]);
		expect(cardRevocationWarnings([null, undefined], t)).toEqual([]);
		expect(cardRevocationWarnings(undefined, t)).toEqual([]);
	});

	it('warns about a card the provider did not confirm closed', () => {
		const r = rev({ not_closed: [item('c1', 'card_cancel_error:TimeoutError')] });
		expect(cardRevocationWarnings([r], t)).toEqual(['vendors.cards.notClosed:1']);
	});

	it('warns separately about a card that only a payment void can close', () => {
		const r = rev({ requires_payment_void: [item('c2', 'payment_live', 'p1')] });
		expect(cardRevocationWarnings([r], t)).toEqual(['vendors.cards.requiresVoid:1']);
	});

	it('sums across every vendor a bulk call touched', () => {
		const a = rev({ vendor_id: 'a', not_closed: [item('c1', 'card_cancel_rejected')] });
		const b = rev({
			vendor_id: 'b',
			not_closed: [item('c2', 'cards_not_configured')],
			requires_payment_void: [item('c3', 'payment_live', 'p1')]
		});
		expect(cardRevocationWarnings([a, b], t)).toEqual([
			'vendors.cards.notClosed:2',
			'vendors.cards.requiresVoid:1'
		]);
	});
});
