import { afterEach, describe, expect, it } from 'vitest';
import {
	COMMON_CURRENCIES,
	currencyOptions,
	formatMoney,
	isNegativeAmount,
	isPositiveAmount,
	parseMoneyForLayout,
	scaleMoney,
	sumMoney
} from './money';
import { getActiveFormatLocale, setActiveFormatLocale } from '$lib/i18n/formatLocale';

afterEach(() => {
	// Reset the shared holder so locale state never leaks across tests.
	setActiveFormatLocale(undefined);
});

// sumMoney sums exact Decimal-string money amounts without going through
// JS floats. The bug it fixes: `sum + Number(amount)` reduces coerce each
// string to a binary float before adding, which can drift off the exact
// cent value for the same reason `0.1 + 0.2 !== 0.3` in JS — even though
// every individual amount is an exact Decimal.

describe('sumMoney', () => {
	it('sums plain decimal strings without float drift', () => {
		// The canonical repro: Number('0.1') + Number('0.2') === 0.30000000000000004
		expect(sumMoney(['0.1', '0.2'])).toBe(0.3);
	});

	it('sums a longer run of two-decimal amounts exactly', () => {
		// A naive float reduce accumulates error across many additions —
		// this must land on the exact cent total, not something like
		// 60.099999999999994.
		expect(sumMoney(['10.10', '20.20', '15.35', '14.45'])).toBe(60.1);
	});

	it('treats null/undefined/empty entries as zero', () => {
		expect(sumMoney(['10.00', null, undefined, '', '5.00'])).toBe(15);
	});

	it('handles negative amounts (e.g. a discount/credit line)', () => {
		expect(sumMoney(['100.00', '-25.50'])).toBe(74.5);
	});

	it('accepts numbers as well as strings', () => {
		expect(sumMoney([10, 20.5, '5.25'])).toBe(35.75);
	});

	it('returns 0 for an empty input', () => {
		expect(sumMoney([])).toBe(0);
	});

	it('returns 0 when every entry is null/undefined/empty', () => {
		expect(sumMoney([null, undefined, ''])).toBe(0);
	});

	it('skips non-numeric garbage rather than throwing on a display path', () => {
		expect(sumMoney(['10.00', 'not-a-number', '5.00'])).toBe(15);
	});

	it('handles amounts of differing decimal scale (e.g. a 0-decimal currency mixed with 2-decimal)', () => {
		expect(sumMoney(['100', '0.5'])).toBe(100.5);
	});

	it('accepts any iterable, not just arrays', () => {
		function* gen() {
			yield '1.00';
			yield '2.00';
		}
		expect(sumMoney(gen())).toBe(3);
	});
});

// `isPositiveAmount` is a *predicate* over the string-Decimals the API sends
// — it decides whether a figure is worth rendering at all (the 1099 report's
// card-excluded total / per-vendor card leg). It must never be mistaken for
// money arithmetic, so the cases below pin the boundaries: zero is not
// positive (there is nothing to show), and a missing/garbage figure is not
// positive either rather than throwing or rendering NaN.
describe('isPositiveAmount', () => {
	it('accepts a positive string-Decimal from the API', () => {
		expect(isPositiveAmount('1500.00')).toBe(true);
		expect(isPositiveAmount('0.01')).toBe(true);
	});

	it('accepts a positive number', () => {
		expect(isPositiveAmount(1500)).toBe(true);
	});

	it('rejects an exact zero in either shape', () => {
		// The 1099 card-excluded figure is "0.00" for the overwhelming majority
		// of vendors — showing an "excludes $0.00" qualifier there would be noise.
		expect(isPositiveAmount('0.00')).toBe(false);
		expect(isPositiveAmount('0')).toBe(false);
		expect(isPositiveAmount(0)).toBe(false);
	});

	it('rejects a negative amount', () => {
		expect(isPositiveAmount('-10.00')).toBe(false);
		expect(isPositiveAmount(-10)).toBe(false);
	});

	it('rejects absent / unparseable figures instead of throwing', () => {
		expect(isPositiveAmount(null)).toBe(false);
		expect(isPositiveAmount(undefined)).toBe(false);
		expect(isPositiveAmount('')).toBe(false);
		expect(isPositiveAmount('not-a-number')).toBe(false);
		expect(isPositiveAmount(Number.NaN)).toBe(false);
		expect(isPositiveAmount(Number.POSITIVE_INFINITY)).toBe(false);
	});
});

// `parseMoneyForLayout` is the ONE sanctioned money-string → number
// conversion: chart geometry and ordering, never a rendered figure. It exists
// so the CFO surfaces can keep their bar charts while every money field on
// the wire becomes an exact decimal string.

describe('parseMoneyForLayout', () => {
	it('parses an exact decimal string', () => {
		expect(parseMoneyForLayout('1234.50')).toBe(1234.5);
	});

	it('passes a number through (endpoints not yet migrated off float)', () => {
		expect(parseMoneyForLayout(1234.5)).toBe(1234.5);
	});

	it('parses a negative amount', () => {
		expect(parseMoneyForLayout('-42.00')).toBe(-42);
	});

	it('returns 0 for absent / empty input so a chart width is never NaN', () => {
		expect(parseMoneyForLayout(null)).toBe(0);
		expect(parseMoneyForLayout(undefined)).toBe(0);
		expect(parseMoneyForLayout('')).toBe(0);
	});

	it('returns 0 for unparseable input rather than throwing on a render path', () => {
		expect(parseMoneyForLayout('not-a-number')).toBe(0);
		expect(parseMoneyForLayout(Number.NaN)).toBe(0);
		expect(parseMoneyForLayout(Number.POSITIVE_INFINITY)).toBe(0);
	});

	it('keeps a Math.max scale usable across a mixed-scale series', () => {
		// The exact shape the CFO forecast chart uses: derive the scale from
		// the series, then each bar's width as a share of it.
		const series = ['0', '1500.00', '250.5'];
		const max = Math.max(1, ...series.map(parseMoneyForLayout));
		expect(max).toBe(1500);
		expect((parseMoneyForLayout(series[2]) / max) * 100).toBeCloseTo(16.7, 1);
	});
});

// `isNegativeAmount` mirrors `isPositiveAmount` — a predicate that decides
// whether to tint a figure as a loss, never what the figure reads as.

describe('isNegativeAmount', () => {
	it('accepts a negative string-Decimal or number', () => {
		expect(isNegativeAmount('-0.01')).toBe(true);
		expect(isNegativeAmount(-1500)).toBe(true);
	});

	it('rejects zero in either shape', () => {
		expect(isNegativeAmount('0.00')).toBe(false);
		expect(isNegativeAmount(0)).toBe(false);
		// -0 is zero, not a loss.
		expect(isNegativeAmount('-0.00')).toBe(false);
	});

	it('rejects a positive amount', () => {
		expect(isNegativeAmount('10.00')).toBe(false);
		expect(isNegativeAmount(10)).toBe(false);
	});

	it('rejects absent / unparseable figures instead of throwing', () => {
		expect(isNegativeAmount(null)).toBe(false);
		expect(isNegativeAmount(undefined)).toBe(false);
		expect(isNegativeAmount('')).toBe(false);
		expect(isNegativeAmount('not-a-number')).toBe(false);
		expect(isNegativeAmount(Number.NaN)).toBe(false);
		expect(isNegativeAmount(Number.NEGATIVE_INFINITY)).toBe(false);
	});
});

// `formatMoney` resolves its locale from `i18n/formatLocale.ts` on EVERY call
// (never captured at import), which is what makes a locale switch observable
// to a formatter call site at all. These lock the value half of that contract
// — the re-render half needs a browser, see `i18n/formatLocale.test.ts`.

/**
 * ICU separates an amount from a trailing currency symbol with a no-break
 * space (U+00A0 in de-DE, U+202F in fr) — invisible characters an editor will
 * silently normalise in a source literal — hence the escapes rather than the
 * characters themselves. Fold them to a plain space so the expectations below
 * stay readable and stable.
 */
const norm = (s: string) => s.replace(/[\u00a0\u202f]/g, ' ');

describe('formatMoney locale resolution', () => {
	it('defers to the runtime/browser locale until one is selected', () => {
		expect(getActiveFormatLocale()).toBeUndefined();
		// Whatever the runtime default is, the currency code still drives the
		// symbol + minor units — this must not throw or lose the amount.
		expect(formatMoney('1234.50', { currency: 'USD' })).toContain('1');
	});

	it('follows the active in-app locale', () => {
		setActiveFormatLocale('en-US');
		expect(norm(formatMoney('1234.50', { currency: 'USD' }))).toBe('$1,234.50');
		setActiveFormatLocale('de-DE');
		// Same amount, same ISO code — the locale moves the grouping/decimal
		// separators and the symbol placement.
		expect(norm(formatMoney('1234.50', { currency: 'USD' }))).toBe('1.234,50 $');
	});

	it('re-reads the locale on every call, so a switch needs no remount', () => {
		// The bug this guards: a module-level capture of the locale (or a
		// memoized Intl.NumberFormat keyed on nothing) would freeze the first
		// locale a page ever rendered with.
		setActiveFormatLocale('en-US');
		const first = formatMoney('1234.50', { currency: 'EUR' });
		setActiveFormatLocale('de-DE');
		const second = formatMoney('1234.50', { currency: 'EUR' });
		expect(norm(first)).toBe('€1,234.50');
		expect(norm(second)).toBe('1.234,50 €');
	});

	it('lets an explicit locale option win over the active locale', () => {
		setActiveFormatLocale('de-DE');
		expect(norm(formatMoney('1234.50', { currency: 'USD', locale: 'en-US' }))).toBe('$1,234.50');
	});

	it('resets to the runtime default when the locale is cleared', () => {
		setActiveFormatLocale('de-DE');
		expect(norm(formatMoney('1234.50', { currency: 'USD' }))).toBe('1.234,50 $');
		setActiveFormatLocale('');
		expect(getActiveFormatLocale()).toBeUndefined();
	});

	it('still localizes a figure it cannot label', () => {
		setActiveFormatLocale('de-DE');
		// Wrong length, and right length but not alphabetic: neither is a code
		// anyone established. The bare figure must still follow the ACTIVE
		// locale's separators, not drop back to the browser's.
		expect(norm(formatMoney('1234.50', { currency: 'NOPE' }))).toBe('1.234,50');
		expect(norm(formatMoney('1234.50', { currency: 'ZZ9' }))).toBe('1.234,50');
	});
});

// A code the payload did not name is not a licence to substitute one
// (`docs/decisions.md` §79/§82, §160, §196). `formatMoney` used to return
// `$1,234.50` for every row below — so a payment whose invoice carried no
// currency read as dollars on the web while mobile rendered it bare, and a
// by-entity row with no configured currency borrowed the org's.
describe('formatMoney with no provable currency', () => {
	it('renders the figure bare — no symbol, and not the org default either', () => {
		setActiveFormatLocale('en-US');
		for (const unproven of [undefined, null, '', '   ', 'US', 'USDD', 'u$d', 'ZZ9']) {
			expect(norm(formatMoney('1234.50', { currency: unproven }))).toBe('1,234.50');
		}
		expect(norm(formatMoney('1234.50'))).toBe('1,234.50');
	});

	it('still labels every code it was given, whatever its case', () => {
		setActiveFormatLocale('en-US');
		expect(norm(formatMoney('1234.50', { currency: 'EUR' }))).toBe('€1,234.50');
		expect(norm(formatMoney('1234.50', { currency: ' gbp ' }))).toBe('£1,234.50');
		// An unfamiliar but well-formed code is still a code: Intl prints it.
		expect(norm(formatMoney('1234.50', { currency: 'XYZ' }))).toBe('XYZ 1,234.50');
	});

	it('keeps the placeholder for a missing FIGURE, which is a different gap', () => {
		expect(formatMoney(null, { currency: null })).toBe('—');
		expect(formatMoney('', { currency: null }, 'n/a')).toBe('n/a');
		// A genuine zero is a figure.
		expect(norm(formatMoney('0', { currency: null, locale: 'en-US' }))).toBe('0.00');
	});

	it('honours whole and accounting without a symbol', () => {
		setActiveFormatLocale('en-US');
		expect(norm(formatMoney('1234.50', { currency: null, whole: true }))).toBe('1,235');
		expect(norm(formatMoney('-1234.50', { currency: null, accounting: true }))).toBe(
			'(1,234.50)'
		);
		expect(norm(formatMoney('-1234.50', { currency: null }))).toBe('-1,234.50');
		expect(norm(formatMoney('1234.50', { currency: null, accounting: true }))).toBe('1,234.50');
	});
});

describe('currencyOptions', () => {
	it('puts the preferred currency first without duplicating it', () => {
		const opts = currencyOptions('EUR');
		expect(opts[0]).toBe('EUR');
		expect(opts.filter((c) => c === 'EUR')).toHaveLength(1);
		expect(opts).toHaveLength(COMMON_CURRENCIES.length);
	});

	it('includes a preferred currency the shortlist does not carry', () => {
		// The whole point: a shortlist that cannot express the currency the org
		// reports in is a dead end, which is the failure the hardcoded "USD" on
		// the credit-memo create had.
		const opts = currencyOptions('KES');
		expect(opts[0]).toBe('KES');
		expect(opts).toHaveLength(COMMON_CURRENCIES.length + 1);
	});

	it('normalises case and whitespace', () => {
		expect(currencyOptions('  eur ')[0]).toBe('EUR');
	});

	it('ignores an absent or malformed preference rather than adding a blank', () => {
		// A store that has not loaded yet must not put an empty option at the top.
		for (const bad of [undefined, null, '', '   ', 'US', 'USDD']) {
			expect(currencyOptions(bad)).toEqual([...COMMON_CURRENCIES]);
		}
	});

	it('is order-stable, so a picker does not reshuffle between renders', () => {
		expect(currencyOptions('GBP')).toEqual(currencyOptions('GBP'));
	});
});

describe('scaleMoney', () => {
	it('multiplies a money string by a quantity exactly', () => {
		// 0.1 * 3 is 0.30000000000000004 in float.
		expect(scaleMoney('0.10', 3)).toBe('0.30');
		expect(scaleMoney('19.99', 3)).toBe('59.97');
		expect(scaleMoney('1234.56', '2.5')).toBe('3086.40');
	});

	it('folds a divisor into the same rounding step for percentages', () => {
		// A 2.5% early-pay discount on 1,000.00.
		expect(scaleMoney('1000.00', '2.5', { divideBy: 100 })).toBe('25.00');
		expect(scaleMoney('333.33', '15', { divideBy: 100 })).toBe('50.00');
	});

	it('rounds half up on the magnitude, like the backend', () => {
		expect(scaleMoney('0.005', 1)).toBe('0.01');
		expect(scaleMoney('0.015', 1)).toBe('0.02');
		expect(scaleMoney('-0.005', 1)).toBe('-0.01');
	});

	it('rounds once, not twice', () => {
		// Two sequential 2dp roundings of 1.004 * 1.004 would give 1.00 via
		// 1.00 * 1.00; the exact product is 1.008016 -> 1.01.
		expect(scaleMoney('1.004', '1.004')).toBe('1.01');
	});

	it('honours an explicit scale', () => {
		expect(scaleMoney('1.00', '1', { scale: 0 })).toBe('1');
		expect(scaleMoney('0.123456', '1', { scale: 4 })).toBe('0.1235');
	});

	it('never renders a negative zero', () => {
		expect(scaleMoney('-0.001', 1)).toBe('0.00');
	});

	it('refuses rather than repairs unreadable input', () => {
		// The defect class this exists to prevent: a preview that silently
		// invents a figure the user then trusts.
		expect(scaleMoney('1234.56abc', 2)).toBeNull();
		expect(scaleMoney('', 2)).toBeNull();
		expect(scaleMoney(null, 2)).toBeNull();
		expect(scaleMoney('10.00', undefined)).toBeNull();
		expect(scaleMoney('10.00', 'two')).toBeNull();
		expect(scaleMoney('10.00', 2, { divideBy: 0 })).toBeNull();
		expect(scaleMoney('10.00', 2, { scale: -1 })).toBeNull();
	});

	it('accepts a number amount without going through float arithmetic', () => {
		expect(scaleMoney(19.99, 3)).toBe('59.97');
	});
});
