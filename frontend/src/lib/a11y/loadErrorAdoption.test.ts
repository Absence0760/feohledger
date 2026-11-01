import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: a failed load with a Retry is `ui/LoadError`, not a hand-rolled
 * copy (#635, `docs/component-library.md` § LoadError).
 *
 * About a dozen pages each carried their own `role="alert"` block with a Retry
 * button and a private `*.retry` catalogue key. None moved focus on Retry, so
 * the pressed button unmounted and a keyboard user landed back on `<body>`;
 * none matched each other's styling. LoadError owns that shape — the focus
 * move to `<main>` (or the enclosing dialog), the `common.retry` label — so a
 * new copy is what this refuses.
 *
 * What it detects, per `.svelte` file: an element with a literal
 * `role="alert"` containing a `<button>` whose label is a retry — a `m('…')`
 * call on a key ending `.retry` / `common.retry`, or the literal text "Retry".
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * The deliberate exceptions, each with a focus contract LoadError's in-page
 * shape does not fit.
 */
const ALLOWED = new Set([
	// The primitive itself.
	'/src/lib/components/ui/LoadError.svelte',
	// A popover, not a page: Retry moves focus to the popover's own heading so
	// it stays inside the popover (whose focus-out closes it).
	'/src/lib/components/layout/NotificationBell.svelte',
	// A pre-auth form whose `role="alert"` is a persistent live region wrapping
	// the config banner, beside the field it blocks — there is no `<main>` to
	// focus and the block is not where content would have been.
	'/src/routes/find-workspace/+page.svelte'
]);

type Node = { type: string; [key: string]: unknown };

function isNode(v: unknown): v is Node {
	return !!v && typeof v === 'object' && typeof (v as Node).type === 'string';
}

function walk(node: unknown, visit: (n: Node) => void) {
	if (Array.isArray(node)) {
		for (const child of node) walk(child, visit);
		return;
	}
	if (!isNode(node)) return;
	visit(node);
	for (const [key, value] of Object.entries(node)) {
		if (key === 'parent' || key === 'metadata') continue;
		if (value && typeof value === 'object') walk(value, visit);
	}
}

function isRetryLabel(button: Node): boolean {
	let found = false;
	walk(button.fragment, (n) => {
		if (n.type === 'Text' && /^\s*Retry\s*$/.test(n.data as string)) found = true;
		if (n.type !== 'CallExpression') return;
		const callee = n.callee as Node;
		const [arg] = n.arguments as Node[];
		if (
			callee.type === 'Identifier' &&
			callee.name === 'm' &&
			arg?.type === 'Literal' &&
			typeof arg.value === 'string' &&
			/(^|\.)retry$/.test(arg.value)
		) {
			found = true;
		}
	});
	return found;
}

function handRolledLoadErrors(source: string): number {
	const ast = parse(source, { modern: true }) as unknown as Node;
	let count = 0;
	walk(ast.fragment, (n) => {
		if (n.type !== 'RegularElement') return;
		const role = ((n.attributes as Node[]) ?? []).find(
			(a) => a.type === 'Attribute' && a.name === 'role'
		);
		const value = role?.value;
		const roleText = Array.isArray(value) && value.length === 1 ? (value[0] as Node).data : null;
		if (roleText !== 'alert') return;
		walk(n.fragment, (inner) => {
			if (inner.type === 'RegularElement' && inner.name === 'button' && isRetryLabel(inner)) {
				count += 1;
			}
		});
	});
	return count;
}

describe('a failed load with Retry is ui/LoadError', () => {
	it('detects the hand-rolled shapes', () => {
		expect(
			handRolledLoadErrors(
				`<div class="state error" role="alert"><p>{error}</p><button onclick={load}>{m('x.retry')}</button></div>`
			)
		).toBe(1);
		expect(
			handRolledLoadErrors(`<p role="alert">{e}<button onclick={load}>Retry</button></p>`)
		).toBe(1);
		// A retry of an ACTION (not a load) under its own key is not this shape.
		expect(
			handRolledLoadErrors(
				`<p role="alert">{e}<button onclick={go}>{m('payments.void.card.retrying')}</button></p>`
			)
		).toBe(0);
		expect(handRolledLoadErrors(`<LoadError message={e} onretry={load} />`)).toBe(0);
	});

	it('no component outside the allowlist hand-rolls one', () => {
		const offenders = Object.entries(RAW)
			.filter(([path]) => !ALLOWED.has(path))
			.filter(([, source]) => handRolledLoadErrors(source) > 0)
			.map(([path]) => path);
		expect(offenders).toEqual([]);
	});
});
