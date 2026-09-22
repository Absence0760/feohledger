import { api } from '$lib/api';
import {
	ALL_ENTITIES,
	getStoredEntitySelection,
	setStoredEntitySelection,
} from '$lib/entity';

/**
 * Multi-entity (subsidiary) selector store — backs the sidebar entity
 * switcher. Loads the tenant's entities (`GET /api/entities`) and tracks the
 * current selection (a UUID, or {@link ALL_ENTITIES} for the consolidated
 * view). The selection persists via `$lib/entity` (tenant-scoped localStorage);
 * `api.ts` reads it on every request to set `X-Entity-ID`.
 *
 * Switching entity calls {@link select}, which persists the choice and reloads
 * the page so every store/page re-fetches scoped data — pages fetch in their
 * own `$effect`/`onMount`, not in SvelteKit `load`, so a hard reload is the
 * simplest correct way to re-scope the whole app at once.
 *
 * The switcher only renders when the tenant has more than one entity, so a
 * single-entity tenant sees exactly the pre-multi-entity UI.
 *
 * The list is loaded WHOLE (active and inactive alike) because two readers
 * need the archived rows — `/admin/entities` reactivates them, and this store
 * has to be able to name the entity behind a stored selection that has since
 * been retired. What must never include them is the *selection* surface: see
 * {@link EntityStore.activeEntities} and {@link EntityStore.deactivatedSelection}.
 */

export interface Entity {
	id: string;
	name: string;
	slug: string;
	currency: string | null;
	is_default: boolean;
	is_active: boolean;
}

class EntityStore {
	entities = $state<Entity[]>([]);
	/** The current selection: an entity id or {@link ALL_ENTITIES}. */
	selectedId = $state<string>(getStoredEntitySelection());
	loading = $state(false);
	/**
	 * The entity that WAS selected until this load found it deactivated.
	 *
	 * Set by {@link ensureLoaded} at the moment it drops the selection back to
	 * the consolidated view, so the switcher can say what happened to a choice
	 * that would otherwise just vanish from the menu. `null` whenever there is
	 * nothing to explain — including a selection whose entity is gone
	 * altogether, which leaves no name to show.
	 */
	deactivatedSelection = $state<Entity | null>(null);
	#loaded = false;
	#inflight: Promise<void> | null = null;

	/**
	 * The entities a user may actually select — the ACTIVE ones.
	 *
	 * An entity deactivated on `/admin/entities` is archived: it keeps its
	 * history, but nothing new belongs in it. Offering it in the switcher is
	 * not a cosmetic slip — `tenant.py::get_entity_id` validates only that the
	 * id EXISTS, so an inactive id in `X-Entity-ID` is accepted and
	 * `get_write_entity_id` files every new invoice, vendor and payment under
	 * the retired subsidiary. The server cannot be the gate here (reading an
	 * archived entity's history is legitimate), so the selection surface is.
	 */
	get activeEntities(): Entity[] {
		return this.entities.filter((e) => e.is_active);
	}

	/**
	 * True once the tenant has more than one entity — gates the switcher UI.
	 *
	 * Counts ALL entities, not just {@link activeEntities}. Deactivating one
	 * must not make the control disappear: a tenant that retires a subsidiary
	 * still needs the switcher to reach the consolidated view, and to see why
	 * its selection was dropped. Archiving an entity is not a decision to stop
	 * being a multi-entity tenant. The retired entity is filtered out of the
	 * OPTIONS instead — see {@link activeEntities}, which is what the menu
	 * renders.
	 */
	get multiEntity(): boolean {
		return this.entities.length > 1;
	}

	/** The selected Entity object, or `null` for the consolidated view. */
	get selected(): Entity | null {
		if (this.selectedId === ALL_ENTITIES) return null;
		return this.entities.find((e) => e.id === this.selectedId) ?? null;
	}

	/**
	 * The entity a NEW row is filed under — the backend's
	 * `tenant.get_write_entity_id` rule: the selected entity, else the tenant's
	 * default one. `null` until the list has loaded. A create form that has to
	 * say (or scope by) where its row will land reads this rather than
	 * re-deriving the rule — `CreateInvoiceModal` scopes its GL picker to that
	 * entity's chart.
	 */
	get writeEntityId(): string | null {
		return this.selected?.id ?? this.entities.find((e) => e.is_default)?.id ?? null;
	}

	/** Label for the current selection (switcher button text). */
	get selectedLabel(): string {
		return this.selected?.name ?? 'All entities';
	}

	async ensureLoaded(): Promise<void> {
		if (this.#loaded) return;
		if (this.#inflight) return this.#inflight;
		this.loading = true;
		this.#inflight = (async () => {
			try {
				const rows = await api.get<Entity[]>('/api/entities');
				this.entities = rows;
				this.#reconcileSelection(rows);
				this.#loaded = true;
			} catch {
				// Non-fatal: without the list the switcher just doesn't render.
			} finally {
				this.loading = false;
				this.#inflight = null;
			}
		})();
		return this.#inflight;
	}

	/**
	 * Drop a selection this tenant can no longer honour back to the
	 * consolidated view.
	 *
	 * Two cases, one answer:
	 *
	 * - the entity is GONE (removed since the last visit) — every request
	 *   carrying it would 400;
	 * - the entity is INACTIVE (archived on `/admin/entities`) — requests
	 *   carrying it still succeed, which is the dangerous half: the backend
	 *   checks only that the id exists, so new rows keep landing in a
	 *   subsidiary the tenant has retired.
	 *
	 * The fallback is {@link ALL_ENTITIES} rather than the default entity
	 * because it is the one selection that asserts nothing. Silently moving a
	 * reader into the default entity's books swaps a claim they made for one
	 * they didn't; consolidated says exactly what is true — no entity is
	 * selected — and new rows then land on the tenant's default entity
	 * server-side (`tenant.py::get_write_entity_id`), which is where an
	 * un-scoped row belongs anyway.
	 */
	#reconcileSelection(rows: Entity[]): void {
		if (this.selectedId === ALL_ENTITIES) return;
		const current = rows.find((e) => e.id === this.selectedId);
		if (current?.is_active) return;
		// A retired entity is remembered by NAME so the switcher can explain the
		// change; one that no longer exists has no name to show.
		this.deactivatedSelection = current ?? null;
		this.#applySelection(ALL_ENTITIES);
	}

	/** Persist a selection without reloading (internal — used for stale reset). */
	#applySelection(id: string): void {
		this.selectedId = id;
		setStoredEntitySelection(id);
	}

	/**
	 * Switch the active entity. Persists the choice and reloads so every page
	 * re-fetches under the new scope. No-op when the selection is unchanged.
	 */
	select(id: string): void {
		// A fresh choice retires the "your entity was deactivated" notice — it
		// has done its job the moment the user picks a scope themselves.
		this.deactivatedSelection = null;
		if (id === this.selectedId) return;
		this.#applySelection(id);
		if (typeof window !== 'undefined') {
			window.location.reload();
		}
	}

	reset(): void {
		this.entities = [];
		this.deactivatedSelection = null;
		this.selectedId = getStoredEntitySelection();
		this.#loaded = false;
		this.#inflight = null;
	}
}

export const entityStore = new EntityStore();
export { ALL_ENTITIES };
