import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * `invoiceStore`'s failure flags.
 *
 * - `errored` describes the latest list load, so it clears the moment a fresh
 *   load starts — a Retry shows the loading line, not the old failure. A
 *   failed fresh load empties the list, so there are no stale rows for that
 *   Retry to redraw (unless a local edit since owns them).
 * - `loadMore` never rejects: a failed page records `loadMoreErrored` and
 *   keeps the rows already shown, so no caller has to remember a `.catch`.
 */

const get = vi.fn();
vi.mock('#lib/api.ts', () => ({ api: { get: (...a: unknown[]) => get(...a) } }));

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void };
function deferred<T>(): Deferred<T> {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

const page = (ids: string[], total: number, n = 1) => ({
	items: ids.map((id) => ({ id })),
	total,
	page: n,
	page_size: 20
});

async function freshStore() {
	vi.resetModules();
	const { invoiceStore } = await import('./invoices.svelte.ts');
	return invoiceStore;
}

beforeEach(() => get.mockReset());

describe('invoiceStore.errored', () => {
	it('clears as soon as a new load starts, not only when it succeeds', async () => {
		const store = await freshStore();
		get.mockRejectedValueOnce(new Error('offline'));
		await expect(store.fetch()).rejects.toThrow('offline'); // noqa: raw-fetch-in-component — store method, routes through api client
		expect(store.errored).toBe(true);

		const retry = deferred<ReturnType<typeof page>>();
		get.mockReturnValueOnce(retry.promise);
		const pending = store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client
		expect(store.errored).toBe(false);
		expect(store.loading).toBe(true);

		retry.resolve(page(['a'], 1));
		await pending;
		expect(store.errored).toBe(false);
		expect(store.all.map((i) => i.id)).toEqual(['a']);
	});

	it('a failed fresh load drops the previous rows, so a Retry cannot redraw them', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page(['old'], 1));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client

		get.mockRejectedValueOnce(new Error('offline'));
		await expect(store.fetch({ status: 'paid' })).rejects.toThrow('offline'); // noqa: raw-fetch-in-component — store method, routes through api client
		expect(store.all).toEqual([]);
		expect(store.total).toBe(0);

		const retry = deferred<ReturnType<typeof page>>();
		get.mockReturnValueOnce(retry.promise);
		const pending = store.fetch({ status: 'paid' }); // noqa: raw-fetch-in-component — store method, routes through api client
		expect(store.errored).toBe(false);
		expect(store.all).toEqual([]);
		retry.resolve(page(['new'], 1));
		await pending;
		expect(store.all.map((i) => i.id)).toEqual(['new']);
	});

	it('a failure superseded by a local edit keeps the edited rows', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page(['a'], 1));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client

		const slow = deferred<ReturnType<typeof page>>();
		get.mockReturnValueOnce(slow.promise);
		const pending = store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client
		store.patchLocal('a', { id: 'a' });
		slow.reject(new Error('offline'));
		await expect(pending).rejects.toThrow('offline');
		expect(store.errored).toBe(true);
		expect(store.all.map((i) => i.id)).toEqual(['a']);
	});
});

describe('invoiceStore.loadMore', () => {
	it('resolves on failure, keeps the rows, and sets loadMoreErrored', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page(['a'], 2));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client

		get.mockRejectedValueOnce(new Error('500'));
		await expect(store.loadMore()).resolves.toBeUndefined();
		expect(store.loadMoreErrored).toBe(true);
		expect(store.errored).toBe(false);
		expect(store.all.map((i) => i.id)).toEqual(['a']);
		expect(store.loading).toBe(false);

		get.mockResolvedValueOnce(page(['b'], 2, 2));
		await store.loadMore();
		expect(store.loadMoreErrored).toBe(false);
		expect(store.all.map((i) => i.id)).toEqual(['a', 'b']);
	});

	it('a page superseded by a newer load reports no failure', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page(['a'], 2));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client

		const more = deferred<ReturnType<typeof page>>();
		get.mockReturnValueOnce(more.promise);
		const pendingMore = store.loadMore();
		get.mockResolvedValueOnce(page(['c'], 1));
		await store.fetch({ status: 'paid' }); // noqa: raw-fetch-in-component — store method, routes through api client

		more.reject(new Error('late failure'));
		await pendingMore;
		expect(store.loadMoreErrored).toBe(false);
		expect(store.all.map((i) => i.id)).toEqual(['c']);
	});

	it('a fresh list load clears a previous Load-more failure', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page(['a'], 2));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client
		get.mockRejectedValueOnce(new Error('500'));
		await store.loadMore();
		expect(store.loadMoreErrored).toBe(true);

		get.mockResolvedValueOnce(page(['a'], 2));
		await store.fetch(); // noqa: raw-fetch-in-component — store method, routes through api client
		expect(store.loadMoreErrored).toBe(false);
	});
});
