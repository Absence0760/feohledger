import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: a control whose "on" / "current" state is drawn by
 * `class:active` also exposes that state to assistive tech (WCAG 4.1.2 Name,
 * Role, Value; 1.3.1 Info and Relationships).
 *
 * `class:active` is how this codebase paints "this one is selected" — the
 * highlighted sidebar link, the chosen horizon on `/cfo`, the pressed filter
 * chip. A sighted user reads it from the colour; a screen reader reads nothing
 * unless the element also carries the matching ARIA state:
 *
 *   - a navigation link to the page you are on → `aria-current="page"`
 *   - a toggle / segmented / filter button     → `aria-pressed`
 *   - a tab                                    → `role="tab"` + `aria-selected`
 *
 * The app shell's sidebar and the supplier-portal nav marked the current page
 * by colour alone, and `/cfo`'s granularity and horizon pickers did the same
 * for the chosen value, until 2026-10-05.
 *
 * {@link ALLOWLIST} names the matches whose state is already in their text.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const STATE_ATTRS = ['aria-current', 'aria-pressed', 'aria-selected', 'aria-checked'];

/**
 * `path:line-text` → why the element needs no ARIA state. Keyed on the trimmed
 * source line of the element's opening tag, not a line number, so an unrelated
 * edit cannot re-point an entry.
 */
const ALLOWLIST: Record<string, string> = {
	// The label itself flips between "Active" and "Inactive" — the state IS the
	// name. `aria-pressed` on top would announce it twice, contradictorily.
	'routes/workflows/[id]/+page.svelte:<button class="btn-toggle" class:active={workflow.is_active} onclick={toggleActive}>':
		'state is the label',
	// The sort state belongs to the COLUMN, and the enclosing `<th>` carries it
	// as `aria-sort` — which is what a screen reader announces for a header.
	'lib/components/ui/SortableHeader.svelte:<button type="button" class="sort-btn" class:active onclick={() => onsort(field)}>':
		'aria-sort on the enclosing <th>'
};

interface Element {
	type: string;
	name: string;
	start: number;
	attributes: { type: string; name: string }[];
}

function collectElements(node: unknown, out: Element[] = []): Element[] {
	if (node === null || typeof node !== 'object') return out;
	if (Array.isArray(node)) {
		for (const child of node) collectElements(child, out);
		return out;
	}
	const record = node as Record<string, unknown>;
	if (record.type === 'RegularElement') out.push(record as unknown as Element);
	for (const [key, value] of Object.entries(record)) {
		if (key === 'parent') continue;
		collectElements(value, out);
	}
	return out;
}

function silentToggles(path: string, source: string): string[] {
	const ast = parse(source, { modern: true });
	const lines = source.split('\n');
	return collectElements(ast.fragment)
		.filter((el) => el.name === 'a' || el.name === 'button')
		.filter((el) => el.attributes.some((a) => a.type === 'ClassDirective' && a.name === 'active'))
		.filter((el) => !el.attributes.some((a) => STATE_ATTRS.includes(a.name)))
		.map((el) => {
			const line = source.slice(0, el.start).split('\n').length;
			return { line, text: lines[line - 1].trim() };
		})
		.filter(({ text }) => !(`${path}:${text}` in ALLOWLIST))
		.map(({ line, text }) => `${path}:${line}  ${text}`);
}

describe('selected state is exposed, not only painted (WCAG 4.1.2)', () => {
	it('the scanner flags a colour-only toggle and accepts each ARIA state', () => {
		expect(silentToggles('t', `<button class:active={on}>A</button>`)).toHaveLength(1);
		expect(silentToggles('t', `<a href="/x" class:active={on}>A</a>`)).toHaveLength(1);
		expect(silentToggles('t', `<button class:active={on} aria-pressed={on}>A</button>`)).toEqual(
			[]
		);
		expect(
			silentToggles('t', `<a href="/x" class:active={on} aria-current={on ? 'page' : undefined}>A</a>`)
		).toEqual([]);
	});

	it('every allowlist entry still names a real element', () => {
		const lines = new Set<string>();
		for (const { path, source } of files) {
			for (const line of source.split('\n')) lines.add(`${path}:${line.trim()}`);
		}
		expect(Object.keys(ALLOWLIST).filter((k) => !lines.has(k))).toEqual([]);
	});

	it('no `class:active` link or button is silent about its state', () => {
		const offenders = files.flatMap(({ path, source }) => silentToggles(path, source));
		expect(
			offenders,
			'add aria-current="page" (nav link), aria-pressed (toggle) or role="tab" + aria-selected'
		).toEqual([]);
	});
});
