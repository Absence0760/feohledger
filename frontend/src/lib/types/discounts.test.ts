import { describe, it, expect } from 'vitest';
import type { DiscountOptimization, DiscountRoi } from './discounts';
import { isTierStarted, normalizeTierDays, normalizeTierPercent } from './discounts';
import { en } from '#lib/i18n/locales/en.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';

/**
 * Guard on the optimizer's two-list contract.
 *
 * `POST /api/discounts/optimize` returns `recommendations` (ranked by APR) AND
 * `unrankable` — offers with no net due date, whose ROI horizon is unknown.
 * The server used to substitute the discount deadline for the missing due
 * date, which reported a fabricated `0.00 %` APR beside a positive net
 * benefit; it now withholds the horizon-relative fields instead.
 *
 * Rendering the second list is the whole point of the frontend half: an offer
 * the page does not render is ABSENT, and a real capturable saving that
 * disappears from the UI is a worse failure than the wrong number it replaced.
 */
describe('the unrankable copy', () => {
	it('exists in the catalogue', () => {
		for (const key of [
			'discounts.opt.unrankableHeading',
			'discounts.opt.unrankableNote',
			'discounts.opt.aprUnknown',
			'discounts.opt.notRanked'
		] as const) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('counts the offers it is describing', () => {
		// The note carries `{n}` so it can say how many offers are outside the
		// ranking — "some offers" would leave the reader unable to tell whether
		// the section is an edge case or most of their savings.
		expect(en['discounts.opt.unrankableNote']).toContain('{n');
		expect(interpolate(en['discounts.opt.unrankableNote'], { n: 1 }, 'en')).toContain('1 offer ');
		expect(interpolate(en['discounts.opt.unrankableNote'], { n: 4 }, 'en')).toContain('4 offers ');
	});

	it('never states a rate for an unknown horizon', () => {
		// The failure mode this replaced: a 0 % APR presented as measured. The
		// strings must say "unknown", not a number.
		expect(en['discounts.opt.aprUnknown']).not.toMatch(/\d/);
		expect(en['discounts.opt.unrankableHeading']).not.toMatch(/\d/);
	});
});

describe('DiscountRoi', () => {
	it('reads an unrankable ROI as unknown, not as zero', () => {
		// The shape the server sends for a horizonless offer: the saving is real
		// (a percentage of the base amount — horizon-free), everything the
		// horizon feeds is null, and `worthwhile` is null meaning CANNOT RANK,
		// which is a different answer from `false` (ranked, and it loses).
		const roi: DiscountRoi = {
			base_amount: '10000.00',
			discount_percent: 2,
			days_accelerated: null,
			savings: '200.00',
			annualized_return_pct: null,
			cost_of_capital_pct: 8,
			opportunity_cost: null,
			net_benefit: null,
			worthwhile: null,
			horizon_known: false
		};
		expect(roi.annualized_return_pct).toBeNull();
		expect(roi.savings).toBe('200.00');
		// The page tints the row on `worthwhile === true`, never on truthiness:
		// `null` must not read as a decided negative verdict either.
		expect(roi.worthwhile === true).toBe(false);
		expect(roi.worthwhile === false).toBe(false);
	});

	it('treats a payload with no unrankable array as having none', () => {
		// The field is optional so a response predating it still parses; the
		// page reads `?? []` rather than throwing on `undefined.length`.
		const older: DiscountOptimization = {
			cash_budget: null,
			currency: 'USD',
			cost_of_capital_pct: 8,
			total_savings_available: '0.00',
			total_savings_selected: '0.00',
			total_outlay_selected: '0.00',
			unconvertible_count: 0,
			recommendations: []
		};
		expect(older.unrankable ?? []).toEqual([]);
	});
});

/**
 * Type-level half, in the shape `moneyTypeAudit.test.ts` uses: TypeScript
 * fails an UNUSED `@ts-expect-error`, so re-widening these fields back to
 * non-nullable turns `pnpm check` red as well as this file. Never resolve one
 * with an `as` cast — that puts the fabricated 0.00 % straight back.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars
function unknownHorizonMustNotCompile(roi: DiscountRoi) {
	// @ts-expect-error a withheld APR is null — render "unknown", never 0.0.
	const apr = roi.annualized_return_pct.toFixed(1);
	// @ts-expect-error an unknown horizon has no day count to do maths on.
	const weeks = roi.days_accelerated / 7;
	return { apr, weeks };
}

/**
 * The bulk-negotiation tier validators
 * (`POST /api/discounts/bulk-negotiate`, proposed from the `/discounts` page).
 *
 * They exist so a doomed tier is refused with a sentence rather than a raw
 * pydantic 422, and — more importantly — so the percent reaches the wire as the
 * exact decimal TEXT it was typed as. A tier percent is applied to a base that
 * spans a vendor's whole open balance, so a rounding introduced on the way out
 * is a rounding on real money (root `CLAUDE.md` § Project invariants).
 */
describe('normalizeTierDays', () => {
	it('accepts the server range 0…365', () => {
		expect(normalizeTierDays('0')).toBe(0);
		expect(normalizeTierDays('10')).toBe(10);
		expect(normalizeTierDays('365')).toBe(365);
	});

	it('refuses out-of-range, non-integer and non-numeric input', () => {
		// 366 is outside `le=365`; 1000 also fails the digit bound.
		expect(normalizeTierDays('366')).toBeNull();
		expect(normalizeTierDays('1000')).toBeNull();
		expect(normalizeTierDays('7.5')).toBeNull();
		expect(normalizeTierDays('-1')).toBeNull();
		expect(normalizeTierDays('ten')).toBeNull();
	});

	it('treats blank / absent as not-a-value', () => {
		expect(normalizeTierDays('')).toBeNull();
		expect(normalizeTierDays('   ')).toBeNull();
		expect(normalizeTierDays(null)).toBeNull();
		expect(normalizeTierDays(undefined)).toBeNull();
	});
});

describe('normalizeTierPercent', () => {
	it('returns the typed text UNCHANGED, never a re-formatted number', () => {
		// The point of the whole helper: `2.50` must not come back as `2.5`, and
		// a long fraction must not be rounded on its way to the server.
		expect(normalizeTierPercent('2.50')).toBe('2.50');
		expect(normalizeTierPercent('3')).toBe('3');
		expect(normalizeTierPercent('0.005')).toBe('0.005');
		expect(normalizeTierPercent('1.234567890123')).toBe('1.234567890123');
	});

	it('trims surrounding whitespace only', () => {
		expect(normalizeTierPercent('  2.00  ')).toBe('2.00');
	});

	it('enforces the server bounds gt=0, lt=100 at both edges', () => {
		expect(normalizeTierPercent('0')).toBeNull();
		expect(normalizeTierPercent('0.00')).toBeNull();
		expect(normalizeTierPercent('100')).toBeNull();
		expect(normalizeTierPercent('100.00')).toBeNull();
		expect(normalizeTierPercent('150')).toBeNull();
		// Just inside each edge still passes.
		expect(normalizeTierPercent('0.01')).toBe('0.01');
		expect(normalizeTierPercent('99.99')).toBe('99.99');
	});

	it('refuses anything that is not a bare decimal', () => {
		expect(normalizeTierPercent('2%')).toBeNull();
		expect(normalizeTierPercent('-2')).toBeNull();
		expect(normalizeTierPercent('2e1')).toBeNull();
		expect(normalizeTierPercent('1,5')).toBeNull();
		expect(normalizeTierPercent('')).toBeNull();
		expect(normalizeTierPercent(null)).toBeNull();
	});
});

describe('isTierStarted', () => {
	it('reads a wholly blank row as an unused slot, not an error', () => {
		expect(isTierStarted({ days: '', percent: '' })).toBe(false);
		expect(isTierStarted({ days: '  ', percent: '  ' })).toBe(false);
	});

	it('reads either field carrying text as the user’s intent', () => {
		// A started-but-unreadable row must be refused, never silently dropped:
		// sending fewer tiers than were typed proposes a different offer from
		// the one on screen.
		expect(isTierStarted({ days: '10', percent: '' })).toBe(true);
		expect(isTierStarted({ days: '', percent: '2' })).toBe(true);
	});
});

/**
 * The bulk-negotiation copy, guarded the same way the unrankable copy above is.
 */
describe('the vendor-negotiation copy', () => {
	it('exists in the catalogue', () => {
		for (const key of [
			'discounts.bulk.open',
			'discounts.bulk.title',
			'discounts.bulk.baseLabel',
			'discounts.bulk.baseExplain',
			'discounts.bulk.confirm',
			'discounts.bulk.noValidUntil'
		] as const) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('names the vendor in the armed confirm', () => {
		// The second click commits an offer covering that vendor's ENTIRE open
		// balance, so the button has to say whose.
		expect(en['discounts.bulk.confirm']).toContain('{vendor}');
		expect(interpolate(en['discounts.bulk.confirm'], { vendor: 'Globex' }, 'en')).toContain(
			'Globex'
		);
	});

	it('never states a base amount before the server computes one', () => {
		// The intro describes the RULE; it must not carry a figure of its own.
		expect(en['discounts.bulk.intro']).not.toMatch(/\d/);
	});
});
