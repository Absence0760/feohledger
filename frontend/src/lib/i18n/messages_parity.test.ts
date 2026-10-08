import { describe, test, expect } from 'vitest';
import { en } from './locales/en';
import { SUPPORTED_LOCALES } from './locale';
import { CATALOGUE_LOADERS, HELP_CATALOGUE_LOADERS } from './catalogues';
import { enHelp } from './locales/help/en';

// `satisfies Messages` already enforces key parity at compile time; this
// guards it at runtime too and — by iterating SUPPORTED_LOCALES through the
// typed loader registry rather than a hard-coded list — guarantees that
// *every* shipped locale is loadable, complete, non-empty, and preserves
// the English {placeholder} set. Adding a locale to SUPPORTED_LOCALES (with
// its CATALOGUE_LOADERS entry) automatically brings it under this test; a
// forgotten / empty / placeholder-drifted catalogue fails here.

const enRecord = en as Record<string, string>;
const enKeys = Object.keys(en).sort();

/** The `{placeholder}` tokens in a string (excludes `{n, plural, …}` blocks). */
function placeholders(s: string): string[] {
	return (s.match(/\{[a-zA-Z0-9_]+\}/g) ?? []).sort();
}

describe('message catalogue parity', () => {
	test('the English source dict is non-empty', () => {
		expect(enKeys.length).toBeGreaterThan(0);
	});

	test('every shipped locale has a loader registered', () => {
		// Guards against adding a locale to SUPPORTED_LOCALES without wiring its
		// CATALOGUE_LOADERS entry (a missing loader would otherwise only surface
		// as a runtime `undefined()` in the per-locale tests below).
		for (const loc of SUPPORTED_LOCALES) {
			expect(typeof CATALOGUE_LOADERS[loc], `${loc} has no loader`).toBe('function');
		}
		// The full starter set ships.
		expect([...SUPPORTED_LOCALES]).toEqual(['en', 'de', 'fr', 'es', 'pt-BR', 'ja']);
	});

	for (const loc of SUPPORTED_LOCALES) {
		test(`${loc}: loadable, complete, non-empty, placeholder-faithful`, async () => {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			expect(Object.keys(dict).sort(), `${loc} key set differs from en`).toEqual(enKeys);
			for (const key of enKeys) {
				expect(dict[key].trim().length, `${loc}.${key} is empty`).toBeGreaterThan(0);
				expect(placeholders(dict[key]), `${loc}.${key} placeholder mismatch`).toEqual(
					placeholders(enRecord[key]),
				);
			}
		});
	}
});

// The help-centre slice is a second catalogue per locale (decisions §261), held
// to the same rules — and to two of its own: no key may live in both slices
// (one would silently shadow the other in `m()`), and only `help.*` keys may
// live in the help slice, while the main catalogue keeps just the chrome ones.
const enHelpRecord = enHelp as Record<string, string>;
const enHelpKeys = Object.keys(enHelp).sort();

describe('help catalogue parity', () => {
	test('the two slices never share a key', () => {
		const shared = enHelpKeys.filter((k) => k in en);
		expect(shared).toEqual([]);
	});

	test('the help slice holds help.* keys, and main keeps only the chrome ones', () => {
		expect(enHelpKeys.length).toBeGreaterThan(0);
		expect(enHelpKeys.every((k) => k.startsWith('help.'))).toBe(true);
		const helpInMain = enKeys.filter((k) => k.startsWith('help.'));
		expect(helpInMain.every((k) => k === 'help.pageLink' || k.startsWith('help.tip.'))).toBe(true);
	});

	for (const loc of SUPPORTED_LOCALES) {
		test(`${loc}: help slice loadable, complete, non-empty, placeholder-faithful`, async () => {
			const dict = (await HELP_CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			expect(Object.keys(dict).sort(), `${loc} help key set differs from en`).toEqual(enHelpKeys);
			for (const key of enHelpKeys) {
				expect(dict[key].trim().length, `${loc}.${key} is empty`).toBeGreaterThan(0);
				expect(placeholders(dict[key]), `${loc}.${key} placeholder mismatch`).toEqual(
					placeholders(enHelpRecord[key]),
				);
			}
		});
	}
});

