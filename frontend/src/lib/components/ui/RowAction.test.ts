import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import RowAction from './RowAction.svelte';

/**
 * `busy` is the in-flight state of a row action's own request. It must mark
 * the button `aria-busy` and `aria-disabled` (not native `disabled`, which
 * would drop the keyboard user's focus) and must stay absent when not set, so
 * every existing call site renders exactly as before. The click swallowing is
 * covered end to end by `tests-e2e/admin/api-keys.spec.ts`.
 */

const label = createRawSnippet(() => ({ render: () => '<span>Confirm</span>' }));

function button(props: Record<string, unknown>): string {
	const html = render(RowAction, { props: { children: label, ...props } }).body;
	const open = html.match(/<(button|a)\b[^>]*>/);
	if (!open) throw new Error(`no control rendered: ${html}`);
	return open[0];
}

describe('RowAction busy', () => {
	it('marks a busy button aria-busy and aria-disabled, not natively disabled', () => {
		const tag = button({ variant: 'danger', armed: true, busy: true });
		expect(tag).toMatch(/aria-busy="true"/);
		expect(tag).toMatch(/aria-disabled="true"/);
		expect(tag).toMatch(/\bbusy\b/);
		expect(tag).not.toMatch(/\sdisabled(=|\s|>)/);
	});

	it('renders no busy attributes when busy is omitted', () => {
		const tag = button({ variant: 'danger', armed: true });
		expect(tag).not.toMatch(/aria-busy/);
		expect(tag).not.toMatch(/aria-disabled/);
	});

	it('still renders native disabled from the disabled prop', () => {
		expect(button({ disabled: true })).toMatch(/\sdisabled(=""|\s|>)/);
	});

	it('ignores busy on the link form', () => {
		const tag = button({ href: '/admin', busy: true });
		expect(tag.startsWith('<a')).toBe(true);
		expect(tag).not.toMatch(/aria-busy/);
	});
});

describe('RowAction reason', () => {
	function html(props: Record<string, unknown>): string {
		return render(RowAction, { props: { children: label, ...props } }).body;
	}

	it('renders the reason above the button and points aria-describedby at it', () => {
		const out = html({ disabled: true, reason: 'A colleague must approve this.', reasonId: 'why-1' });
		const note = out.indexOf('id="why-1"');
		const btn = out.indexOf('<button');
		expect(note).toBeGreaterThanOrEqual(0);
		expect(note).toBeLessThan(btn);
		expect(out).toContain('A colleague must approve this.');
		expect(button({ disabled: true, reason: 'x', reasonId: 'why-1' })).toMatch(
			/aria-describedby="why-1"/
		);
	});

	it('generates a note id when none is given, and keeps an explicit describedby', () => {
		const out = html({ disabled: true, reason: 'x', describedby: 'other' });
		const id = out.match(/<span[^>]*\bid="([^"]+)"/)?.[1];
		expect(id).toBeTruthy();
		// Read from the same render: a second render may mint a different id.
		const tag = out.match(/<button\b[^>]*>/)?.[0] ?? '';
		expect(tag).toContain(`aria-describedby="${id} other"`);
	});

	it('renders no note and no describedby without a reason', () => {
		const out = html({ disabled: true });
		expect(out).not.toContain('refusal-note');
		expect(button({ disabled: true })).not.toMatch(/aria-describedby/);
	});
});
