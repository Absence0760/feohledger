import { describe, test, expect } from 'vitest';
import { en } from './locales/en';
import { SUPPORTED_LOCALES } from './locale';
import { CATALOGUE_LOADERS, SLICE_LOADERS, SLICE_NAMES } from './catalogues';
import { EN_SLICES } from './allMessages';

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

// Each lazy slice (catalogues.ts, decisions §261 + §300) is a catalogue per
// locale of its own, held to the same rules. Which keys a slice may hold, and
// that no key lives in two places, is `slices.test.ts`'s job; this file proves
// every locale ships every slice complete.
describe('catalogue slice parity', () => {
	test('every slice has a loader for every shipped locale', () => {
		expect(SLICE_NAMES.length).toBeGreaterThan(0);
		for (const s of SLICE_NAMES)
			for (const loc of SUPPORTED_LOCALES) expect(typeof SLICE_LOADERS[s][loc], `${s}/${loc}`).toBe('function');
	});

	test('the help slice holds help.* keys, and main keeps only the chrome ones', () => {
		expect(Object.keys(EN_SLICES.help).every((k) => k.startsWith('help.'))).toBe(true);
		const helpInMain = enKeys.filter((k) => k.startsWith('help.'));
		expect(helpInMain.every((k) => k === 'help.pageLink' || k.startsWith('help.tip.'))).toBe(true);
	});

	for (const slice of SLICE_NAMES) {
		const enSlice = EN_SLICES[slice] as Record<string, string>;
		const sliceKeys = Object.keys(enSlice).sort();
		for (const loc of SUPPORTED_LOCALES) {
			test(`${slice}/${loc}: loadable, complete, non-empty, placeholder-faithful`, async () => {
				const dict = (await SLICE_LOADERS[slice][loc]()) as Record<string, string>;
				expect(sliceKeys.length).toBeGreaterThan(0);
				expect(Object.keys(dict).sort(), `${loc} ${slice} key set differs from en`).toEqual(sliceKeys);
				for (const key of sliceKeys) {
					expect(dict[key].trim().length, `${loc}.${key} is empty`).toBeGreaterThan(0);
					expect(placeholders(dict[key]), `${loc}.${key} placeholder mismatch`).toEqual(
						placeholders(enSlice[key]),
					);
				}
			});
		}
	}
});
