import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: the one `.grid-container` in the tree is a keyboard stop.
 *
 * `.grid-container` scrolls sideways once a table is wider than its card —
 * the WCAG 1.4.10 remedy for a table that cannot reflow. A region that scrolls
 * has to be reachable by keyboard too (2.1.1), and a read-only table gives the
 * keyboard no other way in, so `ui/DataTable.svelte` makes the container
 * itself a named, focusable region.
 *
 * `tests-e2e/a11y/reflow.spec.ts` checks the same thing with axe at 320px, but
 * axe only sees a region while it actually overflows, which depends on the
 * viewport and how wide the font renders — the legal pages' tables passed
 * every local run and failed in CI that way. This guard reads the markup, so
 * it does not depend on rendering at all.
 *
 * It also pins that no page hand-rolls `<div class="grid-container">` again:
 * a copy would carry the table styling (app.css keys it on the class) without
 * the attributes, and `frontend/docs/ui-patterns.md` § Data tables already
 * says to use the component instead.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const DATA_TABLE = 'lib/components/ui/DataTable.svelte';

interface Attribute {
	type: string;
	name: string;
	value: unknown;
}

interface Element {
	type: string;
	name: string;
	attributes: Attribute[];
}

/** Every element in a template fragment, whatever block it sits in. */
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

/** The literal text of a static attribute, or `null` when it is an expression. */
function staticValue(attr: Attribute): string | null {
	if (!Array.isArray(attr.value) || attr.value.length !== 1) return null;
	const only = attr.value[0] as Record<string, unknown>;
	return only.type === 'Text' ? String(only.data) : null;
}

function attr(element: Element, name: string): Attribute | undefined {
	return element.attributes.find((a) => a.type === 'Attribute' && a.name === name);
}

function gridContainers(source: string): Element[] {
	const ast = parse(source, { modern: true });
	return collectElements(ast.fragment).filter((el) => {
		const cls = attr(el, 'class');
		const value = cls ? staticValue(cls) : null;
		return value !== null && value.split(/\s+/).includes('grid-container');
	});
}

describe('DataTable scroll region (WCAG 2.1.1)', () => {
	it('scans a realistic number of files', () => {
		expect(files.length).toBeGreaterThan(50);
		expect(files.some((f) => f.path === DATA_TABLE)).toBe(true);
	});

	it('the container is a focusable, named region', () => {
		const source = files.find((f) => f.path === DATA_TABLE)!.source;
		const found = gridContainers(source);
		expect(found).toHaveLength(1);
		const [region] = found;

		const tabindex = attr(region, 'tabindex');
		expect(tabindex && staticValue(tabindex), 'tabindex="0" — a tab stop the arrows can pan').toBe(
			'0'
		);
		const role = attr(region, 'role');
		expect(role && staticValue(role), 'role="region" — what the stop is announced as').toBe(
			'region'
		);
		expect(attr(region, 'aria-label'), 'an accessible name for the region').toBeDefined();
	});

	it('every other scroll region made a tab stop is named too (4.1.2)', () => {
		// A page that wraps its own table in a scroller (`/cfo`'s two money
		// tables) needs the same three attributes the DataTable container
		// carries. With `tabindex="0"` alone the keyboard can reach it, and a
		// screen reader then announces an anonymous stop with no role and no
		// name — the CFO page did exactly that until both tables were named.
		//
		// The published legal pages are excluded, not exempt: their 19
		// `.table-scroll` wrappers share the defect and are tracked in
		// docs/followups.md, because naming each table is an edit to the legal
		// text (decisions §174) rather than to UI copy.
		const LEGAL = /^(routes\/legal\/|lib\/legal\/)/;
		const offenders: string[] = [];
		for (const f of files) {
			if (LEGAL.test(f.path)) continue;
			const ast = parse(f.source, { modern: true });
			for (const el of collectElements(ast.fragment)) {
				const tabindex = attr(el, 'tabindex');
				if (!tabindex || staticValue(tabindex) !== '0') continue;
				const role = attr(el, 'role');
				// An element given an interactive role (StepNode's role="button")
				// is a control, named by its content; this rule is about regions.
				if (role && staticValue(role) !== 'region') continue;
				const named = attr(el, 'aria-label') || attr(el, 'aria-labelledby');
				if (!role || !named) offenders.push(`${f.path} <${el.name}>`);
			}
		}
		expect(offenders, 'give the tab stop role="region" and an aria-label').toEqual([]);
	});

	it('no other file hand-rolls a .grid-container', () => {
		const offenders = files
			.filter((f) => f.path !== DATA_TABLE)
			.filter((f) => gridContainers(f.source).length > 0)
			.map((f) => f.path);
		expect(offenders, 'use <DataTable> rather than copying its container').toEqual([]);
	});
});
