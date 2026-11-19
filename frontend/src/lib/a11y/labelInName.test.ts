import { describe, test, expect } from 'vitest';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import { loadAllMessages } from '#lib/i18n/allMessages.ts';

// WCAG 2.5.3 Label in Name: when a control's accessible name is an
// aria-label, that name must contain the words the control SHOWS, or a
// speech-input user who says what they see ("click Resolve") gets nothing.
//
// The pairing convention the catalogues already follow: an aria-label for a
// control whose visible text is message `<key>` is message `<key>Aria`
// (`supplierChat.resolve` → `supplierChat.resolveAria`). For every such pair,
// in every shipped locale, the Aria text must contain the visible text,
// compared case-insensitively, with `{placeholders}` and a trailing ellipsis
// or colon set aside. An Aria text that interpolates `{action}` is handed
// the visible label at the call site, so `{action}` counts as containing it.
//
// Translators reword freely, which is how this broke: the English
// "Resolve chat thread" became "Chat-Thread als gelöst markieren" in German,
// a fine sentence that no longer says "Lösen".

/** `<key>Aria` pairs that are NOT a visible-label / aria-label pair: the
 *  Aria key names a different control than the one showing `<key>`. */
const NOT_A_PAIR: Record<string, string> = {
	'expenses.reports.rejectAria': 'labels the rejection-reason textarea, not the Reject button',
	'expenses.reports.attachAria': 'labels the expense-id input, not the Attach button',
	'invoices.modal.review.rejectAria': 'labels the rejection-reason textarea, not the Reject button',
	'scheduledReports.modal.createAria': 'names the create dialog, not the Create button',
	'workflows.mgmt.ver.fromAria': 'labels the "from version" select beside a "From" caption',
	'workflows.mgmt.ver.toAria': 'labels the "to version" select beside a "To" caption',
	'assistant.thinkingAria': 'names the typing indicator image, which shows no text',
};

function visibleCore(text: string): string {
	return text
		.replace(/\{[^}]*\}/g, '')
		.trim()
		.replace(/[….:]+$/u, '')
		.trim()
		.toLowerCase();
}

function containsVisible(aria: string, visible: string): boolean {
	if (aria.includes('{action}')) return true;
	return aria.toLowerCase().includes(visibleCore(visible));
}

describe('WCAG 2.5.3 Label in Name: every <key>Aria contains <key>', () => {
	test('the exemption list names keys that exist', async () => {
		const en = await loadAllMessages('en');
		for (const key of Object.keys(NOT_A_PAIR)) {
			expect(key in en, `${key} is not a message key`).toBe(true);
			expect(key.slice(0, -4) in en, `${key} has no visible sibling`).toBe(true);
		}
	});

	test('the supplier chat status buttons are covered (not exempted)', async () => {
		const en = await loadAllMessages('en');
		for (const key of ['supplierChat.resolveAria', 'supplierChat.reopenAria']) {
			expect(key in en, key).toBe(true);
			expect(key in NOT_A_PAIR, key).toBe(false);
		}
	});

	for (const locale of SUPPORTED_LOCALES) {
		test(`${locale}: each aria-label contains its visible label`, async () => {
			const dict = await loadAllMessages(locale);
			const failures: string[] = [];
			for (const [key, aria] of Object.entries(dict)) {
				if (!key.endsWith('Aria') || key in NOT_A_PAIR) continue;
				const visible = (dict as Record<string, string>)[key.slice(0, -4)];
				if (visible === undefined) continue;
				if (!containsVisible(aria, visible)) {
					failures.push(`${key}: "${aria}" does not contain "${visible}"`);
				}
			}
			expect(failures).toEqual([]);
		});
	}
});

// The catalogue check above pairs `<key>Aria` with `<key>`, so it only sees a
// state the catalogue has an Aria key for. A control whose visible text
// switches with its state ("Remove" → "Confirm remove", "Copy" → "Copied")
// under ONE fixed aria-label slips past it: the armed name still says
// "Remove". This scans the markup for that shape — a `<button>` /
// `<RowAction>` with a static `aria-label={m('k')}` (no ternary in it) whose
// body shows two or more message keys behind a condition — so the label is
// switched with the state (or dropped while busy) as the visible text is.

const SVELTE = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** The `{…}` expression starting at `open` (an index of `{`), braces balanced. */
function braced(src: string, open: number): string {
	let depth = 0;
	for (let i = open; i < src.length; i++) {
		if (src[i] === '{') depth++;
		else if (src[i] === '}' && --depth === 0) return src.slice(open, i + 1);
	}
	return src.slice(open);
}

function fixedLabelOverSwitchedText(source: string): string[] {
	const found: string[] = [];
	const control = /<(button|RowAction)\b/g;
	let match: RegExpExecArray | null;
	while ((match = control.exec(source)) !== null) {
		const tag = match[1];
		const close = source.indexOf(`</${tag}>`, match.index);
		if (close < 0) continue;
		const element = source.slice(match.index, close);
		const attr = /(?:aria-label|ariaLabel)=\{/.exec(element);
		if (!attr) continue;
		const expr = braced(element, attr.index + attr[0].length - 1);
		if (expr.includes('?')) continue; // already switched with the state
		const key = /^\{m\('([^']+)'/.exec(expr)?.[1];
		if (!key) continue;
		// The body: after the opening tag's own expressions, whose `>` may sit
		// inside a `{…}` (an arrow function), so skip over those.
		let i = match.index + tag.length + 1;
		while (i < element.length && element[i] !== '>') {
			i = element[i] === '{' ? i + braced(element, i).length : i + 1;
		}
		const body = element.slice(i + 1);
		const keys = new Set([...body.matchAll(/m\('([^']+)'/g)].map((k) => k[1]));
		if (keys.size >= 2 && (body.includes('?') || body.includes('{#if'))) {
			found.push(`${key} over ${[...keys].sort().join(' / ')}`);
		}
	}
	return found;
}

describe('WCAG 2.5.3: a fixed aria-label never sits over state-switched text', () => {
	test('the scan catches the shape and passes a switched label', () => {
		expect(
			fixedLabelOverSwitchedText(
				`<button aria-label={m('a.removeAria')} onclick={() => (x = 1 > 0)}>{armed ? m('a.confirmRemove') : m('a.remove')}</button>`
			)
		).toEqual(['a.removeAria over a.confirmRemove / a.remove']);
		expect(
			fixedLabelOverSwitchedText(
				`<button aria-label={armed ? m('a.confirmRemoveAria') : m('a.removeAria')}>{armed ? m('a.confirmRemove') : m('a.remove')}</button>`
			)
		).toEqual([]);
	});

	test('no component does it', () => {
		const failures: string[] = [];
		for (const [path, source] of Object.entries(SVELTE)) {
			for (const hit of fixedLabelOverSwitchedText(source)) failures.push(`${path}: ${hit}`);
		}
		expect(failures).toEqual([]);
	});
});
