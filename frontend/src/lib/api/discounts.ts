// Typed helpers for the Dynamic Discounting & Early-Payment endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). See `#lib/types/discounts` for the response contracts.
import { api } from '#lib/api.ts';
import type {
	DiscountDashboard,
	DiscountOffer,
	DiscountOfferPage,
	DiscountOptimization,
	DiscountRoi,
	DiscountStatusFilter
} from '#lib/types/discounts.ts';
import type { MoneyString } from '#lib/utils/money.ts';

/** KPI roll-up for the dashboard header (captured / missed / open / projected). */
export function getDiscountDashboard(): Promise<DiscountDashboard> {
	return api.get<DiscountDashboard>('/api/discounts/dashboard');
}

export interface OfferListParams {
	/** Maps the UI `missed` bucket to the backend status param; `all`/`missed`
	 *  are sent as-is and the backend resolves `missed` → declined+expired. */
	status?: DiscountStatusFilter;
	scope?: 'invoice' | 'vendor';
	vendorId?: string;
	page?: number;
	pageSize?: number;
}

/** Paginated offer list. Omits `status` when `all`. */
export function listDiscountOffers(params: OfferListParams = {}): Promise<DiscountOfferPage> {
	const qs = new URLSearchParams();
	if (params.status && params.status !== 'all') qs.set('status', params.status);
	if (params.scope) qs.set('scope', params.scope);
	if (params.vendorId) qs.set('vendor_id', params.vendorId);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.pageSize ?? 20));
	return api.get<DiscountOfferPage>(`/api/discounts/offers?${qs.toString()}`);
}

/** Accept an offer, optionally pinning a specific tier by its `days`. Returns
 *  the updated offer (status → accepted). */
export function acceptDiscountOffer(id: string, tierDays?: number): Promise<DiscountOffer> {
	const body = tierDays === undefined ? {} : { tier_days: tierDays };
	return api.post<DiscountOffer>(`/api/discounts/offers/${id}/accept`, body);
}

/** Decline an offer (status → declined). Returns the updated offer. */
export function declineDiscountOffer(id: string): Promise<DiscountOffer> {
	return api.post<DiscountOffer>(`/api/discounts/offers/${id}/decline`, {});
}

/** Per-invoice ROI comparison (savings vs cost-of-capital opportunity cost). */
export function getInvoiceRoi(invoiceId: string): Promise<DiscountRoi> {
	return api.get<DiscountRoi>(`/api/discounts/invoices/${invoiceId}/roi`);
}

/** Budget-constrained optimization: ranks open offers by ROI and greedily
 *  selects within the optional cash budget. */
/**
 * Rank open offers and pick which to capture under a cash budget.
 *
 * `cashBudget` is an **exact decimal string** (`MoneyString`), never a number:
 * the backend's `json.loads` turns a JSON number into a float before any
 * validator runs, so the budget it selected against was the rounded double —
 * and that budget decides which invoices get paid early. `POST /optimize` now
 * refuses a JSON number outright (422). Validate the user's text with
 * `utils/moneyInput.ts` before calling.
 */
export function optimizeDiscounts(cashBudget?: MoneyString): Promise<DiscountOptimization> {
	const body = cashBudget === undefined ? {} : { cash_budget: cashBudget };
	return api.post<DiscountOptimization>('/api/discounts/optimize', body);
}

export interface BulkNegotiationPayload {
	vendorId: string;
	/** `percent` is the EXACT decimal string the user typed — see
	 *  `#lib/types/discounts::normalizeTierPercent`. */
	tiers: { days: number; percent: string }[];
	validUntil?: string | null;
	notes?: string | null;
}

/**
 * Propose ONE vendor-scoped offer across a vendor's open invoices.
 *
 * "Bulk" describes the base, not the batch: the server sums that one vendor's
 * open-invoice balances (`approved` → `payment_scheduled`, in the caller's
 * entity) into a single offer's `base_amount`. There is no multi-vendor
 * variant and no per-row skip-and-report result — the response is the created
 * offer, at status `offered`, and accepting it stays a separate decision
 * behind its own gate.
 *
 * The base is computed server-side, so the caller cannot know it in advance
 * and must not display one it invented; the returned offer is the first honest
 * sighting of that figure.
 *
 * Optional fields are omitted rather than sent as `null`: the request model is
 * `extra="forbid"`, and omission is what "no end date" means to it.
 */
export function bulkNegotiateDiscount(payload: BulkNegotiationPayload): Promise<DiscountOffer> {
	return api.post<DiscountOffer>('/api/discounts/bulk-negotiate', {
		vendor_id: payload.vendorId,
		tiers: payload.tiers,
		...(payload.validUntil ? { valid_until: payload.validUntil } : {}),
		...(payload.notes ? { notes: payload.notes } : {})
	});
}
