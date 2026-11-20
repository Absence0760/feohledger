import type { ComponentProps } from 'svelte';
import { render } from 'svelte/server';
import { describe, expect, it, vi } from 'vitest';

// The footer reaches the i18n store, which reads `$app/env` — a SvelteKit
// virtual module the plain vitest config does not provide.
vi.mock('$app/env', () => ({ browser: false }));

import LoadMoreFooter from './LoadMoreFooter.svelte';

/**
 * The one paged-list footer: a Load-more button while rows remain unfetched,
 * "Showing all N" only once they are all loaded, and nothing for an empty
 * list or a failed load. The hooks Playwright selects on (`.load-more-row`,
 * `.btn-load-more`, `.load-more-end`, the role name "Load more (…)") are
 * pinned here; clicking through pages is the per-list e2e specs' job
 * (e.g. `tests-e2e/budgets/pagination.spec.ts`).
 */

const base: ComponentProps<typeof LoadMoreFooter> = {
	shown: 20,
	total: 87,
	hasMore: true,
	loading: false,
	onloadmore: () => {},
	moreKey: 'invoices.loadMore',
	allKey: 'invoices.showingAll'
};

function html(props: Record<string, unknown> = {}): string {
	return render(LoadMoreFooter, { props: { ...base, ...props } }).body;
}

function button(props: Record<string, unknown> = {}): string {
	const tag = html(props).match(/<button\b[^>]*>/);
	if (!tag) throw new Error(`no button rendered: ${html(props)}`);
	return tag[0];
}

describe('LoadMoreFooter', () => {
	it('offers Load more with both counts while rows remain', () => {
		const out = html();
		expect(out).toContain('class="load-more-row"');
		expect(out).toMatch(/<button[^>]*class="btn-load-more"/);
		expect(out).toContain('Load more (20 of 87)');
		expect(out).not.toContain('load-more-end');
		expect(out).not.toContain('Showing all');
		expect(button()).toMatch(/type="button"/);
		expect(button()).not.toMatch(/\sdisabled/);
	});

	it('groups both Load-more counts for the locale', () => {
		expect(html({ shown: 1000, total: 12345 })).toContain('Load more (1,000 of 12,345)');
	});

	it('says Showing all only once nothing is left to load', () => {
		const out = html({ shown: 87, hasMore: false });
		expect(out).toMatch(/<span class="load-more-end">Showing all 87 invoices<\/span>/);
		expect(out).not.toContain('btn-load-more');
	});

	it('picks the plural from the raw total and still groups it', () => {
		expect(html({ shown: 1, total: 1, hasMore: false })).toContain('Showing all 1 invoice<');
		expect(html({ shown: 1500, total: 1500, hasMore: false })).toContain(
			'Showing all 1,500 invoices'
		);
	});

	it('renders nothing for an empty list', () => {
		const out = html({ shown: 0, total: 0, hasMore: false });
		expect(out).not.toContain('load-more');
	});

	it('renders nothing under a failed load, in either branch', () => {
		expect(html({ errored: true })).not.toContain('load-more');
		expect(html({ errored: true, hasMore: false })).not.toContain('load-more');
	});

	it('disables the button and says so while its request is in flight', () => {
		const out = html({ loading: true });
		expect(out).toContain('Loading…');
		expect(out).not.toContain('Load more (');
		expect(button({ loading: true })).toMatch(/\sdisabled/);
	});

	it('takes the loading message from loadingKey', () => {
		// A stand-in key with distinct text: the portal passes its own catalogue's.
		expect(html({ loading: true, loadingKey: 'common.saving' })).toContain('Saving…');
	});

	it('can be disabled for another in-flight load without claiming its own', () => {
		const out = html({ disabled: true });
		expect(out).toContain('Load more (20 of 87)');
		expect(button({ disabled: true })).toMatch(/\sdisabled/);
	});

	it('puts testId on the button, and no data-testid without one', () => {
		expect(button({ testId: 'load-more-rebates' })).toMatch(/data-testid="load-more-rebates"/);
		expect(button()).not.toMatch(/data-testid/);
	});
});
