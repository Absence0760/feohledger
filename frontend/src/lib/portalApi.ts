/**
 * Supplier-portal HTTP client. Parallel to $lib/api.ts but scoped to a
 * separate token key so the AP app and the portal can't stomp on each other's
 * localStorage (opening both in the same browser would otherwise clobber one).
 */
import { getApiBase, getTenantSlug } from '$lib/tenant';

// Resolved per request, not frozen at module load — see the note in
// `$lib/api.ts`. The supplier portal is served from the SAME hosts as the AP
// app (including a tenant's vanity custom domain), so it must resolve its API
// origin through the same owner rather than keeping a second spelling.
const TOKEN_KEY = 'portal_auth_token';

function getToken(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem(TOKEN_KEY);
}

export function setPortalToken(token: string) {
	localStorage.setItem(TOKEN_KEY, token);
}

export function clearPortalToken() {
	localStorage.removeItem(TOKEN_KEY);
}

export function hasPortalToken(): boolean {
	return !!getToken();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const token = getToken();
	const inHeaders = (init?.headers ?? {}) as Record<string, string>;
	const headers: Record<string, string> = {
		...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
		...inHeaders,
	};
	if (token) headers['Authorization'] = `Bearer ${token}`;
	const tenant = getTenantSlug();
	if (tenant) headers['X-Tenant-Slug'] = tenant;

	const res = await fetch(`${getApiBase()}${path}`, { ...init, headers });

	if (res.status === 401) {
		// Only treat a 401 as a session expiry — clear the token and bounce to
		// the portal login — when we actually sent one. A 401 on an
		// unauthenticated request (the login POST with bad credentials) must
		// surface to the caller so the login page can render the error, not
		// silently full-page-reload it away.
		if (token) {
			clearPortalToken();
			if (typeof window !== 'undefined') window.location.href = '/portal/login';
			throw new Error('Unauthorized');
		}
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || 'Invalid credentials');
	}

	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
	}

	if (res.status === 204) return undefined as T;
	return res.json();
}

async function download(path: string): Promise<Blob> {
	// The remittance endpoint returns application/pdf, not JSON, so it can't go
	// through `request<T>` (which assumes a JSON body). Same auth + tenant
	// headers, but we read the response as a Blob and let the caller save it.
	const token = getToken();
	const headers: Record<string, string> = {};
	if (token) headers['Authorization'] = `Bearer ${token}`;
	const tenant = getTenantSlug();
	if (tenant) headers['X-Tenant-Slug'] = tenant;

	const res = await fetch(`${getApiBase()}${path}`, { headers });
	if (res.status === 401 && token) {
		clearPortalToken();
		if (typeof window !== 'undefined') window.location.href = '/portal/login';
		throw new Error('Unauthorized');
	}
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw new Error(body.detail || `API error ${res.status}`);
	}
	return res.blob();
}

export const portalApi = {
	get: <T>(path: string) => request<T>(path),
	post: <T>(path: string, body: unknown) =>
		request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
	patch: <T>(path: string, body: unknown) =>
		request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
	delete: (path: string) => request<void>(path, { method: 'DELETE' }),
	download,
	upload: <T>(path: string, file: File, fields?: Record<string, string | undefined>) => {
		const form = new FormData();
		form.append('file', file);
		// Optional extra multipart form fields (e.g. a chat attachment body).
		if (fields) {
			for (const [key, value] of Object.entries(fields)) {
				if (value === undefined) continue;
				form.append(key, value);
			}
		}
		return request<T>(path, { method: 'POST', body: form, headers: {} });
	},
};

// ---------------------------------------------------------------------------
// Paged list reads
//
// Every portal list endpoint returns the canonical envelope
// `{items, total, page, page_size}` (`backend/app/api/pagination.py` —
// `page` is 1-based, `page_size` defaults to 20 and is capped at 100). The
// portal pages read `total` and append the next page through the shared
// Load-More control, exactly like the AP app: raising `page_size` alone would
// only move the truncation cliff to `MAX_PAGE_SIZE`, leaving a supplier with
// 101 invoices unable to reach the tail.
// ---------------------------------------------------------------------------

export interface PortalPage<T> {
	items: T[];
	total: number;
	page: number;
	page_size: number;
}

/** Rows a portal list requests per page. Matches the backend default so a
 *  bare `GET /api/portal/<list>` and the UI's first page return the same rows. */
export const PORTAL_PAGE_SIZE = 20;

export interface PortalListParams {
	page?: number;
	page_size?: number;
}

/** GET a paged portal list, threading `?page=&page_size=` (plus any extra
 *  per-list filter) onto the path. Empty / undefined params are dropped; an
 *  array value is appended once per element (e.g. `?status=a&status=b`). */
function portalList<T>(
	path: string,
	params: Record<string, string | number | string[] | undefined> = {},
): Promise<PortalPage<T>> {
	const qs = new URLSearchParams();
	for (const [key, value] of Object.entries(params)) {
		if (value === undefined || value === '') continue;
		if (Array.isArray(value)) {
			for (const v of value) if (v !== '') qs.append(key, String(v));
		} else {
			qs.set(key, String(value));
		}
	}
	const query = qs.toString();
	return portalApi.get<PortalPage<T>>(query ? `${path}?${query}` : path);
}

/** Shared page/page_size pair, defaulted, for a list request. */
function pageParams(params: PortalListParams): { page: number; page_size: number } {
	return { page: params.page ?? 1, page_size: params.page_size ?? PORTAL_PAGE_SIZE };
}

// Money arrives as a JSON number or an exact decimal string depending on the
// field — never parse it, hand it to `formatMoney` / `<Money>`.
export interface PortalInvoiceListItem {
	id: string;
	invoice_number: string;
	amount: number | string;
	currency: string;
	status: string;
	invoice_date: string | null;
	due_date: string | null;
	submitted_at: string;
	file_url: string | null;
	/** AP's reason, present only while `status === 'rejected'` — what to fix
	 *  before resubmitting. Plain text; render escaped, never as HTML. */
	rejection_reason: string | null;
	/** Vendor-facing "what is this waiting on" bucket — `'review' | 'processing'
	 *  | 'erp'` while the invoice is in a processing phase, else null. Rendered
	 *  via `portal.invoices.waitingOn.<bucket>` + `waiting_on_days`. */
	waiting_on: 'review' | 'processing' | 'erp' | null;
	waiting_on_days: number | null;
}

export interface PortalPaymentListItem {
	id: string;
	invoice_id: string;
	invoice_number: string;
	amount: number | string;
	currency: string;
	method: string | null;
	status: string;
	reference: string | null;
	submitted_at: string | null;
	completed_at: string | null;
}

export interface PortalPOListItem {
	id: string;
	po_number: string;
	status: string;
	total: number | string;
	/** The PO's own code; `null` when no source recorded one, and `<Money>`
	 *  then renders the total bare. It used to arrive as `"USD"` for every PO,
	 *  a schema default the handler never overrode (decisions §197). */
	currency: string | null;
	line_item_count: number;
	created_at: string;
}

/** Filter params for the signed-in vendor's own invoice list. `status` carries
 *  the raw internal `InvoiceStatus` values behind a vendor-facing phase chip
 *  (see `$lib/types/portalStatus.PORTAL_INVOICE_PHASES`); `search` is a
 *  substring match on the invoice number. */
export interface PortalInvoiceListParams extends PortalListParams {
	status?: string[];
	search?: string;
	/** `YYYY-MM-DD` inclusive bounds on the submitted date. */
	date_from?: string;
	date_to?: string;
}

/** Revise & resubmit a REJECTED invoice with a corrected file — reuses the
 *  same invoice row (no duplicate flag) and sends it back to AP review. */
export function resubmitPortalInvoice(invoiceId: string, file: File) {
	return portalApi.upload<{ id: string; status: string; message: string }>(
		`/api/portal/invoices/${invoiceId}/resubmit`,
		file
	);
}

/** The signed-in vendor's own invoices (newest first). */
export function listPortalInvoices(params: PortalInvoiceListParams = {}) {
	return portalList<PortalInvoiceListItem>('/api/portal/invoices', {
		...pageParams(params),
		status: params.status,
		search: params.search,
		date_from: params.date_from,
		date_to: params.date_to,
	});
}

/** Filter params for the signed-in vendor's payment history. `status` carries
 *  the raw `payments.status` values behind a vendor-facing phase chip (see
 *  `$lib/types/portalStatus.PORTAL_PAYMENT_PHASES`); `search` matches the paid
 *  invoice's number. */
export interface PortalPaymentListParams extends PortalListParams {
	status?: string[];
	search?: string;
	/** `YYYY-MM-DD` inclusive bounds on the payment-created date. */
	date_from?: string;
	date_to?: string;
}

/** Payments on the signed-in vendor's invoices (newest first). */
export function listPortalPayments(params: PortalPaymentListParams = {}) {
	return portalList<PortalPaymentListItem>('/api/portal/payments', {
		...pageParams(params),
		status: params.status,
		search: params.search,
		date_from: params.date_from,
		date_to: params.date_to,
	});
}

/** Purchase orders owned by the signed-in vendor (newest first). */
export function listPortalPurchaseOrders(params: PortalListParams = {}) {
	return portalList<PortalPOListItem>('/api/portal/purchase-orders', pageParams(params));
}

// ---------------------------------------------------------------------------
// Early-payment discount offers (portal side)
//
// Mirrors the AP-side dynamic-discounting types, but vendor-scoped and without
// any internal actor ids. Money + percent arrive as JSON numbers. Accepting an
// offer only flips its status — it never moves money (the CFO-gated payment run
// still funds it). See backend/docs/dynamic-discounting.md § Supplier portal.
// ---------------------------------------------------------------------------

export type PortalOfferStatus = 'offered' | 'accepted' | 'captured' | 'declined' | 'expired';

export interface PortalDiscountTier {
	days: number;
	percent: number;
	savings: number;
}

export interface PortalDiscountOffer {
	id: string;
	status: PortalOfferStatus;
	scope: 'invoice' | 'vendor';
	invoice_id: string | null;
	invoice_number: string | null;
	base_amount: number;
	currency: string;
	tiers: PortalDiscountTier[];
	best_tier: PortalDiscountTier | null;
	valid_from: string | null;
	valid_until: string | null;
	accepted_tier: PortalDiscountTier | null;
	accepted_at: string | null;
	captured_amount: number | null;
	captured_at: string | null;
	notes: string | null;
	created_at: string;
}

export type PortalDiscountOfferPage = PortalPage<PortalDiscountOffer>;

/** List early-payment discount offers relevant to the signed-in vendor.
 *  `status` is the backend's optional comma-separated `?status=` filter. */
export function listPortalDiscountOffers(
	status?: string,
	params: PortalListParams = {},
): Promise<PortalDiscountOfferPage> {
	return portalList<PortalDiscountOffer>('/api/portal/discount-offers', {
		...pageParams(params),
		status,
	});
}

/** Accept an offer (optionally at a specific tier). Flips status only. */
export function acceptPortalDiscountOffer(
	id: string,
	tierDays?: number,
): Promise<PortalDiscountOffer> {
	return portalApi.post<PortalDiscountOffer>(`/api/portal/discount-offers/${id}/accept`, {
		tier_days: tierDays ?? null,
	});
}

/** Decline an offer. */
export function declinePortalDiscountOffer(id: string): Promise<PortalDiscountOffer> {
	return portalApi.post<PortalDiscountOffer>(`/api/portal/discount-offers/${id}/decline`, {});
}

// --- Notification preferences -------------------------------------------------

/** Vendor-controlled email preferences for their own invoices' lifecycle. */
export interface PortalNotificationPreferences {
	email_on_payment: boolean;
	email_on_rejection: boolean;
}

export function getPortalNotificationPreferences() {
	return portalApi.get<PortalNotificationPreferences>('/api/portal/notification-preferences');
}

/** Partial update — omit a field to leave that preference unchanged. */
export function updatePortalNotificationPreferences(
	update: Partial<PortalNotificationPreferences>
) {
	return portalApi.patch<PortalNotificationPreferences>(
		'/api/portal/notification-preferences',
		update
	);
}

// --- Home summary -------------------------------------------------------------

/** One currency's slice of a portal money rollup.
 *
 *  `total` is an EXACT decimal string from the backend — never parsed into a
 *  number, never added to another currency's total. Hand it to `<Money>`. */
export interface PortalCurrencyTotal {
	currency: string;
	total: string;
	count: number;
}

/** The at-a-glance figures the portal home renders.
 *
 *  Whole-set and vendor-scoped: the counts describe every invoice this supplier
 *  has, not the 20 rows the list page happens to have loaded. Buckets mirror
 *  the collapsed phases in `$lib/types/portalStatus` — a supplier never sees a
 *  raw workflow status. */
export interface PortalSummary {
	invoices_total: number;
	/** Rejected — the only bucket the supplier can move (resubmit). */
	invoices_action_required: number;
	/** Sitting with the customer. */
	invoices_in_progress: number;
	invoices_paid: number;
	invoices_completed: number;
	/** Value still owed, per currency. Deliberately not a "paid" total — that
	 *  figure belongs to the payments page. */
	outstanding_by_currency: PortalCurrencyTotal[];
	open_discount_offers: number;
	pending_change: {
		id: string;
		change_type: string;
		status: string;
		created_at: string;
	} | null;
}

export function getPortalSummary(): Promise<PortalSummary> {
	return portalApi.get<PortalSummary>('/api/portal/summary');
}
