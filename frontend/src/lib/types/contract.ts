import type { MoneyAmount, MoneyString } from '$lib/utils/money';

// Types for the Contracts surface. Mirrors the JSON returned by the
// `/api/contracts` endpoints (backend `Contract.to_dict()`). Money fields
// arrive as numbers (or null); date fields are ISO date strings (or null).
import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';

export type ContractType =
	| 'purchase'
	| 'service'
	| 'subscription'
	| 'lease'
	| 'sla'
	| 'msa'
	| 'sow'
	| 'other';

export const CONTRACT_TYPES: ContractType[] = [
	'purchase',
	'service',
	'subscription',
	'lease',
	'sla',
	'msa',
	'sow',
	'other'
];

// The contract type is a table cell AND a `<select>` option beside the
// translated status badge, so an English literal here reads as a gap in the
// row rather than a deliberate data value. Keyed like the status map below.
export const CONTRACT_TYPE_LABEL_KEYS: Record<ContractType, MessageKey> = {
	purchase: 'contracts.type.purchase',
	service: 'contracts.type.service',
	subscription: 'contracts.type.subscription',
	lease: 'contracts.type.lease',
	sla: 'contracts.type.sla',
	msa: 'contracts.type.msa',
	sow: 'contracts.type.sow',
	other: 'contracts.type.other'
};

/** The message key for a contract type, or `null` for an unrecognised one. */
export function contractTypeLabelKey(contractType: string): MessageKey | null {
	return CONTRACT_TYPE_LABEL_KEYS[contractType as ContractType] ?? null;
}

export type ContractStatus =
	| 'draft'
	| 'active'
	| 'expired'
	| 'terminated'
	| 'cancelled';

export const CONTRACT_STATUSES: ContractStatus[] = [
	'draft',
	'active',
	'expired',
	'terminated',
	'cancelled'
];

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Both surfaces that render a contract status (the `/contracts` list page and
 * `ContractModal`) are inside the i18n extraction slice, so a hardcoded
 * English map here put a translated lifecycle button beside an untranslated
 * `Active` badge. `Record<ContractStatus, MessageKey>` makes a new status a
 * compile error rather than a blank badge, and `contract.test.ts` proves every
 * key exists in the catalogue.
 */
export const STATUS_LABEL_KEYS: Record<ContractStatus, MessageKey> = {
	draft: 'contracts.status.draft',
	active: 'contracts.status.active',
	expired: 'contracts.status.expired',
	terminated: 'contracts.status.terminated',
	cancelled: 'contracts.status.cancelled'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). `Contract.status` is typed `string` because the API is the
 * source of truth.
 */
export function contractStatusLabelKey(status: string): MessageKey | null {
	return STATUS_LABEL_KEYS[status as ContractStatus] ?? null;
}

// Badge tone per status, so the list page and the modal can't tint the same
// status two different shades — which is exactly what they did (the modal's
// `.badge.active` and the list's differed by an alpha step).
export const STATUS_TONES: Record<ContractStatus, BadgeTone> = {
	draft: 'accent',
	active: 'success',
	expired: 'warning',
	terminated: 'danger',
	// Cancelled is the absence of a signal, not a weak one — flat, not tinted.
	cancelled: 'neutral'
};

export interface ContractLineItem {
	id: string;
	line_number: number | null;
	item_code: string | null;
	description: string | null;
	quantity: number | null;
	// `schemas/contract.py::ContractLineItemResponse` inherits these as bare
	// `Decimal | None`, and pydantic's DEFAULT JSON shape for a Decimal is an
	// exact STRING — unlike the `float`-typed header figures below. So they
	// arrive as `"1200.00"`, which the old `number` type quietly mis-declared.
	unit_price: MoneyString | null;
	total: MoneyString | null;
	gl_account: string | null;
}

export interface ContractSpend {
	invoiced_total: MoneyAmount;
	invoice_count: number;
	spend_limit: MoneyAmount;
	/**
	 * Headroom left under the limit, computed SERVER-side. Never re-derive it
	 * from `spend_limit - invoiced_total`; `over_limit` is the server's own
	 * verdict and is what the UI branches on.
	 */
	remaining: MoneyAmount;
	over_limit: boolean;
}

export interface Contract {
	id: string;
	contract_number: string;
	title: string | null;
	description: string | null;
	contract_type: ContractType;
	status: ContractStatus;
	vendor_id: string;
	vendor_name: string | null;
	currency: string;
	total_value: MoneyAmount;
	spend_limit: MoneyAmount;
	not_to_exceed: boolean;
	start_date: string | null;
	end_date: string | null;
	signed_date: string | null;
	auto_renew: boolean;
	renewal_term_months: number | null;
	renewal_notice_days: number;
	renewal_alert_sent_at: string | null;
	payment_terms: string | null;
	owner_user_id: string | null;
	file_url: string | null;
	file_key: string | null;
	terms: Record<string, unknown> | null;
	line_items: ContractLineItem[];
	spend: ContractSpend | null;
	created_at: string;
	updated_at: string;
}

// Writable line-item shape for ContractCreate / line-item edits. All fields
// optional — the backend fills line_number when omitted.
export interface ContractLineItemInput {
	line_number?: number;
	item_code?: string | null;
	description?: string | null;
	quantity?: number | null;
	/** Request side — the exact decimal text typed, never a JSON number. */
	unit_price?: MoneyString | null;
	total?: MoneyString | null;
	gl_account?: string | null;
}

// POST /api/contracts body. vendor_id + contract_number are required.
export interface ContractCreate {
	contract_number: string;
	vendor_id: string;
	title?: string | null;
	description?: string | null;
	contract_type?: ContractType;
	currency?: string;
	total_value?: MoneyString | null;
	spend_limit?: MoneyString | null;
	not_to_exceed?: boolean;
	start_date?: string | null;
	end_date?: string | null;
	signed_date?: string | null;
	auto_renew?: boolean;
	renewal_term_months?: number | null;
	renewal_notice_days?: number;
	payment_terms?: string | null;
	owner_user_id?: string | null;
	line_items?: ContractLineItemInput[];
}

export interface ContractListResponse {
	items: Contract[];
	total: number;
	page: number;
	page_size: number;
}
