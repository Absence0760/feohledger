// Types for the procurement Intake surface. Mirrors the JSON returned by the
// `/api/intake` endpoints (backend `IntakeRequestResponse`). Date/datetime
// fields are ISO strings.
//
// **Money is `MoneyAmount` on the way in, `MoneyString` on the way out.** The
// backend serialises `estimated_amount` as a JSON number (`float(...)`), so
// `MoneyAmount` is the honest read type — and it makes `a - b` / `Math.max()`
// on the figure a type error rather than a convention. Outbound it is the exact
// decimal string the user typed (`utils/moneyInput.ts`), because a fractional
// JSON number is already a float by the time pydantic sees it. The `total` on
// the list/summary envelopes stays `number`: it is a ROW COUNT, not money.
// See `frontend/CLAUDE.md` § Money formatting.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount, MoneyString } from '$lib/utils/money';

export type IntakeStatus =
	| 'open'
	| 'in_review'
	| 'approved'
	| 'rejected'
	| 'converted'
	| 'cancelled';

export const INTAKE_STATUSES: IntakeStatus[] = [
	'open',
	'in_review',
	'approved',
	'rejected',
	'converted',
	'cancelled'
];

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Both surfaces that render an intake status (the `/intake` list page and
 * `IntakeModal`) are inside the i18n extraction slice, so a hardcoded English
 * map here put a translated Reopen confirm ("Zurück auf „Offen“") directly
 * beside an untranslated `Open` badge. Keyed the same way
 * `notification.ts::EVENT_LABEL_KEYS` is; `Record<IntakeStatus, MessageKey>`
 * makes a new status a compile error rather than a blank badge, and
 * `intake.test.ts` proves every key exists in the catalogue.
 */
export const INTAKE_STATUS_LABEL_KEYS: Record<IntakeStatus, MessageKey> = {
	open: 'intake.status.open',
	in_review: 'intake.status.inReview',
	approved: 'intake.status.approved',
	rejected: 'intake.status.rejected',
	converted: 'intake.status.converted',
	cancelled: 'intake.status.cancelled'
};

// Badge tone per status, so the list page and the modal can't tint the same
// status two different shades — which is exactly what they did (.12 alpha on
// the list, .15 in the modal, and `open` already token-based in only one).
export const INTAKE_STATUS_TONES: Record<IntakeStatus, BadgeTone> = {
	open: 'accent',
	in_review: 'warning',
	approved: 'success',
	rejected: 'danger',
	// Converted means the ask left intake and became a requisition — a
	// mid-pipeline handoff, which is exactly what the `erp` purple marks.
	converted: 'erp',
	// Cancelled is the absence of a signal, not a weak one — flat, not tinted.
	cancelled: 'neutral'
};

/**
 * `IntakeRequest.status` is typed `string` (the API is the source of truth, and
 * the backend may add a status before this union does), so both call sites read
 * the tone through a tolerant accessor — mirroring how they read the label.
 * An unrecognised status renders flat rather than borrowing another tone's
 * meaning.
 */
export function intakeStatusTone(status: string): BadgeTone {
	return INTAKE_STATUS_TONES[status as IntakeStatus] ?? 'neutral';
}

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). Mirrors `intakeStatusTone`'s tolerance: `IntakeRequest.status`
 * is typed `string` because the API is the source of truth.
 */
export function intakeStatusLabelKey(status: string): MessageKey | null {
	return INTAKE_STATUS_LABEL_KEYS[status as IntakeStatus] ?? null;
}

export type IntakeType = 'software' | 'services' | 'hardware' | 'other';

export const INTAKE_TYPES: IntakeType[] = ['software', 'services', 'hardware', 'other'];

// Same treatment as the status labels above: the type name is a filter-chip
// label, a table cell AND the `{type}` interpolated into the translated
// questionnaire heading, so an English literal here shows up mid-sentence in a
// German modal.
export const INTAKE_TYPE_LABEL_KEYS: Record<IntakeType, MessageKey> = {
	software: 'intake.type.software',
	services: 'intake.type.services',
	hardware: 'intake.type.hardware',
	other: 'intake.type.other'
};

/** The message key for a request type, or `null` for an unrecognised one. */
export function intakeTypeLabelKey(requestType: string): MessageKey | null {
	return INTAKE_TYPE_LABEL_KEYS[requestType as IntakeType] ?? null;
}

// Flexible questionnaire payload — free-form key/value answers, shape varies by
// request type. Persisted verbatim as JSONB on the backend.
export type IntakeFormData = Record<string, unknown>;

export interface IntakeRequest {
	id: string;
	request_number: string;
	title: string;
	request_type: string;
	requester_user_id: string;
	description: string | null;
	estimated_amount: MoneyAmount;
	currency: string;
	vendor_name: string | null;
	vendor_id: string | null;
	status: string;
	form_data: IntakeFormData | null;
	needed_by: string | null;
	justification: string | null;
	converted_requisition_id: string | null;
	converted_po_id: string | null;
	created_at: string;
	updated_at: string;
}

export interface IntakeListResponse {
	items: IntakeRequest[];
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	page: number;
	page_size: number;
}

/**
 * Whole-set KPI rollup from `GET /api/intake/summary`, over the SAME
 * status/type/search filters the list ran with.
 *
 * `openCount` / `reviewCount` used to filter the LOADED page by status, so both
 * contradicted the whole-set `total` count beside them.
 */
export interface IntakeSummary {
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	by_status: Record<string, number>;
}

// Request side. Money goes out as the EXACT DECIMAL STRING the user typed —
// never a JSON number, which `json.loads` would have already rounded to a
// float before any pydantic `Decimal` annotation could see it.
export interface IntakeCreate {
	request_number?: string | null;
	title: string;
	request_type: string;
	description: string | null;
	estimated_amount: MoneyString | null;
	currency: string;
	vendor_name: string | null;
	form_data: IntakeFormData | null;
	needed_by: string | null;
	justification: string | null;
}

export interface IntakeUpdate {
	title?: string;
	request_type?: string;
	description?: string | null;
	estimated_amount?: MoneyString | null;
	currency?: string;
	vendor_name?: string | null;
	form_data?: IntakeFormData | null;
	needed_by?: string | null;
	justification?: string | null;
}

export interface IntakeConvertResponse {
	intake: IntakeRequest;
	requisition_id: string;
	requisition_number: string;
	created: boolean;
}

// Per-request-type questionnaire fields shown in the create modal. The labels
// drive a small dynamic form; answers are collected into `form_data`.
export const INTAKE_FORM_FIELDS: Record<IntakeType, { key: string; labelKey: MessageKey }[]> = {
	software: [
		{ key: 'seats', labelKey: 'intake.form.software.seats' },
		{ key: 'renewal', labelKey: 'intake.form.software.renewal' },
		{ key: 'data_residency', labelKey: 'intake.form.software.dataResidency' }
	],
	services: [
		{ key: 'scope', labelKey: 'intake.form.services.scope' },
		{ key: 'duration', labelKey: 'intake.form.services.duration' },
		{ key: 'sow_ref', labelKey: 'intake.form.services.sowRef' }
	],
	hardware: [
		{ key: 'quantity', labelKey: 'intake.form.hardware.quantity' },
		{ key: 'model', labelKey: 'intake.form.hardware.model' },
		{ key: 'ship_to', labelKey: 'intake.form.hardware.shipTo' }
	],
	other: [{ key: 'details', labelKey: 'intake.form.other.details' }]
};
