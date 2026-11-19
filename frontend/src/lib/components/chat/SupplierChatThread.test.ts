import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import SupplierChatThread from './SupplierChatThread.svelte';

/**
 * A failed thread read must say so — never pose as an empty conversation
 * ("No messages yet"), which tells a supplier nobody has written when the read
 * simply failed. `error` + `onretry` are optional so the internal invoice
 * modal, which passes neither, renders exactly as before.
 */

const base = {
	thread: null,
	surface: 'vendor' as const,
	onsend: async () => {}
};

function html(props: Record<string, unknown>): string {
	return render(SupplierChatThread, { props: { ...base, ...props } }).body;
}

describe('SupplierChatThread error state', () => {
	it('renders the error with a Retry button instead of the empty conversation', () => {
		const out = html({ error: 'The conversation could not be loaded.', onretry: () => {} });
		expect(out).toContain('The conversation could not be loaded.');
		expect(out).toContain('data-testid="chat-load-error"');
		expect(out).toMatch(/role="alert"/);
		expect(out).toMatch(/<button[^>]*class="btn-primary/);
		expect(out).not.toContain('No messages yet');
	});

	it('hides the composer while the read is failed', () => {
		const out = html({ error: 'Failed', onretry: () => {} });
		expect(out).not.toContain('data-testid="chat-input"');
		expect(out).not.toContain('data-testid="chat-send"');
	});

	it('still states the failure without a retry handler', () => {
		const out = html({ error: 'Failed' });
		expect(out).toContain('data-testid="chat-load-error"');
		expect(out).toMatch(/role="alert"/);
		expect(out).not.toContain('No messages yet');
	});

	it('shows the loading line, not the error, while a retry is in flight', () => {
		const out = html({ error: 'Failed', onretry: () => {}, loading: true });
		expect(out).not.toContain('data-testid="chat-load-error"');
		expect(out).toContain('Loading');
	});

	it('renders the empty conversation and composer when neither prop is passed', () => {
		const out = html({});
		expect(out).not.toContain('data-testid="chat-load-error"');
		expect(out).toContain('No messages yet');
		expect(out).toContain('data-testid="chat-input"');
	});
});
