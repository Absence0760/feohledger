import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';

// LoadError reaches the i18n store, which reads `$app/env` — a SvelteKit
// virtual module the plain vitest config does not provide.
vi.mock('$app/env', () => ({ browser: false }));

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

describe('SupplierChatThread copy comes from the catalogue', () => {
	const now = new Date().toISOString();
	const msg = (created_at: string) => ({
		id: 'm1',
		author_role: 'supplier',
		author_name: null,
		body: 'Hello',
		created_at,
		attachments: []
	});

	it('labels the status pill and keeps the wire value on data-status', () => {
		const out = html({ thread: { id: 't', status: 'resolved', messages: [] }, surface: 'ap' });
		expect(out).toMatch(/data-status="resolved"[^>]*>Resolved</);
		expect(out).toContain('aria-label="Supplier chat"');
		expect(out).toContain('This thread is resolved.');
	});

	it('shows an unknown status as-is rather than dropping it', () => {
		const out = html({ thread: { id: 't', status: 'archived', messages: [] } });
		expect(out).toMatch(/data-status="archived"[^>]*>archived</);
	});

	it('renders the composer labels and the unknown-author fallback', () => {
		const out = html({ thread: { id: 't', status: 'open', messages: [msg(now)] } });
		expect(out).toContain('placeholder="Write a message…"');
		expect(out).toContain('aria-label="Write a message"');
		expect(out).toContain('>Unknown<');
		expect(out).toMatch(/>\s*Attach\s*</);
	});

	it('puts message times in a <time> with the exact instant, through Intl', () => {
		const out = html({ thread: { id: 't', status: 'open', messages: [msg(now)] } });
		expect(out).toContain(`datetime="${now}"`);
		// Under a minute: `Intl.RelativeTimeFormat`'s "now", not a hand-built
		// English "just now".
		expect(out).not.toContain('just now');
		const rel = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' }).format(
			0,
			'second'
		);
		expect(out).toContain(`>${rel}</time>`);
	});

	it('shows an older message as a localized date', () => {
		const old = '2020-01-15T12:00:00Z';
		const out = html({ thread: { id: 't', status: 'open', messages: [msg(old)] } });
		const expected = new Date(old).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		});
		expect(out).toContain(`>${expected}</time>`);
	});
});
