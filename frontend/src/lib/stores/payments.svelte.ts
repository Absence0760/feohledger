import type { Payment } from '#lib/types/payment.ts';
import { api } from '#lib/api.ts';
import { appendUnique } from '#lib/utils/pagination.ts';
import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

interface PaymentListResponse {
	items: Payment[];
	total: number;
	page: number;
	page_size: number;
}

const PAGE_SIZE = 20;

function createPaymentStore() {
	let payments = $state<Payment[]>([]);
	let loading = $state(false);
	let total = $state(0);
	let page = $state(1);
	// The active filter params (status/method/search/amount), minus paging —
	// remembered so loadMore() requests the next page with the same filters.
	let lastParams: Record<string, string> = {};
	// Did the most recent (non-append) load fail? The list empty-state reads
	// this: without it a 500 / offline backend leaves `all` empty and the table
	// renders "No … match your filters." — an outage indistinguishable from a
	// filter that matched nothing. Set only when this is still the newest
	// request (`isCurrentRequest`, the same rule the `loading` flag uses), and
	// the error is re-thrown so callers that await a refresh keep their own
	// error handling.
	let errored = $state(false);

	// Sequences every `load()` call (fetch and loadMore alike — one shared
	// counter, latest-issued wins) so a slow response for an earlier
	// search/filter can't land after a faster later one and clobber the list.
	// This store exposes no local-mutation helper — every /payments mutation
	// re-fetches through `load()` — so it needs no `supersedeInFlight()` call.
	const fetchSequence = createRequestSequencer();

	async function load(params: Record<string, string>, opts: { append?: boolean; nextPage?: number } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = fetchSequence.start();
		loading = true;
		try {
			const query = new URLSearchParams({
				...params,
				page: String(nextPage),
				page_size: String(PAGE_SIZE),
			}).toString();
			const res = await api.get<PaymentListResponse>(`/api/payments?${query}`);
			if (!fetchSequence.canCommit(token)) return; // superseded by a newer load
			payments = opts.append ? appendUnique(payments, res.items) : res.items;
			total = res.total;
			page = nextPage;
			if (!opts.append) errored = false;
		} catch (err) {
			if (!opts.append && fetchSequence.isCurrentRequest(token)) errored = true;
			throw err;
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	async function fetch(params?: Record<string, string>) { // noqa: raw-fetch-in-component — store method name; routes through api.get
		lastParams = params ?? {};
		await load(lastParams);
	}

	async function loadMore() {
		await load(lastParams, { append: true, nextPage: page + 1 });
	}

	return {
		get all() { return payments; },
		get loading() { return loading; },
		get total() { return total; },
		get errored() { return errored; },
		get hasMore() { return payments.length < total; },
		fetch,
		loadMore,
	};
}

export const paymentStore = createPaymentStore();
