import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard for WCAG 2.2 SC 4.1.3 Status Messages on inline form feedback.
 *
 * The idiom this catches is the one the supplier portal's Company page shipped
 * eight times over:
 *
 *     {#if contactErr}<div class="error">{contactErr}</div>{/if}
 *     {#if contactMsg}<div class="message">{contactMsg}</div>{/if}
 *
 * A sighted user sees "Contact details saved." appear above the form. A
 * screen-reader user hears nothing at all: the text is inserted into the DOM
 * with no live region around it, so the save, the failed bank-change request
 * and the "two-factor enabled" confirmation were all silent. Every other portal
 * page already got this right, which is exactly why nothing noticed — axe has no
 * rule for it, because it cannot know the text appeared *in response* to
 * something.
 *
 * The rule: an element rendered conditionally by an `{#if}` whose class marks it
 * as feedback (`error`, `message`, `msg`, `success`) must be announced — it
 * carries `role="alert"` / `role="status"` / `aria-live` itself, or sits inside
 * an ancestor that does. The ancestor form is the more robust of the two for a
 * polite status: a live region that already exists when its content changes is
 * announced reliably, while one inserted together with its text is not on
 * every screen reader. `role="alert"` is the exception — its insertion IS the
 * announcement — so an error may carry it inline.
 *
 * There is a third announced form, for feedback that REPLACES the control the
 * user just activated (a form swapped for its confirmation): the submit button
 * is gone, focus would otherwise fall to `<body>`, so the page moves focus onto
 * the confirmation instead — `tabindex="-1"` plus a `.focus()` after `tick()`,
 * the `/signup` pattern. Moving focus there both announces it and fixes the
 * lost focus, so an element that is (or contains) a `tabindex="-1"` focus
 * target counts as announced.
 *
 * {@link ALLOWLIST} holds the elements the class names match that are not
 * feedback at all; each says why.
 *
 * Like `imageDragging.test.ts` this walks the Svelte TEMPLATE AST, not the
 * source text, so it tracks real ancestry through every block type and a
 * comment that merely mentions the idiom cannot trip it.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

/** Class tokens that mark an element as inline feedback for the user. */
const FEEDBACK_CLASSES = new Set(['error', 'message', 'msg', 'success']);
const LIVE_ROLES = new Set(['alert', 'status', 'log']);

/**
 * `path:snippet` of elements the class match catches that are NOT a response to
 * something the user just did — there is nothing to announce, because nothing
 * happened. Keyed on the trimmed source line rather than a line number, so an
 * unrelated edit above it cannot silently re-point an entry at a real defect.
 */
const ALLOWLIST: Record<string, string> = {
	// One entry in the invoice's audit timeline: the error text a past
	// extraction run recorded. Historical data rendered as part of a list, not a
	// message about an action taken in this session.
	'lib/components/modals/InvoiceModal.svelte:<span class="activity-detail error">— {entry.details.error}</span>':
		'historical audit-log entry',
	// The card-reveal page's load-failure state. It is the page's whole content
	// on arrival (an expired or unknown link), carries the page's <h1>, and is
	// read as the page is read — it never appears in response to an action.
	'routes/portal/cards/[token]/+page.svelte:<div class="state error">': 'page-load state with its own <h1>'
};

interface Attr {
	type: string;
	name: string;
	value: unknown;
}
interface Element {
	type: 'RegularElement';
	name: string;
	start: number;
	attributes: Attr[];
	fragment: { nodes: unknown[] };
}

/** The literal text of a static attribute, or null when it is dynamic/absent. */
function staticAttr(el: Element, name: string): string | null {
	const attr = el.attributes.find((a) => a.type === 'Attribute' && a.name === name);
	if (!attr) return null;
	if (attr.value === true) return '';
	if (!Array.isArray(attr.value)) return null;
	return (attr.value as { type: string; data?: string }[])
		.map((part) => (part.type === 'Text' ? (part.data ?? '') : ''))
		.join('');
}

function isLive(el: Element): boolean {
	const role = staticAttr(el, 'role');
	if (role !== null && LIVE_ROLES.has(role.trim())) return true;
	return el.attributes.some((a) => a.type === 'Attribute' && a.name === 'aria-live');
}

/** True when the element, or anything inside it, is a `tabindex="-1"` focus target. */
function holdsFocusTarget(node: unknown): boolean {
	if (node === null || typeof node !== 'object') return false;
	if (Array.isArray(node)) return node.some(holdsFocusTarget);
	const record = node as Record<string, unknown>;
	if (record.type === 'RegularElement' && staticAttr(record as unknown as Element, 'tabindex') === '-1') {
		return true;
	}
	return Object.entries(record).some(([key, value]) => key !== 'parent' && holdsFocusTarget(value));
}

function isFeedback(el: Element): boolean {
	const cls = staticAttr(el, 'class');
	if (!cls) return false;
	return cls.split(/\s+/).some((token) => FEEDBACK_CLASSES.has(token));
}

/** Top-level elements of an `{#if}` branch — what appears when it turns true. */
function branchElements(fragment: unknown): Element[] {
	const nodes = (fragment as { nodes?: unknown[] } | null)?.nodes ?? [];
	return nodes.filter(
		(n): n is Element => (n as { type?: string }).type === 'RegularElement'
	);
}

interface SilentFeedback {
	path: string;
	line: number;
	snippet: string;
}

function walk(
	node: unknown,
	insideLive: boolean,
	onIf: (block: Record<string, unknown>, insideLive: boolean) => void
): void {
	if (node === null || typeof node !== 'object') return;
	if (Array.isArray(node)) {
		for (const child of node) walk(child, insideLive, onIf);
		return;
	}
	const record = node as Record<string, unknown>;
	let live = insideLive;
	if (record.type === 'RegularElement' && isLive(record as unknown as Element)) live = true;
	if (record.type === 'IfBlock') onIf(record, live);
	for (const [key, value] of Object.entries(record)) {
		if (key === 'parent') continue;
		walk(value, live, onIf);
	}
}

function scan(path: string, source: string): SilentFeedback[] {
	const ast = parse(source, { modern: true });
	const found: SilentFeedback[] = [];
	walk(ast.fragment, false, (block, insideLive) => {
		if (insideLive) return;
		for (const branch of [block.consequent, block.alternate]) {
			for (const el of branchElements(branch)) {
				if (!isFeedback(el) || isLive(el) || holdsFocusTarget(el)) continue;
				const line = source.slice(0, el.start).split('\n').length;
				const snippet = source.split('\n')[line - 1].trim();
				if (`${path}:${snippet}` in ALLOWLIST) continue;
				found.push({ path, line, snippet });
			}
		}
	});
	return found;
}

describe('inline feedback is announced (WCAG 4.1.3)', () => {
	it('the scanner flags the silent idiom and accepts both announced forms', () => {
		const silent = `{#if msg}<div class="message">{msg}</div>{/if}`;
		const inlineAlert = `{#if err}<div class="error" role="alert">{err}</div>{/if}`;
		const wrapped = `<div role="status">{#if msg}<div class="message">{msg}</div>{/if}</div>`;
		const focused = `{#if done}<p class="success" tabindex="-1" bind:this={el}>ok</p>{/if}`;
		const unrelated = `{#if open}<div class="panel">x</div>{/if}`;
		expect(scan('t.svelte', silent)).toHaveLength(1);
		expect(scan('t.svelte', inlineAlert)).toEqual([]);
		expect(scan('t.svelte', wrapped)).toEqual([]);
		expect(scan('t.svelte', focused)).toEqual([]);
		expect(scan('t.svelte', unrelated)).toEqual([]);
	});

	it('every allowlist entry still matches a real element', () => {
		// A stale entry is a hole waiting for a real defect to land in it.
		const flagged = new Set<string>();
		for (const { path, source } of files) {
			for (const line of source.split('\n')) flagged.add(`${path}:${line.trim()}`);
		}
		expect(Object.keys(ALLOWLIST).filter((k) => !flagged.has(k))).toEqual([]);
	});

	it('no conditionally-rendered feedback element is silent', () => {
		const silent = files.flatMap(({ path, source }) => scan(path, source));
		expect(
			silent.map((s) => `${s.path}:${s.line}  ${s.snippet}`),
			'wrap the message in a live region (role="status") or give an error role="alert"'
		).toEqual([]);
	});
});
