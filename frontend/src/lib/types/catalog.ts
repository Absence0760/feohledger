// Types for the Catalogs surface. Mirrors the JSON returned by the
// `/api/catalogs` endpoints (backend `CatalogResponse` / `CatalogItemResponse`
// / `GuidedBuyingSuggestion`). Money fields arrive as numbers (backend
// `float(...)`); date/datetime fields are ISO strings.
//
// Response money is therefore `MoneyAmount` — honest about the JSON-number
// wire shape while still making `a - b` / `.toFixed()` a type error. Money the
// user TYPES is `MoneyString`: `schemas/catalog.py::CatalogItemCreate` declares
// `unit_price` a `Decimal`, and `json.loads` turns a fractional JSON number
// into a float before any validator can intervene.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount, MoneyString } from '$lib/utils/money';

export type CatalogType = 'internal' | 'punchout';

export const CATALOG_TYPES: CatalogType[] = ['internal', 'punchout'];

// The catalog type is a table cell on the (extracted) `/catalogs` list and a
// `<select>` option in `CatalogModal`, both beside translated copy — so it is
// keyed like every other value map in this tree.
export const CATALOG_TYPE_LABEL_KEYS: Record<CatalogType, MessageKey> = {
	internal: 'catalogs.type.internal',
	punchout: 'catalogs.type.punchout'
};

/** The message key for a catalog type, or `null` for an unrecognised one. */
export function catalogTypeLabelKey(catalogType: string): MessageKey | null {
	return CATALOG_TYPE_LABEL_KEYS[catalogType as CatalogType] ?? null;
}

export interface CatalogItem {
	id: string;
	catalog_id: string;
	sku: string | null;
	name: string;
	description: string | null;
	unit_price: MoneyAmount;
	currency: string;
	uom: string | null;
	vendor_id: string | null;
	gl_account_id: string | null;
	category: string | null;
	is_active: boolean;
	created_at: string;
	updated_at: string;
}

export interface Catalog {
	id: string;
	name: string;
	catalog_type: string;
	vendor_id: string | null;
	/** The supplier's NAME, resolved server-side — the same field `Contract` /
	 *  `RecurringTemplate` / `Reconciliation` already carry. `ui/VendorPicker`
	 *  needs it as `selectedLabel`: the chosen vendor can sit on any page of the
	 *  tenant's set, so an id alone leaves the edit form unable to name it. */
	vendor_name: string | null;
	punchout_url: string | null;
	is_active: boolean;
	is_preferred: boolean;
	description: string | null;
	item_count: number;
	items: CatalogItem[];
	created_at: string;
	updated_at: string;
}

export interface CatalogListResponse {
	items: Catalog[];
	total: number;
	page: number;
	page_size: number;
}

// Payload shapes for create / update (request side).
export interface CatalogCreate {
	name: string;
	catalog_type: CatalogType;
	vendor_id: string | null;
	punchout_url: string | null;
	is_active: boolean;
	is_preferred: boolean;
	description: string | null;
}

export interface CatalogItemCreate {
	sku: string | null;
	name: string;
	description: string | null;
	/** Request side — the exact decimal text typed, never a JSON number. */
	unit_price: MoneyString | null;
	currency: string;
	uom: string | null;
	vendor_id: string | null;
	gl_account_id: string | null;
	category: string | null;
	is_active: boolean;
}

// ===================== Guided buying =====================

export interface GuidedBuyingVendor {
	vendor_id: string;
	vendor_name: string;
	reasons: string[]; // 'preferred_catalog' | 'active_contract'
	contract_id: string | null;
	contract_number: string | null;
	catalog_id: string | null;
	catalog_name: string | null;
}

export interface GuidedBuyingItem {
	catalog_item_id: string;
	catalog_id: string;
	catalog_name: string;
	sku: string | null;
	name: string;
	unit_price: MoneyAmount;
	currency: string;
	uom: string | null;
	vendor_id: string | null;
	category: string | null;
	is_preferred: boolean;
}

export interface GuidedBuyingSuggestion {
	preferred_vendors: GuidedBuyingVendor[];
	in_contract_vendors: GuidedBuyingVendor[];
	items: GuidedBuyingItem[];
}

// Why guided buying is steering the buyer at this vendor. The backend's
// vocabulary is fixed but open-ended (`api/catalogs.py` builds the list), so
// the accessor is tolerant: an unrecognised reason renders its raw code rather
// than disappearing from the card — a reason nobody can read is still better
// evidence than no reason at all.
export const GUIDED_BUYING_REASON_LABEL_KEYS: Record<string, MessageKey> = {
	preferred_catalog: 'catalogs.guided.reason.preferredCatalog',
	active_contract: 'catalogs.guided.reason.activeContract'
};

/** The message key for a guided-buying reason, or `null` for an unknown one. */
export function guidedBuyingReasonLabelKey(reason: string): MessageKey | null {
	return GUIDED_BUYING_REASON_LABEL_KEYS[reason] ?? null;
}

// ===================== Punch-out (live cXML/OCI round-trip) =====================

export type PunchoutSessionStatus =
	| 'pending'
	| 'returned'
	| 'converted'
	| 'expired'
	| 'cancelled';

/**
 * The i18n key carrying each session-status label — never the English string
 * itself. `PunchoutModal` renders it beside its own translated Status label,
 * so a hardcoded English map here was an untranslated badge one word away
 * from a translated one.
 *
 * Keyed on the union (not `string`, as the label map used to be) so a new
 * session status is a compile error rather than a badge that falls through to
 * its raw enum value; `catalog.test.ts` proves every key exists in the
 * catalogue. `PunchoutSession.status` is still a bare string on the wire —
 * `punchoutStatusLabelKey` is the tolerant accessor for it.
 */
export const PUNCHOUT_STATUS_LABEL_KEYS: Record<PunchoutSessionStatus, MessageKey> = {
	pending: 'catalogs.punchout.status.pending',
	returned: 'catalogs.punchout.status.returned',
	converted: 'catalogs.punchout.status.converted',
	expired: 'catalogs.punchout.status.expired',
	cancelled: 'catalogs.punchout.status.cancelled'
};

/** The message key for a session status, or `null` for an unrecognised one. */
export function punchoutStatusLabelKey(status: string): MessageKey | null {
	return PUNCHOUT_STATUS_LABEL_KEYS[status as PunchoutSessionStatus] ?? null;
}

// Badge tone per session status. Only `returned` and `converted` ever carried a
// colour of their own; everything else shared one grey tint, and still does.
export const PUNCHOUT_STATUS_TONES: Record<string, BadgeTone> = {
	pending: 'muted',
	returned: 'success',
	converted: 'accent',
	expired: 'muted',
	cancelled: 'muted'
};

/**
 * `PunchoutSession.status` is a string off the wire (the adapter may report a
 * provider status this union doesn't name), so the tone is read through a
 * tolerant accessor — mirroring how the label is read. An unrecognised status
 * gets the same grey the pill's base rule always gave it.
 */
export function punchoutStatusTone(status: string): BadgeTone {
	return PUNCHOUT_STATUS_TONES[status] ?? 'muted';
}

export interface PunchoutStartResponse {
	session_id: string;
	buyer_cookie: string;
	start_url: string;
	status: string;
	provider: string;
}

export interface PunchoutCartItem {
	description: string;
	sku: string | null;
	quantity: number | null;
	unit_price: MoneyAmount;
	uom: string | null;
	currency: string;
}

export interface PunchoutSession {
	id: string;
	catalog_id: string;
	buyer_cookie: string;
	status: string;
	requested_by_user_id: string;
	start_url: string | null;
	provider: string | null;
	cart_items: PunchoutCartItem[];
	cart_total: MoneyAmount;
	currency: string;
	returned_at: string | null;
	converted_requisition_id: string | null;
	created_at: string;
	updated_at: string;
}

export interface PunchoutConvertResponse {
	session_id: string;
	requisition_id: string;
	requisition_number: string;
	/** The created requisition's money total (`float` on the wire). */
	total: MoneyAmount;
	created: boolean;
}
