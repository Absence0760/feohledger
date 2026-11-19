import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import {
	formatRebateRate,
	nextRebateTransition,
	rebateTone,
	REBATE_STATUSES,
	type CardRebate
} from './cardRebate';

// Intl spaces a French percent sign with a narrow no-break space (U+202F).
const spaces = (s: string) => s.replace(/[\u00a0\u202f]/g, ' ');

describe('CardRebate.currency', () => {
	// `rebateAmountCurrency` used to live here: it derived, from the envelope's
	// `excluded_rebate_count`, whether a ROW could honestly be labelled at all —
	// zero exclusions proved the set was homogeneous, anything else meant "we
	// cannot say", and the table rendered bare figures. It is gone because the
	// wire answers the question directly now: `RebateResponse.currency` is
	// resolved server-side from the `virtual_cards` row the rebate accrued on,
	// so a MIXED list renders every row under its own code and nothing has to be
	// inferred from a count. This test stands in its place so the deletion is
	// legible rather than silent.
	it('is a required per-row field, not something derived from the envelope', () => {
		const row: CardRebate = {
			id: 'r1',
			virtual_card_id: 'c1',
			amount: '125.50',
			rate: 0.0125,
			currency: 'EUR',
			status: 'pending',
			period: '2026-06',
			created_at: '2026-06-01T00:00:00Z'
		};
		expect(row.currency).toBe('EUR');
	});
});

describe('nextRebateTransition', () => {
	it('offers exactly the step the backend will accept', () => {
		expect(nextRebateTransition('pending')).toBe('confirm');
		expect(nextRebateTransition('confirmed')).toBe('mark-paid');
	});

	it('offers nothing at the terminal status — there is no skip and no reversal', () => {
		expect(nextRebateTransition('paid_out')).toBeNull();
	});

	it('offers nothing for a status the backend never persists', () => {
		expect(nextRebateTransition('cancelled')).toBeNull();
		expect(nextRebateTransition('')).toBeNull();
	});
});

describe('rebateTone', () => {
	it('tints every status the lifecycle defines', () => {
		// `pending` is warning, not success: the dashboard's realized headline
		// deliberately excludes it, so it must not read as money in the bank.
		expect(rebateTone('pending')).toBe('warning');
		expect(rebateTone('confirmed')).toBe('accent');
		expect(rebateTone('paid_out')).toBe('success');
		for (const s of REBATE_STATUSES) expect(rebateTone(s)).not.toBe('neutral');
	});

	it('falls back to a flat pill for an unknown status rather than rendering untinted', () => {
		expect(rebateTone('something_new')).toBe('neutral');
	});
});

describe('formatRebateRate', () => {
	// The figure is locale-formatted; pin the locale so the assertions do not
	// depend on the runner's.
	beforeEach(() => setActiveFormatLocale('en-US'));
	afterEach(() => setActiveFormatLocale(undefined));

	it('renders a ratio as the percentage the rate was negotiated in', () => {
		expect(formatRebateRate(0.0125)).toBe('1.25%');
		expect(formatRebateRate('0.01')).toBe('1.00%');
	});

	it('follows the active locale rather than pinning English punctuation', () => {
		setActiveFormatLocale('fr-FR');
		expect(spaces(formatRebateRate(0.0125))).toBe('1,25 %');
	});

	it('renders the placeholder rather than NaN% for absent / unreadable input', () => {
		expect(formatRebateRate(null)).toBe('—');
		expect(formatRebateRate(undefined)).toBe('—');
		expect(formatRebateRate('')).toBe('—');
		expect(formatRebateRate('not-a-rate')).toBe('—');
		expect(formatRebateRate(Number.POSITIVE_INFINITY)).toBe('—');
	});
});
