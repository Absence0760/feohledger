import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount } from '$lib/utils/money';

// Types for the Procurement / Requisitions surface. Mirrors the JSON returned
// by the `/api/requisitions` endpoints (backend `RequisitionResponse` /
// `RequisitionLineItemResponse`). Money fields arrive as numbers (backend
// `float(...)`); date/datetime fields are ISO strings.

export type RequisitionStatus =
	| 'draft'
	| 'submitted'
	| 'pending_approval'
	| 'approved'
	| 'rejected'
	| 'converted'
	| 'cancelled';

// Every value the `purchase_requisitions.status` column can hold — the full
// mirror of the backend `RequisitionStatus` enum
// (`backend/app/models/procurement.py`). Kept COMPLETE on purpose: `submitted`
// is still a legal *source* state for legacy rows
// (`services/requisition_service.VALID_TRANSITIONS[submitted]` allows
// pending_approval + cancelled, and `services/budget_service.
// OPEN_COMMITMENT_REQ_STATUSES` counts it as committed spend), so such a row
// must still render a badge and still offer Cancel. This is NOT the filter-chip
// list — see `REQUISITION_FILTER_STATUSES`.
export const REQUISITION_STATUSES: RequisitionStatus[] = [
	'draft',
	'submitted',
	'pending_approval',
	'approved',
	'rejected',
	'converted',
	'cancelled'
];

// Statuses no backend transition ever stamps, so a filter chip for them is a
// control that can never return a row. Every writer of
// `PurchaseRequisition.status` in the backend:
//   - `draft`            — the column default on insert
//                          (`models/procurement.py:262`), `create_requisition`
//                          (`api/requisitions.py:208`), intake conversion
//                          (`services/intake_service.py:117`) and punch-out
//                          cart conversion (`api/catalogs.py:518`).
//   - `pending_approval` — `submit_requisition` (`api/requisitions.py:352`).
//                          Submit jumps STRAIGHT here; it never passes through
//                          `submitted` (the module docstring at
//                          `api/requisitions.py:335` still advertises the old
//                          draft → submitted → pending_approval graph).
//   - `approved`         — `approve_requisition` (`api/requisitions.py:379`).
//   - `rejected`         — `reject_requisition` (`api/requisitions.py:398`).
//   - `cancelled`        — `cancel_requisition` (`api/requisitions.py:424`).
//   - `converted`        — `convert_to_po` (`api/requisitions.py:485`).
//
// That is the complete list — `submitted` appears nowhere as a destination.
// Delete the entry below the moment `submit_requisition` starts stamping it
// (i.e. if the two-step draft → submitted → pending_approval graph the docstring
// describes is actually implemented).
const UNREACHABLE_REQUISITION_STATUSES: RequisitionStatus[] = ['submitted'];

// The subset offered as *filter chips* — derived by EXCLUSION from the full
// mirror, so a genuinely new status added above joins the chips by default and
// only a deliberate, justified entry in UNREACHABLE_REQUISITION_STATUSES keeps
// one out. The excluded value still lives in the union and in
// REQUISITION_STATUS_LABEL_KEYS (a legacy row must still render its badge and still
// offer Cancel), and the page appends whatever status is *actively* filtered to
// the chip row, so an explicit `?status=submitted` is never an invisible filter.
export const REQUISITION_FILTER_STATUSES: RequisitionStatus[] = REQUISITION_STATUSES.filter(
	(s) => !UNREACHABLE_REQUISITION_STATUSES.includes(s)
);

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Both surfaces that render a requisition status (the `/requisitions` list page
 * and `RequisitionModal`) are inside the i18n extraction slice, so a hardcoded
 * English map here put a translated Reopen confirm ("Zurück auf „Entwurf“")
 * directly beside an untranslated `Draft` badge. Keyed the same way
 * `notification.ts::EVENT_LABEL_KEYS` is; `Record<RequisitionStatus,
 * MessageKey>` makes a new status a compile error rather than a blank badge,
 * and `requisition.test.ts` proves every key exists in the catalogue.
 *
 * Kept COMPLETE for the same reason `REQUISITION_STATUSES` is — an unreachable
 * `submitted` legacy row must still render a badge (see the note above it).
 */
export const REQUISITION_STATUS_LABEL_KEYS: Record<RequisitionStatus, MessageKey> = {
	draft: 'requisitions.status.draft',
	submitted: 'requisitions.status.submitted',
	pending_approval: 'requisitions.status.pendingApproval',
	approved: 'requisitions.status.approved',
	rejected: 'requisitions.status.rejected',
	converted: 'requisitions.status.converted',
	cancelled: 'requisitions.status.cancelled'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). `Requisition.status` is typed `string` because the API is the
 * source of truth.
 */
export function requisitionStatusLabelKey(status: string): MessageKey | null {
	return REQUISITION_STATUS_LABEL_KEYS[status as RequisitionStatus] ?? null;
}

/**
 * Badge tone per requisition status. Hoisted out of `RequisitionModal`, which
 * is where it was first written and where it left a note saying it belonged
 * here once the list page converted — the shape `frontend/CLAUDE.md` § Badge
 * asks for whenever a status is badged in more than one place.
 *
 * `converted` takes the `erp` tone: the measured purple literal the list page's
 * rule already spelled by hand, doing the same job it does mid-invoice-pipeline
 * — "handed off downstream", here to a PO. Keeping it distinct from `approved`
 * (green) matters: approved is a decision, converted is a decision someone has
 * already acted on.
 *
 * `submitted` and `pending_approval` share `warning` because the requisition is
 * waiting on someone in both. They keep their own labels, so the states stay
 * distinguishable in text (SC 1.4.1) — and `submitted` is unreachable by any
 * backend transition anyway (see UNREACHABLE_REQUISITION_STATUSES), so the
 * pairing collapses nothing a live row can show.
 *
 * `cancelled` is `neutral` (flat), not a grey tint: a withdrawn requisition is
 * the absence of a signal rather than a weak one.
 */
export const REQUISITION_STATUS_TONES: Record<RequisitionStatus, BadgeTone> = {
	draft: 'accent',
	submitted: 'warning',
	pending_approval: 'warning',
	approved: 'success',
	rejected: 'danger',
	converted: 'erp',
	cancelled: 'neutral'
};

export interface RequisitionLineItem {
	id: string;
	line_number: number | null;
	catalog_item_id: string | null;
	item_code: string | null;
	description: string | null;
	quantity: number | null;
	/** Per-unit price — string-Decimal from the backend. The per-line preview
	 *  scales it by `quantity` through `scaleMoney`, exactly. */
	unit_price: MoneyAmount;
	/** Server-computed line total (`float` on the wire — `schemas/requisition.py`). */
	total: MoneyAmount;
	gl_account_id: string | null;
	uom: string | null;
}

export interface Requisition {
	id: string;
	requisition_number: string;
	title: string | null;
	requester_user_id: string;
	department: string | null;
	status: string;
	needed_by: string | null;
	justification: string | null;
	vendor_id: string | null;
	contract_id: string | null;
	budget_id: string | null;
	/** Header total, derived server-side as `sum(quantity * unit_price)`. */
	total: MoneyAmount;
	currency: string;
	notes: string | null;
	submitted_at: string | null;
	approved_at: string | null;
	approved_by: string | null;
	rejection_reason: string | null;
	converted_po_id: string | null;
	line_items: RequisitionLineItem[];
	created_at: string;
	updated_at: string;
}

export interface RequisitionListResponse {
	items: Requisition[];
	total: number;
	page: number;
	page_size: number;
}

/** One currency's slice of `GET /api/requisitions/summary`. */
export interface RequisitionCurrencyTotal {
	currency: string;
	/** Exact decimal string — never parse into a float for arithmetic. */
	total: string;
	count: number;
}

/**
 * Whole-set KPI rollup from `GET /api/requisitions/summary`, over the SAME
 * status/search filters the list ran with.
 *
 * The page's `pendingCount` filtered the LOADED page for `pending_approval`
 * and `periodTotal` reduced over it — so both contradicted the whole-set row
 * count beside them, and `periodTotal` added values across currencies.
 * `by_currency` is grouped, never summed (see `$lib/utils/currencyGroups`).
 */
export interface RequisitionSummary {
	total: number;
	by_status: Record<string, number>;
	by_currency: RequisitionCurrencyTotal[];
}

// `POST /api/requisitions/{id}/convert-to-po`. `created` is false on the
// idempotent replay path (requisition already converted).
export interface ConvertToPoResult {
	requisition_id: string;
	po_id: string;
	po_number: string;
	/** The created PO's money total. */
	total: MoneyAmount;
	/** The code `total` is in — the requisition's, copied onto the PO. `null`
	 *  when the requisition held no well-formed code (decisions §197). */
	currency?: string | null;
	created: boolean;
}

// --- Request (create / update) payload shapes ---

export interface RequisitionLineItemInput {
	line_number?: number | null;
	catalog_item_id?: string | null;
	item_code?: string | null;
	description?: string | null;
	quantity?: number | null;
	unit_price?: MoneyAmount;
	gl_account_id?: string | null;
	uom?: string | null;
}

export interface RequisitionCreate {
	requisition_number: string;
	title: string | null;
	department: string | null;
	needed_by: string | null;
	justification: string | null;
	vendor_id?: string | null;
	contract_id?: string | null;
	budget_id?: string | null;
	currency: string;
	notes: string | null;
	line_items: RequisitionLineItemInput[];
}

export interface RequisitionUpdate {
	requisition_number?: string;
	title?: string | null;
	department?: string | null;
	needed_by?: string | null;
	justification?: string | null;
	vendor_id?: string | null;
	contract_id?: string | null;
	budget_id?: string | null;
	currency?: string;
	notes?: string | null;
	line_items?: RequisitionLineItemInput[];
}
