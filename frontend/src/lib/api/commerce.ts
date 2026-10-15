/**
 * `/api/commerce` — the commerce platform connection (admin) and per-receipt
 * inventory sync status (decisions §273). Every request goes through the shared
 * `api` client (Bearer + X-Tenant-Slug + X-Entity-ID).
 *
 * RBAC mirrors `backend/app/api/commerce.py`: connection, locations and SKU
 * mappings are admin-only; the sync read follows the receipt (any signed-in
 * user); a retry is open to the roles that record receipts.
 */
import { api } from '#lib/api.ts';
import type {
	CommerceCatalog,
	CommerceLocationsView,
	CommerceSettingsView,
	CommerceSkuMapping,
	CommerceSync,
	CommerceTestResult
} from '#lib/types/commerce.ts';

const BASE = '/api/commerce';

export const getCommerceCatalog = () => api.get<CommerceCatalog>(`${BASE}/providers`);

export const getCommerceSettings = () => api.get<CommerceSettingsView>(`${BASE}/settings`);

export const saveCommerceSettings = (settings: Record<string, unknown>) =>
	api.put<CommerceSettingsView>(`${BASE}/settings`, { settings });

export const disconnectCommerce = () => api.delete(`${BASE}/settings`);

export const testCommerceConnection = (settings?: Record<string, unknown>) =>
	api.post<CommerceTestResult>(`${BASE}/test`, settings ?? {});

/** The OAuth Connect hook (decisions §273). No provider connects by OAuth yet;
 *  the backend answers a coded 501 the panel shows as-is. */
export const startCommerceOAuth = (provider: string) =>
	api.get<{ authorize_url: string }>(`${BASE}/oauth/${encodeURIComponent(provider)}/authorize`);

export const getCommerceLocations = () => api.get<CommerceLocationsView>(`${BASE}/locations`);

export const saveCommerceLocations = (body: {
	location_id: string | null;
	entity_locations: Record<string, string>;
}) =>
	api.put<{ location_id: string | null; entity_locations: Record<string, string> }>(
		`${BASE}/locations`,
		body
	);

export const listCommerceSkuMappings = () =>
	api.get<{ provider: string | null; items: CommerceSkuMapping[] }>(`${BASE}/sku-mappings`);

export const saveCommerceSkuMapping = (body: { sku: string; provider_item_id: string }) =>
	api.put<CommerceSkuMapping>(`${BASE}/sku-mappings`, body);

export const deleteCommerceSkuMapping = (id: string) =>
	api.delete(`${BASE}/sku-mappings/${encodeURIComponent(id)}`);

export const listCommerceSyncs = (goodsReceiptId: string) =>
	api.get<{ items: CommerceSync[] }>(
		`${BASE}/syncs?goods_receipt_id=${encodeURIComponent(goodsReceiptId)}`
	);

export const retryCommerceSync = (id: string) =>
	api.post<CommerceSync>(`${BASE}/syncs/${encodeURIComponent(id)}/retry`, {});
