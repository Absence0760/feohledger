import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: a tab bar exposes which tab is selected (WCAG 4.1.2).
 *
 * `/payments` and `/expenses` each hand-rolled their tab bar as plain
 * `<button class="tab" class:active={…}>` elements. The selected state lived
 * only in that class — a colour and an underline — so a screen reader
 * announced "Queue, button; History, button; …" and never which list was on
 * screen, and arrow keys did nothing. `ui/Tabs.svelte` already implemented the
 * WAI-ARIA tablist (role=tab, aria-selected, aria-controls, roving tabindex,
 * Arrow/Home/End), and both pages now use it.
 *
 * The rule: a `<button>` carrying the `tab` class outside `ui/Tabs.svelte` must
 * itself carry `role="tab"` and `aria-selected` (`/audit`'s two-way mode toggle
 * is the one such hand-rolled tablist, and does). Prefer the component.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const TABS_COMPONENT = 'lib/components/ui/Tabs.svelte';

interface Attribute {
	type: string;
	name: string;
	value: unknown;
}
interface Element {
	type: string;
	name: string;
	start: number;
	attributes: Attribute[];
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

function attr(el: Element, name: string): Attribute | undefined {
	return el.attributes.find((a) => a.type === 'Attribute' && a.name === name);
}

function staticText(a: Attribute | undefined): string | null {
	if (!a || !Array.isArray(a.value)) return null;
	return (a.value as { type: string; data?: string }[])
		.map((part) => (part.type === 'Text' ? (part.data ?? '') : ''))
		.join('');
}

function unlabelledTabs(path: string, source: string): string[] {
	const ast = parse(source, { modern: true });
	return collectElements(ast.fragment)
		.filter((el) => el.name === 'button')
		.filter((el) => (staticText(attr(el, 'class')) ?? '').split(/\s+/).includes('tab'))
		.filter((el) => staticText(attr(el, 'role')) !== 'tab' || !attr(el, 'aria-selected'))
		.map((el) => `${path}:${source.slice(0, el.start).split('\n').length}`);
}

describe('tab bars expose their selected tab (WCAG 4.1.2)', () => {
	it('the scanner flags a class-only tab and accepts a real one', () => {
		expect(unlabelledTabs('t', `<button class="tab" class:active={a}>A</button>`)).toHaveLength(1);
		expect(
			unlabelledTabs('t', `<button class="tab" role="tab" aria-selected={a}>A</button>`)
		).toEqual([]);
	});

	it('the shared component is the real thing', () => {
		const source = files.find((f) => f.path === TABS_COMPONENT)!.source;
		expect(unlabelledTabs(TABS_COMPONENT, source)).toEqual([]);
	});

	it('no page hand-rolls a class-only tab bar', () => {
		const offenders = files
			.filter((f) => f.path !== TABS_COMPONENT)
			.flatMap((f) => unlabelledTabs(f.path, f.source));
		expect(offenders, 'use ui/Tabs.svelte').toEqual([]);
	});
});
