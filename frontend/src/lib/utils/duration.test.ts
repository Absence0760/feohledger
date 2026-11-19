import { afterEach, describe, expect, it } from 'vitest';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import { formatDays, formatHours } from './duration';

afterEach(() => {
	setActiveFormatLocale(undefined);
});

// Intl may separate the unit with a (narrow) no-break space; normalise for readability.
const spaces = (s: string) => s.replace(/[  ]/g, ' ');

describe('formatDays', () => {
	it('renders a compact day figure in the active locale', () => {
		setActiveFormatLocale('en-US');
		expect(formatDays(12.5)).toBe('12.5d');
		setActiveFormatLocale('de-DE');
		expect(spaces(formatDays(12.5))).toBe('12,5 T');
		setActiveFormatLocale('fr-FR');
		expect(spaces(formatDays(12.5))).toBe('12,5j');
	});

	it('keeps a fixed decimal when asked, as the CFO KPIs do', () => {
		setActiveFormatLocale('en-US');
		expect(formatDays(12, { minimumFractionDigits: 1, maximumFractionDigits: 1 })).toBe('12.0d');
		expect(formatDays(12.46, { minimumFractionDigits: 1, maximumFractionDigits: 1 })).toBe('12.5d');
	});

	it('spells the unit with its plural for an accessible name', () => {
		setActiveFormatLocale('en-US');
		expect(formatDays(1, { unitDisplay: 'long' })).toBe('1 day');
		expect(formatDays(12.5, { unitDisplay: 'long' })).toBe('12.5 days');
		setActiveFormatLocale('de-DE');
		expect(spaces(formatDays(1, { unitDisplay: 'long' }))).toBe('1 Tag');
		expect(spaces(formatDays(3, { unitDisplay: 'long' }))).toBe('3 Tage');
	});

	it('renders the fallback for null or a non-finite value, never NaN', () => {
		expect(formatDays(null)).toBe('—');
		expect(formatDays(undefined)).toBe('—');
		expect(formatDays(NaN)).toBe('—');
		expect(formatDays(Infinity, { fallback: 'n/a' })).toBe('n/a');
	});
});

describe('formatHours', () => {
	it('renders hours with up to the requested decimals', () => {
		setActiveFormatLocale('en-US');
		expect(formatHours(1.5, { unitDisplay: 'long', maximumFractionDigits: 2 })).toBe('1.5 hours');
		expect(formatHours(1, { unitDisplay: 'long', maximumFractionDigits: 2 })).toBe('1 hour');
		expect(formatHours(1 / 3, { unitDisplay: 'long', maximumFractionDigits: 2 })).toBe(
			'0.33 hours'
		);
		setActiveFormatLocale('de-DE');
		expect(spaces(formatHours(1.5, { unitDisplay: 'long' }))).toBe('1,5 Stunden');
	});
});
