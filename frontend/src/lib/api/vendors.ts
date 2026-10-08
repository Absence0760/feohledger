// Typed helpers for the vendor sanctions-screening + risk endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). Mirrors the pattern of `src/lib/api/contracts.ts` / `tax.ts`.
import { api } from '#lib/api.ts';
import type { MatchingIdsResponse } from '#lib/utils/pagination.ts';
import { triggerDownload } from '#lib/utils/download.ts';
import type {
	Vendor,
	VendorCardRevocation,
	SanctionsCheck,
	ScreeningReviewQueueResponse,
	VendorRisk,
	RiskSummaryBucket,
	EnrichmentApplyField,
	VendorEnrichmentResponse,
	VendorEnrichmentApplyResponse,
	VendorConsolidationResponse,
	VendorMergeResponse,
	VendorStatusCounts
} from '#lib/types/vendor.ts';
import type { ImportResult } from '#lib/types/csvImport.ts';

// --- Vendor picker options ---

/**
 * Vendor option from `GET /api/vendors` — picker value is the uuid `id`.
 *
 * The CANONICAL type. `ContractModal`, `RecurringModal` and
 * `VendorStatementReconModal` each re-declared their own structurally-identical
 * copy; they now import this one, so a field added here (as `code` was) reaches
 * every picker instead of three of five.
 */
export interface VendorOption {
	id: string;
	name: string;
	code?: string | null;
}

/** One page of vendor options plus the size of the whole matching set. */
export interface VendorOptionPage {
	items: VendorOption[];
	total: number;
}

/**
 * One page of vendor picker options, filtered SERVER-side.
 *
 * This replaces a `listVendors()` that asked for `page_size=100` and rendered
 * page 1. Two surfaces used it and three others walked every page on mount; all
 * five fed a native `<select>`, which has no search — so on a tenant past the
 * cap the remaining vendors were simply unreachable, everywhere, with nothing
 * on screen saying so. `search=` is the reach mechanism (it matches name / code
 * / email in `_vendor_list_filters`), and `total` is what lets the picker say
 * out loud that it is showing a subset.
 *
 * Deliberately does NOT swallow its errors the way `listVendors` did. Degrading
 * a 403 (`/api/vendors` is admin/ap_manager/cfo — an `ap_clerk` gets one) to an
 * empty array presents "you may not see vendors" as "this tenant has none",
 * which is the same class of silent-subset bug this whole change is about. The
 * picker catches it and says so.
 *
 * Lives here, beside the rest of the vendor surface, rather than in
 * `api/catalogs.ts` where it was first written: five surfaces now want a vendor
 * picker, and a `/discounts` page reaching into the catalogs module for a
 * generic vendor list is the wrong dependency (root `CLAUDE.md` guard rail 10).
 */
export async function searchVendorOptions(
	params: { search?: string; page?: number; page_size?: number } = {}
): Promise<VendorOptionPage> {
	const qs = new URLSearchParams();
	const search = params.search?.trim();
	if (search) qs.set('search', search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 25));
	const res = await api.get<{ items: VendorOption[]; total: number }>(
		`/api/vendors?${qs.toString()}`
	);
	return { items: res.items ?? [], total: res.total ?? 0 };
}

// --- List / select-all-matching / sort ---

export interface VendorListParams {
	search?: string;
	status?: string;
	source?: string;
	sort?: string;
	order?: 'asc' | 'desc';
	page?: number;
	page_size?: number;
}

function vendorQuery(params: VendorListParams): URLSearchParams {
	const qs = new URLSearchParams();
	if (params.search) qs.set('search', params.search);
	if (params.status) qs.set('status', params.status);
	if (params.source) qs.set('source', params.source);
	if (params.sort) qs.set('sort', params.sort);
	if (params.order) qs.set('order', params.order);
	return qs;
}

/**
 * Every vendor id matching the current list filters — `GET /api/vendors/ids`.
 * Backs "select all N matching" the same way `getExpenseIds` /
 * `GET /api/invoices/ids` do: the header checkbox only ever covers the
 * currently-loaded page, so a bulk action over "select all" would otherwise
 * silently skip every row past it.
 */
export function getVendorIds(params: VendorListParams = {}): Promise<MatchingIdsResponse> {
	const qs = vendorQuery(params);
	const query = qs.toString();
	return api.get<MatchingIdsResponse>(`/api/vendors/ids${query ? `?${query}` : ''}`);
}

// --- Bulk operations ---

export interface BulkSkip {
	id: string;
	reason: string;
}

export interface VendorBulkStatusResponse {
	updated: number;
	skipped: BulkSkip[];
	card_revocations?: VendorCardRevocation[];
}

/** Bulk verify (`status: 'active'`) / reject over a hand-picked set of
 * vendors — the bulk counterpart of the single-row verify/reject row
 * actions. `vendor.manage`-gated on the backend. */
export function bulkVendorStatus(
	ids: string[],
	status: 'active' | 'rejected'
): Promise<VendorBulkStatusResponse> {
	return api.post<VendorBulkStatusResponse>('/api/vendors/bulk/status', { ids, status });
}

export interface VendorBulkScreenResponse {
	screened: number;
	skipped: BulkSkip[];
	card_revocations?: VendorCardRevocation[];
}

/** Bulk re-screen against the configured sanctions provider. admin / ap_manager. */
export function bulkScreenVendors(ids: string[]): Promise<VendorBulkScreenResponse> {
	return api.post<VendorBulkScreenResponse>('/api/vendors/bulk/screen', { ids });
}

/** CSV export of a hand-picked set of vendors (name/code/email/phone/status/
 * source/created_at only — never bank details or the raw tax id). */
export async function exportVendorsCsv(ids: string[]): Promise<void> {
	const blob = await api.downloadBlobPost('/api/vendors/bulk/export', { ids });
	triggerDownload(blob, `vendors-export.csv`);
}

// Manual re-screen of a vendor against the sanctions provider. admin / ap_manager.
export function screenVendor(id: string): Promise<Vendor> {
	return api.post<Vendor>(`/api/vendors/${id}/screen`, {});
}

// Sanctions-screening history for a vendor, newest first.
export function getScreeningHistory(id: string): Promise<SanctionsCheck[]> {
	return api.get<SanctionsCheck[]>(`/api/vendors/${id}/screening-history`);
}

// Vendors needing screening review (potential matches / blocked / high risk).
// Paginated on the canonical `page` / `page_size` envelope — the queue is
// unbounded in principle (it grows with the tenant's flagged population), and
// the page's headline counts deliberately do NOT come from it: they read
// `by_screening_status` off `getVendorCounts()`, which asks the whole-set
// question directly instead of filtering whatever happens to be loaded.
export function getScreeningReviewQueue(
	params: { page?: number; page_size?: number; search?: string } = {}
): Promise<ScreeningReviewQueueResponse> {
	const qs = new URLSearchParams();
	if (params.page) qs.set('page', String(params.page));
	if (params.page_size) qs.set('page_size', String(params.page_size));
	// Server-side: a client-side filter over the loaded page would hide every
	// match living on a later one. Matches vendor name + any matched list on
	// the vendor's screening trail.
	if (params.search?.trim()) qs.set('search', params.search.trim());
	const query = qs.toString();
	return api.get<ScreeningReviewQueueResponse>(
		`/api/vendors/screening/review-queue${query ? `?${query}` : ''}`
	);
}

// Whole-set vendor tallies (status chips + the payment-block figure), over the
// entity-scoped population narrowed by the SAME `search` / `source` filters the
// vendor list takes. admin / ap_manager / cfo — exactly the vendor list's gate.
export function getVendorCounts(params?: {
	search?: string;
	source?: string;
}): Promise<VendorStatusCounts> {
	const qs = new URLSearchParams();
	if (params?.search?.trim()) qs.set('search', params.search.trim());
	if (params?.source) qs.set('source', params.source);
	const query = qs.toString();
	return api.get<VendorStatusCounts>(`/api/vendors/counts${query ? `?${query}` : ''}`);
}

// Block / unblock vendor payments. admin / ap_manager.
export function blockVendor(id: string, reason?: string): Promise<Vendor> {
	return api.post<Vendor>(`/api/vendors/${id}/block`, reason ? { reason } : {});
}

export function unblockVendor(id: string): Promise<Vendor> {
	return api.post<Vendor>(`/api/vendors/${id}/unblock`, {});
}

// Reject an unverified / active vendor. The response carries what happened to
// its live virtual cards (`card_revocation`).
export function rejectVendor(id: string): Promise<Vendor> {
	return api.post<Vendor>(`/api/vendors/${id}/reject`, {});
}

// Retry the card leg of a deactivation / block / sanctions match: cancel every
// live, unbooked card still open on an un-payable vendor. 409 on a payable one.
// `vendor.manage` or `vendor.block`.
export function cancelVendorCards(id: string): Promise<VendorCardRevocation> {
	return api.post<VendorCardRevocation>(`/api/vendors/${id}/cancel-cards`, {});
}

// Vendor risk detail + recompute. recompute is admin / ap_manager.
export function getVendorRisk(id: string): Promise<VendorRisk> {
	return api.get<VendorRisk>(`/api/vendors/${id}/risk`);
}

export function recomputeVendorRisk(id: string): Promise<VendorRisk> {
	return api.post<VendorRisk>(`/api/vendors/${id}/risk/recompute`, {});
}

// Risk-level distribution across vendors.
export function getRiskSummary(): Promise<RiskSummaryBucket[]> {
	return api.get<RiskSummaryBucket[]>('/api/vendors/risk/summary');
}

// External firmographics enrichment (D&B / Clearbit / mock). Advisory only —
// returns the looked-up firmographics + a per-field suggestion diff; nothing is
// written back. admin / ap_manager / cfo (the backend `_ENRICH_ROLES`).
export function enrichVendor(id: string): Promise<VendorEnrichmentResponse> {
	return api.post<VendorEnrichmentResponse>(`/api/enrichment/vendors/${id}/enrich`, {});
}

// Apply a steward-selected subset of enrichment suggestions onto the vendor.
// Non-destructive (only the named fields change) + idempotent + audited.
// `tax_id` is never applyable here — only `name` / `address` / `website`.
export function applyVendorEnrichment(
	id: string,
	fields: EnrichmentApplyField[]
): Promise<VendorEnrichmentApplyResponse> {
	return api.post<VendorEnrichmentApplyResponse>(`/api/enrichment/vendors/${id}/apply`, {
		fields
	});
}

// Advisory clusters of likely-duplicate / similar vendors (by tax_id / code /
// fuzzy name), each with a deterministic canonical pick (tax_id masked).
// Read-only, compute-on-read, no mutation. admin / ap_manager / cfo.
export function getVendorConsolidationSuggestions(): Promise<VendorConsolidationResponse> {
	return api.get<VendorConsolidationResponse>('/api/enrichment/vendors/consolidation-suggestions');
}

// Execute a consolidation: fold `duplicateVendorIds` into `canonicalVendorId`.
// Reassigns every vendor FK to the canonical vendor + soft-retires the
// duplicates (status=inactive); idempotent + audited (`vendor.merged`). Gated
// on the granular `vendor.manage` permission. Refuses self-merge / cross-entity
// / unknown vendor (surfaced as the backend's 4xx detail).
export function mergeVendorConsolidation(
	canonicalVendorId: string,
	duplicateVendorIds: string[]
): Promise<VendorMergeResponse> {
	return api.post<VendorMergeResponse>('/api/enrichment/vendors/consolidation/merge', {
		canonical_vendor_id: canonicalVendorId,
		duplicate_vendor_ids: duplicateVendorIds
	});
}

// Day-0 CSV import — bulk-create vendors from a customer export. Dedup by
// `code` first, then case-insensitive `name`; skip-and-report (a bad row
// never aborts the batch). admin / ap_manager. See backend/docs/csv-import.md.
export function importVendorsCsv(file: File): Promise<ImportResult> {
	return api.upload<ImportResult>('/api/vendors/import-csv', file);
}

// --- Create + supplier-portal invite ---

/** Fields the create-vendor modal collects. `bank_details` is deliberately
 *  absent — the backend dual-control-stages it as a `VendorChangeRequest` on
 *  create (fake-new-payee BEC gate), so the create form must not offer it. */
export interface VendorCreatePayload {
	name: string;
	code?: string | null;
	email?: string | null;
	phone?: string | null;
	address?: string | null;
	tax_id?: string | null;
}

/** `POST /api/vendors` — `vendor.manage`. The row lands `active` / `source=manual`. */
export function createVendor(payload: VendorCreatePayload): Promise<Vendor> {
	return api.post<Vendor>('/api/vendors', payload);
}

export interface PortalInvitePayload {
	email: string;
	full_name: string;
}

/** Mirrors `backend/app/schemas/portal.PortalInviteResponse`.
 *
 *  There is deliberately **no** `temp_password`: the backend now delivers the
 *  credential only by email to the portal user's own address. Returning it here
 *  let one `ap_manager` mint a supplier login at an address they controlled in
 *  a single request — the first link of a BEC bank-redirect chain they could
 *  then complete alone. `portal_url` is `null` when `FEOH_TENANT_URL_TEMPLATE`
 *  isn't configured. */
export interface PortalInviteResult {
	user: {
		id: string;
		vendor_id: string;
		email: string;
		full_name: string;
		is_active: boolean;
		must_change_password: boolean;
		last_login_at: string | null;
		created_at: string;
	};
	portal_url: string | null;
}

/** `POST /api/vendors/{id}/portal-users` — admin / ap_manager. 409 if a portal
 *  user with that email already exists. */
export function inviteVendorPortalUser(
	vendorId: string,
	payload: PortalInvitePayload
): Promise<PortalInviteResult> {
	return api.post<PortalInviteResult>(`/api/vendors/${vendorId}/portal-users`, payload);
}
