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
		// The published legal pages are held to the same rule. Their 19
		// `.table-scroll` wrappers were excluded here until each was named by
		// the heading it sits under, and that exclusion is what let them ship
		// as anonymous stops in the first place.
		const offenders: string[] = [];
		const dangling: string[] = [];
		for (const f of files) {
			const ast = parse(f.source, { modern: true });
			const elements = collectElements(ast.fragment);
			const ids = new Set(
				elements
					.map((el) => attr(el, 'id'))
					.map((a) => (a ? staticValue(a) : null))
					.filter((v): v is string => v !== null)
			);
			for (const el of elements) {
				const tabindex = attr(el, 'tabindex');
				if (!tabindex || staticValue(tabindex) !== '0') continue;
				const role = attr(el, 'role');
				// An element given an interactive role (StepNode's role="button")
				// is a control, named by its content; this rule is about regions.
				if (role && staticValue(role) !== 'region') continue;
				const labelledby = attr(el, 'aria-labelledby');
				const named = attr(el, 'aria-label') || labelledby;
				if (!role || !named) offenders.push(`${f.path} <${el.name}>`);
				// `aria-labelledby` naming an id that is not there yields an
				// EMPTY name, which is the defect this rule exists to catch
				// wearing an attribute that hides it. A static reference must
				// resolve to an element in the same file.
				const refs = labelledby ? staticValue(labelledby) : null;
				for (const ref of refs?.split(/\s+/).filter(Boolean) ?? []) {
					if (!ids.has(ref)) dangling.push(`${f.path} <${el.name}> → #${ref}`);
				}
			}
		}
		expect(dangling, 'aria-labelledby must point at an id in the same file').toEqual([]);
		expect(
			offenders,
			'give the tab stop role="region" and an aria-label or aria-labelledby'
		).toEqual([]);
	});

	it('the legal tree is in the scan, not silently outside it', () => {
		// The rule above used to skip `routes/legal/` by path. Pin that its
		// scrollers are now among the elements it actually inspects, so a glob
		// or path change that drops them fails here instead of passing empty.
		const legalStops = files
			.filter((f) => f.path.startsWith('routes/legal/'))
			.flatMap((f) => collectElements(parse(f.source, { modern: true }).fragment))
			.filter((el) => {
				const cls = attr(el, 'class');
				return cls !== undefined && staticValue(cls) === 'table-scroll';
			});
		expect(legalStops.length).toBeGreaterThanOrEqual(19);
		for (const el of legalStops) {
			expect(attr(el, 'role') && staticValue(attr(el, 'role')!)).toBe('region');
			expect(attr(el, 'aria-labelledby')).toBeDefined();
		}
	});

	it('no other file hand-rolls a .grid-container', () => {
		const offenders = files
			.filter((f) => f.path !== DATA_TABLE)
			.filter((f) => gridContainers(f.source).length > 0)
			.map((f) => f.path);
		expect(offenders, 'use <DataTable> rather than copying its container').toEqual([]);
	});
});
