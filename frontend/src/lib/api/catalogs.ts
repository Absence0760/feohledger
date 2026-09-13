// Typed helpers for the catalog + guided-buying endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce). Mirrors the pattern of `src/lib/api/expenses.ts`.
import { api } from '$lib/api';
import type {
	Catalog,
	CatalogCreate,
	CatalogItem,
	CatalogItemCreate,
	CatalogListResponse,
	GuidedBuyingSuggestion,
	PunchoutConvertResponse,
	PunchoutSession,
	PunchoutStartResponse
} from '$lib/types/catalog';

export interface CatalogListParams {
	catalog_type?: string;
	is_active?: boolean;
	is_preferred?: boolean;
	search?: string;
	page?: number;
	page_size?: number;
}

// The GL picker's option type + loader are owned by `api/glAccounts.ts` /
// `types/glAccount.ts` (the chart-of-accounts surface), re-exported here so the
// catalog pages keep one import. They used to be declared here AND in
// `api/expenses.ts` — two hand-written guesses at one endpoint's payload.
export type { GlAccountOption } from '$lib/types/glAccount';

// The vendor picker's option type + loader live with the rest of the vendor
// surface in `api/vendors.ts`, and `ui/VendorPicker.svelte` is now the only
// caller — a catalog surface no longer fetches vendors itself, so there is
// nothing left for this module to re-export.

export function listCatalogs(params: CatalogListParams = {}): Promise<CatalogListResponse> {
	const qs = new URLSearchParams();
	if (params.catalog_type) qs.set('catalog_type', params.catalog_type);
	if (params.is_active !== undefined) qs.set('is_active', String(params.is_active));
	if (params.is_preferred !== undefined) qs.set('is_preferred', String(params.is_preferred));
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<CatalogListResponse>(`/api/catalogs?${qs}`);
}

export function getCatalog(id: string): Promise<Catalog> {
	return api.get<Catalog>(`/api/catalogs/${id}`);
}

export function createCatalog(body: CatalogCreate): Promise<Catalog> {
	return api.post<Catalog>('/api/catalogs', body);
}

export function updateCatalog(id: string, body: Partial<CatalogCreate>): Promise<Catalog> {
	return api.patch<Catalog>(`/api/catalogs/${id}`, body);
}

export function deleteCatalog(id: string): Promise<void> {
	return api.delete(`/api/catalogs/${id}`);
}

// --- Nested catalog items ---

export function listCatalogItems(catalogId: string): Promise<CatalogItem[]> {
	return api.get<CatalogItem[]>(`/api/catalogs/${catalogId}/items`);
}

export function createCatalogItem(catalogId: string, body: CatalogItemCreate): Promise<CatalogItem> {
	return api.post<CatalogItem>(`/api/catalogs/${catalogId}/items`, body);
}

export function updateCatalogItem(
	itemId: string,
	body: Partial<CatalogItemCreate>
): Promise<CatalogItem> {
	return api.patch<CatalogItem>(`/api/catalogs/items/${itemId}`, body);
}

export function deleteCatalogItem(itemId: string): Promise<void> {
	return api.delete(`/api/catalogs/items/${itemId}`);
}

// --- Guided buying ---

export interface GuidedBuyingParams {
	category?: string;
	vendor_id?: string;
	q?: string;
}

export function guidedBuying(params: GuidedBuyingParams = {}): Promise<GuidedBuyingSuggestion> {
	const qs = new URLSearchParams();
	if (params.category) qs.set('category', params.category);
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.q) qs.set('q', params.q);
	return api.get<GuidedBuyingSuggestion>(`/api/catalogs/guided-buying?${qs}`);
}

// --- Punch-out (live cXML/OCI round-trip) ---

/** Start a punch-out session against a `punchout` catalog → supplier start URL. */
export function startPunchout(catalogId: string): Promise<PunchoutStartResponse> {
	return api.post<PunchoutStartResponse>(`/api/catalogs/${catalogId}/punchout/start`, {});
}

/** Fetch a punch-out session (start state, and the returned cart once posted). */
export function getPunchoutSession(sessionId: string): Promise<PunchoutSession> {
	return api.get<PunchoutSession>(`/api/catalogs/punchout/sessions/${sessionId}`);
}

/** Convert a returned session's cart into a purchase requisition (idempotent). */
export function convertPunchoutSession(sessionId: string): Promise<PunchoutConvertResponse> {
	return api.post<PunchoutConvertResponse>(
		`/api/catalogs/punchout/sessions/${sessionId}/convert`,
		{}
	);
}

// --- Lookups reused from existing endpoints ---

export { listGlAccounts } from '$lib/api/glAccounts';

