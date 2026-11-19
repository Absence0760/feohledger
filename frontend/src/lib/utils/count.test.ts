import { afterEach, describe, expect, it } from 'vitest';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import { formatCount } from './count';

afterEach(() => {
	setActiveFormatLocale(undefined);
});

// French groups with a narrow no-break space (U+202F); normalise for reading.
const spaces = (s: string) => s.replace(/[  ]/g, ' ');

describe('formatCount', () => {
	it('groups digits in the active in-app locale', () => {
		setActiveFormatLocale('en');
		expect(formatCount(12345)).toBe('12,345');
		setActiveFormatLocale('de');
		expect(formatCount(12345)).toBe('12.345');
		setActiveFormatLocale('fr');
		expect(spaces(formatCount(12345))).toBe('12 345');
	});

	it('follows a locale switch on the next call', () => {
		setActiveFormatLocale('en');
		expect(formatCount(1000)).toBe('1,000');
		setActiveFormatLocale('de');
		expect(formatCount(1000)).toBe('1.000');
	});

	it('takes an explicit locale over the active one', () => {
		setActiveFormatLocale('en');
		expect(formatCount(1000, { locale: 'de' })).toBe('1.000');
	});

	it('renders small counts and zero unchanged', () => {
		setActiveFormatLocale('de');
		expect(formatCount(0)).toBe('0');
		expect(formatCount(7)).toBe('7');
		expect(formatCount(-3)).toBe('-3');
	});

	it('reads a numeric string, as billing usage counters arrive', () => {
		setActiveFormatLocale('en');
		expect(formatCount('2500')).toBe('2,500');
		expect(formatCount(' 42 ')).toBe('42');
	});

	it('shows no fraction: a count is whole', () => {
		setActiveFormatLocale('en');
		expect(formatCount(1234.4)).toBe('1,234');
	});

	it('renders the fallback, never NaN or a fabricated 0, for unreadable input', () => {
		for (const bad of [null, undefined, '', '  ', 'n/a', '1e3', NaN, Infinity]) {
			expect(formatCount(bad)).toBe('—');
		}
		expect(formatCount(null, { fallback: '0' })).toBe('0');
	});

	it('survives a malformed locale tag', () => {
		expect(formatCount(5, { locale: 'not a locale!!' })).toBe('5');
	});
});
