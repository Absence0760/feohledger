import { describe, expect, it } from 'vitest';
import {
	currencyCodeProblem,
	currencySuggestions,
	isoCurrencyCodes,
	normalizeCurrencyCode,
	seededCurrency,
	upperAsciiLetters
} from './currencyCode.ts';
import { COMMON_CURRENCIES, DEFAULT_CURRENCY } from './money.ts';

describe('normalizeCurrencyCode', () => {
	it('accepts any three-letter code, not only the common shortlist', () => {
		for (const code of ['PLN', 'CZK', 'HUF', 'CNY', 'HKD', 'AED']) {
			expect(COMMON_CURRENCIES as readonly string[]).not.toContain(code);
			expect(normalizeCurrencyCode(code)).toBe(code);
		}
	});

	it('uppercases and trims', () => {
		expect(normalizeCurrencyCode(' pln ')).toBe('PLN');
	});

	it('refuses blank and malformed codes', () => {
		for (const bad of ['', '   ', 'EU', 'EURO', 'U$D', '12A', null, undefined]) {
			expect(normalizeCurrencyCode(bad)).toBeNull();
		}
	});

	it('refuses letters that are not ASCII, checked before uppercasing (decisions §381)', () => {
		// "uß".toUpperCase() is "USS" and "ﬀa" is "FFA": an upper-then-check
		// rule would have accepted both.
		for (const bad of ['ÄÖÜ', 'ДОЛ', 'ＵＳＤ', 'uß', 'ﬀa']) {
			expect(normalizeCurrencyCode(bad)).toBeNull();
		}
	});
});

describe('upperAsciiLetters', () => {
	it('uppercases ASCII letters only, so a typed "ß" never becomes "SS"', () => {
		expect(upperAsciiLetters('pln')).toBe('PLN');
		expect(upperAsciiLetters('uß')).toBe('Uß');
		expect(upperAsciiLetters('äöü')).toBe('äöü');
		expect(upperAsciiLetters('u$d')).toBe('U$D');
	});
});

describe('currencyCodeProblem', () => {
	it('names blank as required and a bad shape as invalid', () => {
		expect(currencyCodeProblem('')).toBe('required');
		expect(currencyCodeProblem('  ')).toBe('required');
		expect(currencyCodeProblem('EU')).toBe('invalid');
		expect(currencyCodeProblem('EURO')).toBe('invalid');
		expect(currencyCodeProblem('pln')).toBeNull();
		expect(currencyCodeProblem('ÄÖÜ')).toBe('invalid');
	});
});

describe('currencySuggestions', () => {
	const all = ['AED', 'CZK', 'EUR', 'HKD', 'PLN', 'USD', 'bad', 'XXXX'];

	it('leads with the preferred code, then the shortlist, then the rest alphabetically', () => {
		const list = currencySuggestions('PLN', all);
		expect(list[0]).toBe('PLN');
		expect(list.slice(1, 1 + COMMON_CURRENCIES.length)).toEqual([...COMMON_CURRENCIES]);
		expect(list.slice(1 + COMMON_CURRENCIES.length)).toEqual(['AED', 'CZK', 'HKD']);
	});

	it('has no duplicates and drops malformed entries', () => {
		const list = currencySuggestions('USD', all);
		expect(new Set(list).size).toBe(list.length);
		expect(list).not.toContain('bad');
		expect(list).not.toContain('XXXX');
	});

	it('still offers the shortlist when the runtime lists nothing', () => {
		expect(currencySuggestions(null, [])).toEqual([...COMMON_CURRENCIES]);
	});
});

describe('isoCurrencyCodes', () => {
	it('lists codes beyond the shortlist where Intl can (Node has supportedValuesOf)', () => {
		const codes = isoCurrencyCodes();
		expect(codes).toContain('PLN');
		expect(codes.every((c) => /^[A-Z]{3}$/.test(c))).toBe(true);
	});
});

describe('seededCurrency', () => {
	it('starts from the platform default while the org currency is unresolved', () => {
		expect(seededCurrency('', null, false)).toBe(DEFAULT_CURRENCY);
	});

	it('follows the org currency once it resolves, after a default seed', () => {
		expect(seededCurrency(DEFAULT_CURRENCY, 'GBP', false)).toBe('GBP');
	});

	it('normalizes the org currency it adopts', () => {
		expect(seededCurrency('', 'pln', false)).toBe('PLN');
	});

	it('keeps what the user typed, even when the org currency lands later', () => {
		expect(seededCurrency('HKD', 'GBP', true)).toBe('HKD');
		expect(seededCurrency('', 'GBP', true)).toBe('');
	});

	it('ignores a malformed org currency rather than adopting it', () => {
		expect(seededCurrency('', 'EURO', false)).toBe(DEFAULT_CURRENCY);
		expect(seededCurrency('CHF', '', false)).toBe('CHF');
	});
});
