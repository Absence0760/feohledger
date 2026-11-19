import { beforeEach, describe, expect, it, vi } from 'vitest';

// The list-load error contract of `workflowStore`: a failed FRESH load (mount,
// entity switch, Retry) must not leave the previous rows on screen, because the
// route only shows its `LoadError` over an empty list. `#lib/api.ts` reaches
// `$app/env/public` through `#lib/tenant.ts`, which only SvelteKit resolves, so
// it is replaced with the one seam the list path uses.
const get = vi.fn();
const post = vi.fn();

vi.mock('#lib/api.ts', () => ({
	api: {
		get: (...args: unknown[]) => get(...args),
		post: (...args: unknown[]) => post(...args)
	}
}));

function wf(id: string) {
	return { id, name: `WF ${id}`, is_default: false, is_active: true, steps: [] };
}

function page(items: ReturnType<typeof wf>[], total = items.length) {
	return { items, total, page: 1, page_size: 20 };
}

async function freshStore() {
	vi.resetModules();
	const mod = await import('./workflows.svelte.ts');
	return mod.workflowStore;
}

beforeEach(() => {
	get.mockReset();
	post.mockReset();
});

describe('workflowStore list load', () => {
	it('drops the old rows and flags the error when a fresh refetch fails', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page([wf('a'), wf('b')]));
		await store.fetch(); // noqa: raw-fetch-in-component — the store method, which routes through api.get
		expect(store.all.map((w) => w.id)).toEqual(['a', 'b']);

		get.mockRejectedValueOnce(new Error('500'));
		await expect(store.fetch()).rejects.toThrow('500'); // noqa: raw-fetch-in-component — the store method, which routes through api.get
		expect(store.errored).toBe(true);
		expect(store.all).toEqual([]);
		expect(store.total).toBe(0);
		expect(store.loading).toBe(false);
	});

	it('clears the error flag as soon as a fresh load starts', async () => {
		const store = await freshStore();
		get.mockRejectedValueOnce(new Error('500'));
		await expect(store.fetch()).rejects.toThrow(); // noqa: raw-fetch-in-component — the store method, which routes through api.get
		expect(store.errored).toBe(true);

		let settle: (v: unknown) => void = () => {};
		get.mockImplementationOnce(() => new Promise((res) => (settle = res)));
		const retry = store.fetch(); // noqa: raw-fetch-in-component — the store method, which routes through api.get
		expect(store.errored).toBe(false);
		expect(store.loading).toBe(true);
		settle(page([wf('c')]));
		await retry;
		expect(store.all.map((w) => w.id)).toEqual(['c']);
		expect(store.errored).toBe(false);
	});

	it('keeps the loaded rows when Load more fails', async () => {
		const store = await freshStore();
		get.mockResolvedValueOnce(page([wf('a')], 2));
		await store.fetch(); // noqa: raw-fetch-in-component — the store method, which routes through api.get

		get.mockRejectedValueOnce(new Error('500'));
		await expect(store.loadMore()).rejects.toThrow();
		expect(store.all.map((w) => w.id)).toEqual(['a']);
		expect(store.total).toBe(2);
		expect(store.errored).toBe(false);
	});

	it('keeps a local edit applied while the failing load was in flight', async () => {
		const store = await freshStore();
		let fail: (e: unknown) => void = () => {};
		get.mockImplementationOnce(() => new Promise((_res, rej) => (fail = rej)));
		const load = store.fetch(); // noqa: raw-fetch-in-component — the store method, which routes through api.get
		post.mockResolvedValueOnce(wf('new'));
		await store.create({ name: 'New', steps: [] });

		fail(new Error('500'));
		await expect(load).rejects.toThrow();
		expect(store.all.map((w) => w.id)).toEqual(['new']);
		expect(store.errored).toBe(true);
	});
});
