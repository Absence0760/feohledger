// Typed helpers for the multi-entity (subsidiary) admin surface. Routes
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce) — never raw fetch. Backend: `backend/app/api/entities.py`.
//
// Gates mirror the router exactly: the LIST is open to any authenticated user
// (the sidebar switcher needs it), every mutation is admin-only. The refusals
// are load-bearing and arrive as an ApiError whose message is the backend's
// own `detail` — a duplicate slug is a 409, deactivating the default is a 400
// — so callers render `e.message` rather than inventing their own copy.
//
// The row shape is the store's `Entity`: one definition, imported here as a
// type (erased at build) so the admin page and the switcher can't drift.
import { api } from '#lib/api.ts';
import type { Entity } from '#lib/stores/entity.svelte.ts';

export type { Entity };

export interface EntityCreate {
	name: string;
	slug: string;
	/** ISO 4217, 3 letters. Omit to inherit the org's reporting currency. */
	currency?: string | null;
}

export interface EntityUpdate {
	name?: string;
	currency?: string | null;
	is_active?: boolean;
}

/** This tenant's entities, default first then alphabetical (backend order). */
export function listEntities(activeOnly = false): Promise<Entity[]> {
	return api.get<Entity[]>(`/api/entities${activeOnly ? '?active_only=true' : ''}`);
}

/** Create a subsidiary. Admin-only; 409 when the slug is already taken,
 *  400 when the slug isn't lowercase-alphanumeric-with-hyphens. */
export function createEntity(body: EntityCreate): Promise<Entity> {
	return api.post<Entity>('/api/entities', body);
}

/** Rename / re-denominate / (de)activate. Admin-only; 400 when the target is
 *  the default entity and `is_active: false` — the default is the home for
 *  un-scoped and new rows, so it must stay active. */
export function updateEntity(id: string, body: EntityUpdate): Promise<Entity> {
	return api.patch<Entity>(`/api/entities/${id}`, body);
}

/** Make this entity the tenant's default. Admin-only; idempotent no-op when it
 *  already is, 400 when the target is inactive. */
export function setDefaultEntity(id: string): Promise<Entity> {
	return api.post<Entity>(`/api/entities/${id}/set-default`, {});
}
