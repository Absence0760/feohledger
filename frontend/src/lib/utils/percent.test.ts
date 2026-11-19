import { afterEach, describe, expect, it } from 'vitest';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import { formatPercent, pointsToRatioString } from './percent';

afterEach(() => {
	setActiveFormatLocale(undefined);
});

// Intl separates a French percent sign with a narrow no-break space (U+202F)
// and German with a no-break space (U+00A0); normalise both for readability.
const spaces = (s: string) => s.replace(/[  ]/g, ' ');

describe('pointsToRatioString', () => {
	it('moves the decimal point two places left as text', () => {
		expect(pointsToRatioString('12.00')).toBe('0.1200');
		expect(pointsToRatioString('2')).toBe('0.02');
		expect(pointsToRatioString('100')).toBe('1.00');
		expect(pointsToRatioString('1234.5')).toBe('12.345');
		expect(pointsToRatioString('-5')).toBe('-0.05');
		expect(pointsToRatioString('.5')).toBe('0.005');
		expect(pointsToRatioString(12.5)).toBe('0.125');
		expect(pointsToRatioString(0)).toBe('0.00');
	});

	it('refuses what is not a finite decimal', () => {
		for (const bad of [null, undefined, '', '   ', 'n/a', '1e3', '1.2.3', '.', NaN, Infinity]) {
			expect(pointsToRatioString(bad)).toBeNull();
		}
	});
});

describe('formatPercent', () => {
	it('reads its input as 0–100 points, not a ratio', () => {
		setActiveFormatLocale('en-US');
		expect(formatPercent(12.5)).toBe('12.5%');
		expect(formatPercent(2)).toBe('2%');
		expect(formatPercent('100')).toBe('100%');
	});

	it('follows the active locale — French spaces the sign and uses a comma', () => {
		setActiveFormatLocale('fr-FR');
		expect(spaces(formatPercent(2))).toBe('2 %');
		expect(spaces(formatPercent('12.5'))).toBe('12,5 %');
		setActiveFormatLocale('de-DE');
		expect(spaces(formatPercent(12.5))).toBe('12,5 %');
	});

	it('rounds a string decimal exactly — no float drift', () => {
		setActiveFormatLocale('en-US');
		// 1.005 / 100 as a float is 0.010049999…, which would round to 1.00%.
		expect(formatPercent('1.005', { minimumFractionDigits: 2, maximumFractionDigits: 2 })).toBe(
			'1.01%'
		);
		expect(formatPercent('0.07', { maximumFractionDigits: 2 })).toBe('0.07%');
	});

	it('honours fraction digits, defaulting to at most one', () => {
		setActiveFormatLocale('en-US');
		expect(formatPercent(12.345)).toBe('12.3%');
		expect(formatPercent('12.00', { minimumFractionDigits: 2 })).toBe('12.00%');
		expect(formatPercent(3, { minimumFractionDigits: 1 })).toBe('3.0%');
	});

	it('renders the fallback, never NaN% or a fabricated 0%', () => {
		expect(formatPercent(null)).toBe('—');
		expect(formatPercent(undefined, { fallback: 'n/a' })).toBe('n/a');
		expect(formatPercent('n/a')).toBe('—');
		expect(formatPercent(NaN)).toBe('—');
	});
});
