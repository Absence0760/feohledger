import { describe, expect, it } from 'vitest';

/**
 * Source-scan guard: a `<DataTable>` picks its own empty-row message.
 *
 * Sixty-odd tables used to hand-build `empty={loading ? m('common.loading') :
 * errored ? … : …}`, and the copies drifted: some dropped the errored branch,
 * some the loading one, several used a private "Loading cards…" key, and the
 * component's own default was the English literal 'No items.'. DataTable now
 * takes `loading` / `errored` / `filtered` (+ `emptyFiltered` / `emptyErrored`)
 * and chooses the message itself, announcing the loading row
 * (`frontend/docs/ui-patterns.md` § Data tables).
 *
 * So inside a `<DataTable …>` tag this refuses:
 * - an `empty` / `emptyFiltered` / `emptyErrored` value that is a string
 *   literal (untranslated copy), and
 * - one that is a ternary over a loading message (`m('common.loading')` or a
 *   private `….loading` key) or over `m('common.loadFailed')` — those states
 *   are the `loading` / `errored` props.
 *
 * `ALLOWED` is the shrinking list of files not converted yet. It may only
 * shrink: the guard fails when an allowlisted file is clean, so the entry is
 * removed in the same change that converts it.
 *
 * Reads the tree through Vite's `import.meta.glob`, as `pagedListFooter.test.ts`
 * does — the frontend deliberately carries no `@types/node`.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** Files still hand-building an empty message, pending conversion. */
const ALLOWED: ReadonlySet<string> = new Set<string>([]);

const EMPTY_ATTRS = new Set(['empty', 'emptyFiltered', 'emptyErrored']);

/** Each `<DataTable …>` opening tag, brace-aware so `{a > b}` does not end it. */
function dataTableTags(source: string): string[] {
	const tags: string[] = [];
	const re = /<DataTable\b/g;
	for (let match = re.exec(source); match; match = re.exec(source)) {
		let depth = 0;
		let quote: string | null = null;
		for (let i = match.index; i < source.length; i++) {
			const ch = source[i];
			if (quote) {
				if (ch === quote) quote = null;
			} else if (depth > 0 && (ch === "'" || ch === '"' || ch === '`')) {
				quote = ch;
			} else if (ch === '{') depth++;
			else if (ch === '}') depth--;
			else if (ch === '>' && depth === 0) {
				tags.push(source.slice(match.index, i + 1));
				break;
			}
		}
	}
	return tags;
}

/** `name=value` pairs of a tag, value being the raw `"…"` or `{…}` text. */
function attributes(tag: string): [string, string][] {
	const out: [string, string][] = [];
	const re = /\s([A-Za-z]+)=/g;
	for (let match = re.exec(tag); match; match = re.exec(tag)) {
		const start = match.index + match[0].length;
		let end = start;
		if (tag[start] === '"') end = tag.indexOf('"', start + 1) + 1;
		else if (tag[start] === '{') {
			let depth = 0;
			for (let i = start; i < tag.length; i++) {
				if (tag[i] === '{') depth++;
				else if (tag[i] === '}' && --depth === 0) {
					end = i + 1;
					break;
				}
			}
		}
		out.push([match[1], tag.slice(start, end)]);
		re.lastIndex = end;
	}
	return out;
}

const LITERAL = /^(?:"|\{\s*['"`])/;
const STATE_MESSAGE = /m\(\s*'(?:common\.loadFailed|[A-Za-z0-9.]*\.?loading)'/i;

/** Offending `attr=value` snippets in one file's source. */
function offences(source: string): string[] {
	const out: string[] = [];
	for (const tag of dataTableTags(source)) {
		for (const [name, value] of attributes(tag)) {
			if (!EMPTY_ATTRS.has(name)) continue;
			const literal = LITERAL.test(value);
			const stateTernary = value.includes('?') && STATE_MESSAGE.test(value);
			if (literal || stateTernary) out.push(`${name}=${value.replace(/\s+/g, ' ')}`);
		}
	}
	return out;
}

describe('DataTable empty messages', () => {
	it('reads the component tree', () => {
		expect(Object.keys(RAW).length).toBeGreaterThan(50);
		const tables = Object.values(RAW).flatMap(dataTableTags);
		expect(tables.length, 'the list pages render DataTable').toBeGreaterThan(50);
	});

	it('no table hand-builds its loading / errored message or uses a literal', () => {
		const found: string[] = [];
		for (const [path, source] of Object.entries(RAW)) {
			if (ALLOWED.has(path)) continue;
			for (const offence of offences(source)) found.push(`${path}: ${offence}`);
		}
		expect(
			found,
			'pass loading / errored / filtered to DataTable and give empty a message key'
		).toEqual([]);
	});

	it('the allowlist only names files that still offend', () => {
		const clean = [...ALLOWED].filter((path) => offences(RAW[path] ?? '').length === 0);
		expect(clean, 'converted (or moved): drop these from ALLOWED').toEqual([]);
	});

	it('DataTable defaults to a message key, not English', () => {
		const table = RAW['/src/lib/components/ui/DataTable.svelte'];
		expect(table, 'DataTable.svelte moved?').toBeTypeOf('string');
		expect(table).not.toMatch(/empty\s*=\s*['"]/);
		expect(table).toContain("m('common.empty')");
		expect(table).toContain('role="status"');
	});
});

describe('the DataTable empty scanner catches what it is for', () => {
	it('flags loading / errored ternaries and literals, not plain keys', () => {
		const source = [
			`<DataTable isEmpty={x} empty={loading ? m('common.loading') : m('a.empty')}>`,
			`<DataTable isEmpty={x}\n\tempty={busy\n\t\t? m('payments.cards.loading')\n\t\t: m('a.empty')}\n>`,
			`<DataTable isEmpty={x} empty={bad ? m('common.loadFailed') : m('a.empty')}>`,
			`<DataTable isEmpty={x} empty="No items.">`,
			`<DataTable isEmpty={x} emptyFiltered={'Nothing here'}>`,
			`<DataTable isEmpty={a > b} {loading} filtered={q !== ''} empty={m('a.empty')}>`,
			`<DataTable isEmpty={x} empty={term ? m('a.filtered') : m('a.empty')}>`
		].join('\n');
		const found = offences(source);
		expect(found).toHaveLength(5);
		expect(found[0]).toContain("m('common.loading')");
		expect(found[1]).toContain("m('payments.cards.loading')");
		expect(found[2]).toContain("m('common.loadFailed')");
		expect(found[3]).toBe('empty="No items."');
		expect(found[4]).toBe("emptyFiltered={'Nothing here'}");
	});

	it('does not end a tag at a > inside an expression', () => {
		const tags = dataTableTags(`<DataTable isEmpty={a > b} empty={m('a.empty')}>\n<tr>`);
		expect(tags).toEqual([`<DataTable isEmpty={a > b} empty={m('a.empty')}>`]);
	});
});
