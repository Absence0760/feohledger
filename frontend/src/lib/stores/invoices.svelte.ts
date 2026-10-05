import type { Invoice } from '#lib/types/invoice.ts';
import { api } from '#lib/api.ts';
import { appendUnique } from '#lib/utils/pagination.ts';
import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

interface InvoiceListResponse {
	items: Invoice[];
	total: number;
	page: number;
	page_size: number;
}

function createInvoiceStore() {
	let invoices = $state<Invoice[]>([]);
	let loading = $state(false);
	let total = $state(0);
	let page = $state(1);
	let statusCounts = $state<Record<string, number>>({});
	let lastParams = $state<Record<string, string>>({});
	// Did the most recent (non-append) load fail? The list empty-state reads
	// this: without it a 500 / offline backend leaves `all` empty and the table
	// renders "No … match your filters." — an outage indistinguishable from a
	// filter that matched nothing. Set only when this is still the newest
	// request (`isCurrentRequest`, the same rule the `loading` flag uses), and
	// the error is re-thrown so callers that await a refresh keep their own
	// error handling.
	let errored = $state(false);

	// Sequences `fetch`/`loadMore` calls (they share one counter — whichever
	// was issued last wins) so a slow response for an earlier search/filter
	// can't land after a faster later one and clobber the list with stale
	// results. `update`/`patchLocal` mark in-flight fetches stale the same way,
	// so a response issued before a local edit can't overwrite it either.
	const fetchSequence = createRequestSequencer();
	// `fetchCounts` runs on its own sequencer: it fires alongside every list
	// fetch (same filter change), so without one a slow counts response for an
	// old filter could land over a fresh one and leave the chips lying.
	const countsSequence = createRequestSequencer();

	async function fetch(params?: Record<string, string>) { // noqa: raw-fetch-in-component — store method name; routes through api.get
		const token = fetchSequence.start();
		loading = true;
		try {
			const merged = { ...(params ?? {}) };
			if (!merged.page) merged.page = '1';
			if (!merged.page_size) merged.page_size = '20';
			const query = '?' + new URLSearchParams(merged).toString();
			const res = await api.get<InvoiceListResponse>(`/api/invoices${query}`);
			// Superseded by a newer fetch/loadMore, or by a local edit.
			if (!fetchSequence.canCommit(token)) return;
			invoices = res.items;
			total = res.total;
			page = res.page;
			lastParams = params ?? {};
			errored = false;
		} catch (err) {
			if (fetchSequence.isCurrentRequest(token)) errored = true;
			throw err;
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	async function loadMore() {
		const token = fetchSequence.start();
		loading = true;
		try {
			const merged = { ...lastParams };
			merged.page = String(page + 1);
			merged.page_size = String(20);
			const query = '?' + new URLSearchParams(merged).toString();
			const res = await api.get<InvoiceListResponse>(`/api/invoices${query}`);
			// Superseded by a newer fetch/loadMore, or by a local edit.
			if (!fetchSequence.canCommit(token)) return;
			invoices = appendUnique(invoices, res.items);
			total = res.total;
			page = res.page;
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	async function fetchCounts(params?: Record<string, string>) {
		const token = countsSequence.start();
		try {
			// Server-side GROUP BY so the chips stay accurate past the first
			// page of results (a client-side tally over page 1 undercounted).
			// `params` carries the list's population filters (search + advanced
			// + assignee) so the chips describe the same rows the table shows —
			// `status`/`sort`/`order`/`page` are along for the ride and ignored
			// by the endpoint (it must NOT filter by status — that's the
			// dimension it tallies).
			const query = params ? '?' + new URLSearchParams(params).toString() : '';
			const res = await api.get<{ counts: Record<string, number>; total: number }>(
				`/api/invoices/counts${query}`
			);
			if (!countsSequence.canCommit(token)) return;
			statusCounts = res.counts;
		} catch {
			// ignore — counts are non-critical
		}
	}

	async function update(
		id: string,
		// `expected_updated_at` isn't an `Invoice` field — it's the
		// optimistic-concurrency token (see `InvoiceModal.svelte`'s
		// `invoiceFieldPayload()`), so it's typed as an addition, not a `Partial`
		// override.
		changes: Partial<Invoice> & { expected_updated_at?: string }
	) {
		const updated = await api.patch<Invoice>(`/api/invoices/${id}`, changes);
		// A fetch already in flight read the invoice BEFORE this PATCH landed,
		// so its response would revert the edit. Retire it before applying.
		fetchSequence.supersedeInFlight();
		invoices = invoices.map((inv) => (inv.id === id ? updated : inv));
	}

	/** Patch the local list cache in place, with no API call. For callers that
	 *  already mutated the invoice via a dedicated endpoint (e.g. the file
	 *  attach/replace/delete routes) and just need the cached row to reflect it. */
	function patchLocal(id: string, changes: Partial<Invoice>) {
		// Same as `update`: retire any pre-edit fetch so it can't overwrite this.
		fetchSequence.supersedeInFlight();
		invoices = invoices.map((inv) => (inv.id === id ? { ...inv, ...changes } : inv));
	}

	return {
		get all() { return invoices; },
		get loading() { return loading; },
		get total() { return total; },
		get page() { return page; },
		get hasMore() { return invoices.length < total; },
		get errored() { return errored; },
		get statusCounts() { return statusCounts; },
		fetch,
		loadMore,
		fetchCounts,
		update,
		patchLocal,
	};
}

export const invoiceStore = createInvoiceStore();
