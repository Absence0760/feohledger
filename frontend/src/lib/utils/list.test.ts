import { afterEach, describe, expect, it } from 'vitest';
import { formatList, __resetListFormatterCache } from './list';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';

afterEach(() => {
	// Reset the shared holder so locale state never leaks across tests, and drop
	// the memo so a later test can't be served a formatter built for another
	// locale. Mirrors `money.test.ts`.
	setActiveFormatLocale(undefined);
	__resetListFormatterCache();
});

describe('formatList — the point of the helper', () => {
	// This is the regression guard the followup entry exists for. A literal
	// `.join(', ')` produces byte-identical output in every locale; the whole
	// reason for the helper is that Japanese must NOT look like English here.
	it('separates Japanese with the ideographic comma, not ", "', () => {
		setActiveFormatLocale('ja');
		const ja = formatList(['A', 'B', 'C']);

		setActiveFormatLocale('en');
		__resetListFormatterCache();
		const en = formatList(['A', 'B', 'C']);

		expect(ja).not.toBe(en);
		expect(ja).toBe('A、B、C');
		expect(ja).not.toContain(', ');
		expect(en).toBe('A, B, C');
	});

	it('leaves the English rendering exactly as the literal it replaces', () => {
		// Every migrated call site previously emitted `parts.join(', ')`. English
		// output must be unchanged, or the migration is a copy change wearing an
		// i18n fix's clothes — and the e2e text assertions would move with it.
		setActiveFormatLocale('en');
		expect(formatList(['Vendor', 'Invoice number', 'Amount'])).toBe(
			['Vendor', 'Invoice number', 'Amount'].join(', ')
		);
	});

	it('uses each locale`s own final-pair rule', () => {
		const expected: Record<string, string> = {
			en: 'A, B, C',
			de: 'A, B und C',
			fr: 'A, B, C',
			es: 'A, B y C',
			'pt-BR': 'A, B, C',
			ja: 'A、B、C'
		};
		for (const loc of SUPPORTED_LOCALES) {
			setActiveFormatLocale(loc);
			__resetListFormatterCache();
			expect(formatList(['A', 'B', 'C']), `${loc} list punctuation`).toBe(expected[loc]);
		}
	});
});

describe('formatList — styles', () => {
	it('renders an explicit conjunction and disjunction in English', () => {
		setActiveFormatLocale('en');
		expect(formatList(['A', 'B', 'C'], 'and')).toBe('A, B, and C');
		__resetListFormatterCache();
		expect(formatList(['A', 'B', 'C'], 'or')).toBe('A, B, or C');
	});

	it('keeps the styles distinct in Japanese too', () => {
		setActiveFormatLocale('ja');
		expect(formatList(['A', 'B'], 'plain')).toBe('A、B');
		__resetListFormatterCache();
		expect(formatList(['A', 'B'], 'or')).toContain('または');
	});

	it('memoizes per (locale, style) rather than per locale', () => {
		// A single-slot cache keyed on locale alone would serve the `plain`
		// formatter to the next `or` call on the same locale.
		setActiveFormatLocale('en');
		expect(formatList(['A', 'B'], 'plain')).toBe('A, B');
		expect(formatList(['A', 'B'], 'or')).toBe('A or B');
		expect(formatList(['A', 'B'], 'plain')).toBe('A, B');
	});
});

describe('formatList — degenerate input', () => {
	it('matches [].join for empty and nullish lists', () => {
		setActiveFormatLocale('ja');
		expect(formatList([])).toBe('');
		expect(formatList(null)).toBe('');
		expect(formatList(undefined)).toBe('');
	});

	it('returns a single entry unchanged, with no separator', () => {
		setActiveFormatLocale('ja');
		expect(formatList(['Only'])).toBe('Only');
	});

	it('coerces numbers rather than throwing', () => {
		// `Intl.ListFormat#format` throws a TypeError on a non-string element.
		setActiveFormatLocale('en');
		expect(formatList([1, 2, 3])).toBe('1, 2, 3');
	});
});

describe('formatList — environment degradation', () => {
	it('falls back to the literal separator when Intl.ListFormat is absent', () => {
		// Reached through an index signature rather than the `Intl` namespace
		// type: `Intl.ListFormat` is declared read-only, and the point of the
		// test is to simulate an engine (an older embedded WebView) that never
		// declared it at all.
		const intlSlot = Intl as unknown as Record<string, unknown>;
		const original = intlSlot.ListFormat;
		try {
			delete intlSlot.ListFormat;
			__resetListFormatterCache();
			setActiveFormatLocale('ja');
			// Degrades to yesterday's behaviour rather than throwing: an
			// English-punctuated list still renders, where a `TypeError` would take
			// the whole surrounding render down.
			expect(formatList(['A', 'B', 'C'])).toBe('A, B, C');
		} finally {
			intlSlot.ListFormat = original;
			__resetListFormatterCache();
		}
	});

	it('falls back when the active locale tag is malformed', () => {
		// The holder carries whatever `navigator.language` reports; a garbage tag
		// makes the constructor throw RangeError.
		setActiveFormatLocale('not a locale');
		expect(formatList(['A', 'B'])).toBe('A, B');
	});
});
