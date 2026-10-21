import { describe, expect, test } from 'vitest';
import { EN_ALL, loadAllMessages } from './allMessages';
import { SUPPORTED_LOCALES } from './locale';
import { interpolate } from './interpolate';

// `messages_parity.test.ts` guards that every locale carries every KEY and the
// same `{placeholder}` set. It cannot see inside a `{n, plural, …}` block — the
// placeholder regex deliberately skips them — so a malformed ICU block (an
// unbalanced brace, a missing `other` branch, a mistyped `plural`) survives
// parity and renders the raw `{n, plural, one {…}` markup to the reader.
//
// This resolves every plural-bearing message in every shipped locale — the
// main catalogue and every lazy slice (catalogues.ts) — at the
// counts that select each CLDR category we use, and asserts nothing of the ICU
// syntax survives into the output.

const PLURAL_MARKER = ', plural,';
const COUNTS = [0, 1, 2, 5, 11];

/** Params covering every placeholder the message declares, plus the count. */
function paramsFor(template: string, n: number): Record<string, string | number> {
	const params: Record<string, string | number> = {};
	for (const token of template.match(/\{[a-zA-Z0-9_]+\}/g) ?? []) {
		params[token.slice(1, -1)] = 'X';
	}
	// Every plural block's control variable, whatever it is named.
	for (const m of template.matchAll(/\{(\w+),\s*plural,/g)) params[m[1]] = n;
	return params;
}

describe('ICU plural messages resolve in every locale', () => {
	const pluralKeys = Object.entries(EN_ALL as Record<string, string>)
		.filter(([, v]) => v.includes(PLURAL_MARKER))
		.map(([k]) => k);

	test('the English catalogue actually uses plurals (guards a vacuous suite)', () => {
		expect(pluralKeys.length).toBeGreaterThan(0);
	});

	for (const loc of SUPPORTED_LOCALES) {
		test(`${loc}: every plural block selects a branch`, async () => {
			const dict = (await loadAllMessages(loc)) as Record<string, string>;
			for (const key of pluralKeys) {
				const template = dict[key];
				for (const n of COUNTS) {
					const out = interpolate(template, paramsFor(template, n), loc);
					expect(out, `${loc}.${key} @ n=${n} left ICU markup unresolved`).not.toContain(
						PLURAL_MARKER
					);
					expect(out, `${loc}.${key} @ n=${n} left a stray brace`).not.toMatch(/[{}]/);
					expect(out.trim().length, `${loc}.${key} @ n=${n} rendered empty`).toBeGreaterThan(0);
				}
			}
		});
	}
});
