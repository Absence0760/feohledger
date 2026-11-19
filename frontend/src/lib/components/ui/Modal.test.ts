import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import Modal from './Modal.svelte';

/**
 * The dialog shell's markup, rendered on the server: the `class` and
 * `fullscreen` props the invoice and run-detail dialogs need, and that a
 * caller passing neither gets exactly the shell it had before. The focus
 * trap, Esc / backdrop close and the page-scroll lock run in a browser and are
 * covered by the e2e suite (`tests-e2e/invoices/detail.spec.ts`,
 * `tests-e2e/a11y/modal-inert.spec.ts`).
 */

const children = createRawSnippet(() => ({ render: () => '<p>body</p>' }));

function html(props: Record<string, unknown> = {}): string {
	return render(Modal, {
		props: { ariaLabel: 'Test dialog', onclose: () => {}, children, ...props }
	}).body;
}

function tag(body: string, re: RegExp): string {
	const found = body.match(re);
	if (!found) throw new Error(`no element matching ${re}`);
	return found[0];
}

const dialog = (body: string) => tag(body, /<div\b[^>]*role="dialog"[^>]*>/);
const backdrop = (body: string) => tag(body, /<div\b[^>]*class="backdrop[^"]*"[^>]*>/);

describe('Modal', () => {
	it('renders the plain shell when given neither class nor fullscreen', () => {
		const body = html();
		expect(dialog(body)).toMatch(/aria-modal="true"/);
		expect(dialog(body)).toMatch(/aria-label="Test dialog"/);
		expect(dialog(body)).not.toMatch(/modal-fullscreen/);
		expect(backdrop(body)).not.toMatch(/backdrop-flush/);
	});

	it('puts a caller class on the dialog element beside `modal`', () => {
		const cls = dialog(html({ class: 'invoice-dialog' })).match(/class="([^"]*)"/)![1];
		expect(cls.split(/\s+/)).toEqual(expect.arrayContaining(['modal', 'invoice-dialog']));
	});

	it('fullscreen flushes the backdrop gutter and marks the dialog', () => {
		const body = html({ fullscreen: true });
		expect(dialog(body)).toMatch(/modal-fullscreen/);
		expect(backdrop(body)).toMatch(/backdrop-flush/);
	});

	it('renders nothing while closed', () => {
		expect(html({ open: false })).not.toMatch(/role="dialog"/);
	});
});
