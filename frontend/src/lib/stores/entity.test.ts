import { beforeEach, describe, expect, it, vi } from 'vitest';

// `#lib/api.ts` reaches `$app/env/public` through `#lib/tenant.ts`, which only
// SvelteKit resolves; `#lib/entity.ts` is localStorage. Both are replaced with
// the two seams the store actually uses: the list fetch and the stored choice.
const get = vi.fn();
let stored = 'all';
vi.mock('#lib/api.ts', () => ({ api: { get: (...args: unknown[]) => get(...args) } }));
vi.mock('#lib/entity.ts', () => ({
	ALL_ENTITIES: 'all',
	getStoredEntitySelection: () => stored,
	setStoredEntitySelection: (value: string) => {
		stored = value;
	}
}));

const { entityStore, ALL_ENTITIES } = await import('./entity.svelte.ts');

const EAST = {
	id: 'e-east',
	name: 'Acme East',
	slug: 'east',
	currency: 'USD',
	is_default: false,
	is_active: true
};
const DEFAULT = { ...EAST, id: 'e-default', name: 'Acme HQ', slug: 'hq', is_default: true };

/** A list fetch the test resolves or rejects when it chooses. */
function pendingFetch() {
	const { promise, resolve, reject } = Promise.withResolvers<unknown>();
	get.mockImplementationOnce(() => promise);
	return { resolve, reject };
}

/** Reset the singleton as if the page had just loaded with `selection` stored. */
function startWith(selection: string) {
	stored = selection;
	entityStore.reset();
}

describe('entityStore settled / namePending', () => {
	beforeEach(() => {
		get.mockReset();
	});

	it('is pending a name while the list for a stored entity is in flight', async () => {
		startWith(EAST.id);
		const fetch = pendingFetch();
		const load = entityStore.ensureLoaded();

		expect(entityStore.settled).toBe(false);
		expect(entityStore.namePending).toBe(true);

		fetch.resolve([DEFAULT, EAST]);
		await load;

		expect(entityStore.settled).toBe(true);
		expect(entityStore.namePending).toBe(false);
		expect(entityStore.selected?.name).toBe('Acme East');
	});

	it('never waits on a name for the consolidated view', () => {
		startWith(ALL_ENTITIES);
		pendingFetch();
		void entityStore.ensureLoaded();

		expect(entityStore.settled).toBe(false);
		expect(entityStore.namePending).toBe(false);
	});

	it('stops waiting when the load fails, because the name will not come', async () => {
		startWith(EAST.id);
		const fetch = pendingFetch();
		const load = entityStore.ensureLoaded();
		fetch.reject(new Error('network'));
		await load;

		expect(entityStore.settled).toBe(true);
		expect(entityStore.selected).toBeNull();
		expect(entityStore.namePending).toBe(false);
		// The stored choice survives a failed load: nothing proved it stale.
		expect(entityStore.selectedId).toBe(EAST.id);
	});

	it('settles to the consolidated view when the stored entity is gone', async () => {
		startWith('e-removed');
		get.mockResolvedValueOnce([DEFAULT, EAST]);
		await entityStore.ensureLoaded();

		expect(entityStore.settled).toBe(true);
		expect(entityStore.selectedId).toBe(ALL_ENTITIES);
		expect(entityStore.namePending).toBe(false);
	});

	it('shares one in-flight load and settles once', async () => {
		startWith(EAST.id);
		const fetch = pendingFetch();
		const first = entityStore.ensureLoaded();
		const second = entityStore.ensureLoaded();
		expect(get).toHaveBeenCalledTimes(1);

		fetch.resolve([DEFAULT, EAST]);
		await Promise.all([first, second]);
		expect(entityStore.settled).toBe(true);

		// Loaded: a later call neither refetches nor unsettles.
		await entityStore.ensureLoaded();
		expect(get).toHaveBeenCalledTimes(1);
		expect(entityStore.settled).toBe(true);
	});

	it('reset() makes the next load unsettled again', async () => {
		startWith(EAST.id);
		get.mockResolvedValueOnce([DEFAULT, EAST]);
		await entityStore.ensureLoaded();
		expect(entityStore.settled).toBe(true);

		entityStore.reset();
		expect(entityStore.settled).toBe(false);
		expect(entityStore.namePending).toBe(true);
	});

	it('a load that a reset() superseded lands nowhere', async () => {
		// `/admin/entities` resets and reloads after a create, while the
		// sidebar's first load may still be in flight. The older answer, landing
		// last, must not overwrite the newer list or settle the newer load.
		startWith(EAST.id);
		const stale = pendingFetch();
		const first = entityStore.ensureLoaded();

		entityStore.reset();
		const fresh = pendingFetch();
		const second = entityStore.ensureLoaded();
		expect(get).toHaveBeenCalledTimes(2);

		stale.resolve([DEFAULT]);
		await first;
		expect(entityStore.entities).toEqual([]);
		expect(entityStore.settled).toBe(false);
		expect(entityStore.loading).toBe(true);
		// Still the fresh load: a third caller joins it rather than refetching.
		const third = entityStore.ensureLoaded();
		expect(get).toHaveBeenCalledTimes(2);

		fresh.resolve([DEFAULT, EAST]);
		await Promise.all([second, third]);
		expect(entityStore.entities.map((e) => e.id)).toEqual([DEFAULT.id, EAST.id]);
		expect(entityStore.selectedId).toBe(EAST.id);
		expect(entityStore.settled).toBe(true);
		expect(entityStore.loading).toBe(false);
	});
});
