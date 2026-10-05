import type { Expense } from '#lib/types/expense.ts';
import { listExpenses, type ExpenseListParams } from '#lib/api/expenses.ts';
import { appendUnique } from '#lib/utils/pagination.ts';
import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

function createExpenseStore() {
	let expenses = $state<Expense[]>([]);
	let loading = $state(false);
	let total = $state(0);
	let page = $state(1);
	let lastParams = $state<ExpenseListParams>({});
	// Did the most recent (non-append) load fail? The list empty-state reads
	// this: without it a 500 / offline backend leaves `all` empty and the table
	// renders "No … match your filters." — an outage indistinguishable from a
	// filter that matched nothing. Set only when this is still the newest
	// request (`isCurrentRequest`, the same rule the `loading` flag uses), and
	// the error is re-thrown so callers that await a refresh keep their own
	// error handling.
	let errored = $state(false);

	// Sequences `fetch`/`loadMore` (one shared counter — latest-issued wins) so a
	// slow response for an earlier filter can't land after a faster later one and
	// clobber the list. `upsert`/`remove` mark in-flight fetches stale the same
	// way, so a response issued before a local edit can't revert it.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function fetch(params: ExpenseListParams = {}) { // noqa: raw-fetch-in-component — store method name; routes through listExpenses → api client
		const token = fetchSequence.start();
		loading = true;
		try {
			const res = await listExpenses({ ...params, page: 1, page_size: 20 });
			// Superseded by a newer fetch/loadMore, or by a local edit.
			if (!fetchSequence.canCommit(token)) return;
			expenses = res.items;
			total = res.total;
			page = res.page;
			lastParams = params;
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
			const res = await listExpenses({ ...lastParams, page: page + 1, page_size: 20 });
			if (!fetchSequence.canCommit(token)) return;
			expenses = appendUnique(expenses, res.items);
			total = res.total;
			page = res.page;
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	// Replace one row in place (after an edit / receipt upload / GL recode) so
	// the list reflects the change without a full refetch.
	function upsert(updated: Expense) {
		// A fetch already in flight read the list BEFORE this mutation landed, so
		// its response would revert it (or drop a just-created expense entirely —
		// a create needs no pre-existing row, so it races even the mount fetch).
		fetchSequence.supersedeInFlight();
		const idx = expenses.findIndex((e) => e.id === updated.id);
		if (idx === -1) {
			expenses = [updated, ...expenses];
			total += 1;
		} else {
			expenses = expenses.map((e) => (e.id === updated.id ? updated : e));
		}
	}

	function remove(id: string) {
		fetchSequence.supersedeInFlight();
		if (expenses.some((e) => e.id === id)) {
			expenses = expenses.filter((e) => e.id !== id);
			total = Math.max(0, total - 1);
		}
	}

	return {
		get all() {
			return expenses;
		},
		get loading() {
			return loading;
		},
		get total() {
			return total;
		},
		get page() {
			return page;
		},
		get errored() {
			return errored;
		},
		get hasMore() {
			return expenses.length < total;
		},
		fetch,
		loadMore,
		upsert,
		remove
	};
}

export const expenseStore = createExpenseStore();
