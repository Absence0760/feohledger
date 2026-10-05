import { getTenantStorageKey } from '#lib/tenant.ts';

/**
 * Multi-entity (subsidiary) selection — the client half of the backend's
 * `X-Entity-ID` request scoping (see `docs/multi-entity.md`).
 *
 * The selection is the literal {@link ALL_ENTITIES} (consolidated view across
 * every subsidiary) or a specific entity UUID. It persists in localStorage
 * **keyed by tenant**, so switching hosts never carries one tenant's entity id
 * into another (which the backend would reject with a 400 on every request).
 * The key comes from `#lib/tenant.ts::getTenantStorageKey` — the slug on a
 * platform subdomain, the hostname on a tenant's vanity custom domain, where
 * there is no slug in the URL at all but the host still maps 1:1 to a tenant.
 * Reading `getTenantSlug()` here would have silently disabled entity
 * persistence on every vanity host. `api.ts` reads {@link getSelectedEntityId}
 * on each call and only sends the header when a specific entity is chosen —
 * absent header means consolidated, identical to a client that predates
 * multi-entity.
 */

export const ALL_ENTITIES = 'all';

function storageKey(): string | null {
	const tenant = getTenantStorageKey();
	return tenant ? `selected_entity_id:${tenant}` : null;
}

/** The raw stored selection: a UUID or {@link ALL_ENTITIES} (the default). */
export function getStoredEntitySelection(): string {
	if (typeof window === 'undefined') return ALL_ENTITIES;
	const key = storageKey();
	if (!key) return ALL_ENTITIES;
	return localStorage.getItem(key) || ALL_ENTITIES;
}

/**
 * The entity UUID to scope requests to, or `null` for the consolidated view.
 * `api.ts` sends `X-Entity-ID` iff this returns a non-null id.
 */
export function getSelectedEntityId(): string | null {
	const value = getStoredEntitySelection();
	return value && value !== ALL_ENTITIES ? value : null;
}

export function setStoredEntitySelection(value: string): void {
	if (typeof window === 'undefined') return;
	const key = storageKey();
	if (!key) return;
	if (value === ALL_ENTITIES) {
		localStorage.removeItem(key);
	} else {
		localStorage.setItem(key, value);
	}
}
