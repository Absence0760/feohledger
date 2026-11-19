import { describe, expect, it } from 'vitest';
import { loadAllMessages } from '../i18n/allMessages';
import { SUPPORTED_LOCALES } from '../i18n/locale';

/**
 * A pre-formatted count never reaches an ICU plural (or select) variable.
 *
 * `interpolate.ts::resolvePlural` picks the branch from `Number(raw)`. A count
 * already run through `formatCount` is a display string: "1,234" (en),
 * "1.234" (de), "1 234" (fr). `Number("1,234")` is NaN, so the plural falls to
 * `other` whatever the count, and `Number("1.234")` is 1.234, which picks a
 * branch for a number nobody has. The block formats `#` through `formatCount`
 * itself, so the fix is always to pass the raw number to a plural variable.
 * A `select` keyed on a formatted value is wrong for the same reason.
 *
 * This finds every `m('key', { x: …formatCount(…)… })` call under `src/` and
 * fails if any shipped locale's message for that key uses `{x, plural` or
 * `{x, select`. Each locale is checked, not only English: a translator may
 * pluralise a sentence the English catalogue states flat.
 */
const RAW = import.meta.glob('/src/**/*.{svelte,ts}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

interface FormattedParam {
	file: string;
	key: string;
	param: string;
}

/** Strip comments so a doc sentence quoting a call is not a hit. */
function code(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
}

/** Index of the bracket closing the one at `open`, skipping string literals. */
function closing(src: string, open: number): number {
	let depth = 0;
	for (let i = open; i < src.length; i++) {
		const c = src[i];
		if (c === "'" || c === '"' || c === '`') {
			const end = src.indexOf(c, i + 1);
			if (end < 0) return -1;
			i = end;
			continue;
		}
		if (c === '{' || c === '(' || c === '[') depth++;
		else if (c === '}' || c === ')' || c === ']') {
			depth--;
			if (depth === 0) return i;
		}
	}
	return -1;
}

/** Split an object literal's body on its top-level commas. */
function properties(body: string): string[] {
	const out: string[] = [];
	let depth = 0;
	let start = 0;
	for (let i = 0; i < body.length; i++) {
		const c = body[i];
		if (c === "'" || c === '"' || c === '`') {
			const end = body.indexOf(c, i + 1);
			if (end < 0) break;
			i = end;
		} else if (c === '{' || c === '(' || c === '[') depth++;
		else if (c === '}' || c === ')' || c === ']') depth--;
		else if (c === ',' && depth === 0) {
			out.push(body.slice(start, i));
			start = i + 1;
		}
	}
	out.push(body.slice(start));
	return out;
}

const CALL = /\bm\(\s*(['"])([\w.-]+)\1\s*,\s*\{/g;

/** Every literal-keyed `m()` param whose value runs through `formatCount`. */
function formattedCountParams(file: string, source: string): FormattedParam[] {
	const src = code(source);
	const found: FormattedParam[] = [];
	for (const match of src.matchAll(CALL)) {
		const open = (match.index ?? 0) + match[0].length - 1;
		const close = closing(src, open);
		if (close < 0) continue;
		for (const prop of properties(src.slice(open + 1, close))) {
			const p = /^\s*(\w+)\s*:([\s\S]*)$/.exec(prop);
			if (p && /\bformatCount\(/.test(p[2])) found.push({ file, key: match[2], param: p[1] });
		}
	}
	return found;
}

/** Whether `template` uses `param` as an ICU plural or select variable. */
function usesAsIcuSelector(template: string, param: string): boolean {
	return new RegExp(`\\{\\s*${param}\\s*,\\s*(?:plural|select)\\s*,`).test(template);
}

function offences(
	sites: FormattedParam[],
	catalogues: Record<string, Record<string, string>>
): string[] {
	const found: string[] = [];
	for (const { file, key, param } of sites) {
		for (const [loc, dict] of Object.entries(catalogues)) {
			const template = dict[key];
			if (typeof template === 'string' && usesAsIcuSelector(template, param)) {
				found.push(`${file}: m('${key}') passes formatCount to {${param}, …} in ${loc}`);
			}
		}
	}
	return found;
}

describe('the scanner catches what it is for', () => {
	it('finds a formatted count across lines, in a ternary and beside raw params', () => {
		const source = `
			const a = m('x.one', { n: formatCount(rows.length) });
			const b = m("x.two", {
				shown: formatCount(items.length),
				total: big ? formatCount(total) : '—',
				raw: total,
				nested: { inner: formatCount(1) }
			});
			// m('x.comment', { n: formatCount(1) })
			const c = m('x.raw', { n: rows.length });
		`;
		expect(formattedCountParams('f.svelte', source)).toEqual([
			{ file: 'f.svelte', key: 'x.one', param: 'n' },
			{ file: 'f.svelte', key: 'x.two', param: 'shown' },
			{ file: 'f.svelte', key: 'x.two', param: 'total' }
		]);
	});

	it('flags a plural or select on a formatted param, in any locale, and nothing else', () => {
		const sites = formattedCountParams('f.svelte', "m('k', { n: formatCount(c), t: formatCount(d) })");
		const catalogues = {
			en: { k: '{n} of {t} rows' },
			de: { k: '{n, plural, one {# Zeile} other {# Zeilen}} von {t}' },
			fr: { k: '{ t , select, other {x}} {n}' }
		};
		expect(offences(sites, catalogues)).toEqual([
			"f.svelte: m('k') passes formatCount to {n, …} in de",
			"f.svelte: m('k') passes formatCount to {t, …} in fr"
		]);
	});

	it('does not confuse a variable that merely shares a prefix', () => {
		expect(usesAsIcuSelector('{count, plural, other {#}}', 'c')).toBe(false);
		expect(usesAsIcuSelector('{count, plural, other {#}}', 'count')).toBe(true);
	});
});

describe('a pre-formatted count never selects an ICU plural', () => {
	const sites = Object.entries(RAW)
		.filter(([path]) => !/\.test\.ts$/.test(path))
		.flatMap(([path, source]) => formattedCountParams(path, source));

	it('the scan finds formatCount params at all (guards a vacuous suite)', () => {
		expect(sites.length).toBeGreaterThan(0);
	});

	it('no locale pluralises or selects on a formatted count', async () => {
		const catalogues: Record<string, Record<string, string>> = {};
		for (const loc of SUPPORTED_LOCALES) {
			catalogues[loc] = (await loadAllMessages(loc)) as Record<string, string>;
		}
		expect(offences(sites, catalogues)).toEqual([]);
	});
});
