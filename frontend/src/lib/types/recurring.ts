// Types for the Recurring / Subscription Invoices surface. Mirrors the JSON
// returned by the `/api/recurring` endpoints. Date fields are ISO date strings
// (or null).
//
// **Money is `MoneyAmount` on the way in, `MoneyString` on the way out**, never
// `number` — a `number`-typed money field invites `a - b`, `Math.max()` and the
// `amount / 3` / `amount / 12` cadence divide this page used to do client-side
// (the server owns that normalisation now, in exact Decimal). `total` on the
// list/summary/history envelopes is a ROW COUNT and stays `number`, as does
// `day_of_period`, `generated_count` and `variance_tolerance_pct` (a percent).
// See `frontend/CLAUDE.md` § Money formatting.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount, MoneyString } from '$lib/utils/money';

export type RecurringCadence = 'monthly' | 'quarterly' | 'annual';

export const RECURRING_CADENCES: RecurringCadence[] = ['monthly', 'quarterly', 'annual'];

// The cadence is a table cell and a `<select>` option sitting beside the
// translated status badge, so an English literal here reads as a gap.
export const CADENCE_LABEL_KEYS: Record<RecurringCadence, MessageKey> = {
	monthly: 'recurring.cadence.monthly',
	quarterly: 'recurring.cadence.quarterly',
	annual: 'recurring.cadence.annual'
};

/** The message key for a cadence, or `null` for an unrecognised one. */
export function cadenceLabelKey(cadence: string): MessageKey | null {
	return CADENCE_LABEL_KEYS[cadence as RecurringCadence] ?? null;
}

export type RecurringStatus = 'active' | 'paused' | 'ended';

export const RECURRING_STATUSES: RecurringStatus[] = ['active', 'paused', 'ended'];

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Both surfaces that render a template status (the `/recurring` list and
 * `RecurringModal`) are inside the i18n extraction slice, so a hardcoded
 * English map here put a translated Pause action beside an untranslated
 * `Active` badge — and the amber "Not generating" badge next to it was
 * already translated. `Record<RecurringStatus, MessageKey>` makes a new
 * status a compile error rather than a blank badge, and `recurring.test.ts`
 * proves every key exists in the catalogue.
 */
export const STATUS_LABEL_KEYS: Record<RecurringStatus, MessageKey> = {
	active: 'recurring.status.active',
	paused: 'recurring.status.paused',
	ended: 'recurring.status.ended'
};

/** The message key for a status, or `null` for an unrecognised one. */
export function recurringStatusLabelKey(status: string): MessageKey | null {
	return STATUS_LABEL_KEYS[status as RecurringStatus] ?? null;
}

// Badge tone per status, so the list page and the modal can't tint the same
// status two different shades — which is exactly what they did (.12 alpha on
// the list, .15 in the modal).
export const STATUS_TONES: Record<RecurringStatus, BadgeTone> = {
	active: 'success',
	// Paused is a schedule someone stopped on purpose — amber, because nothing
	// will be raised until they resume it.
	paused: 'warning',
	// Ended is the absence of a signal, not a weak one — flat, not tinted.
	ended: 'neutral'
};

// A due period the background sweep could NOT generate an invoice for. The
// backend persists it on the template (`meta.generation_skip`) and surfaces it
// here so "nothing raised for months" is distinguishable from "nothing due
// yet". PII-free: a reason code, the period, a count and a timestamp.
// See `backend/docs/recurring-invoices.md` § A skipped period is never silent.
export type GenerationSkipReason =
	| 'missing_amount'
	| 'missing_vendor'
	| 'missing_amount_and_vendor';

export interface GenerationSkip {
	reason: GenerationSkipReason | string;
	period_key: string | null;
	consecutive: number;
	last_skipped_at: string | null;
}

// Every reason code the backend sweep can emit
// (`services/recurring_invoices.SKIP_MISSING_*`). Kept here, beside the
// message-key map, so a code added on the backend fails the unit test rather
// than silently rendering as a raw string in the UI.
export const GENERATION_SKIP_REASONS: GenerationSkipReason[] = [
	'missing_amount',
	'missing_vendor',
	'missing_amount_and_vendor'
];

const SKIP_REASON_KEYS: Record<string, string> = {
	missing_amount: 'recurring.skip.reason.missingAmount',
	missing_vendor: 'recurring.skip.reason.missingVendor',
	missing_amount_and_vendor: 'recurring.skip.reason.missingBoth'
};

/**
 * The i18n key describing a skip reason code, or `null` for a code this
 * frontend doesn't know (the caller renders the raw code rather than a blank).
 * Pure — the typed `MessageKey` cast happens at the call site so this module
 * stays free of the i18n runtime.
 */
export function skipReasonKey(code: string): string | null {
	return SKIP_REASON_KEYS[code] ?? null;
}

export interface RecurringTemplate {
	id: string;
	name: string;
	vendor_id: string | null;
	vendor_name: string | null;
	description: string | null;
	amount: MoneyAmount;
	currency: string;
	gl_account: string | null;
	cost_center: string | null;
	department: string | null;
	project: string | null;
	po_number: string | null;
	payment_terms: string | null;
	cadence: RecurringCadence;
	day_of_period: number;
	start_date: string;
	end_date: string | null;
	next_run_on: string | null;
	last_period_key: string | null;
	last_generated_at: string | null;
	generated_count: number;
	status: RecurringStatus;
	variance_tolerance_pct: number | null;
	notes: string | null;
	last_skip: GenerationSkip | null;
	created_at: string;
	updated_at: string | null;
}

// POST /api/recurring body. name + start_date are required.
export interface RecurringTemplateCreate {
	name: string;
	vendor_id?: string | null;
	description?: string | null;
	amount?: MoneyString | null;
	currency?: string;
	gl_account?: string | null;
	cost_center?: string | null;
	department?: string | null;
	project?: string | null;
	po_number?: string | null;
	payment_terms?: string | null;
	cadence?: RecurringCadence;
	day_of_period?: number;
	start_date: string;
	end_date?: string | null;
	variance_tolerance_pct?: number | null;
	notes?: string | null;
}

export interface RecurringListResponse {
	items: RecurringTemplate[];
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	page: number;
	page_size: number;
}

/** One currency's monthly-equivalent recurring spend — `GET /api/recurring/summary`. */
export interface RecurringCurrencyTotal {
	currency: string;
	/** Exact decimal string. Cadence-normalised (÷1, ÷3, ÷12) server-side. */
	total: MoneyString;
	count: number;
}

/**
 * Whole-set KPI rollup from `GET /api/recurring/summary`, over the SAME
 * status/vendor/search filters the list ran with.
 *
 * The page's `activeCount`, `soonestNextRun` and `monthlyRecurringTotal` were
 * all derived from the LOADED page, and the monthly total divided floats
 * (`amount / 3`, `amount / 12`). `monthly_equivalent` is grouped by currency,
 * never summed across them (see `$lib/utils/currencyGroups`).
 */
export interface RecurringTemplateSummary {
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	by_status: Record<string, number>;
	monthly_equivalent: RecurringCurrencyTotal[];
	/** Earliest `next_run_on` across active templates in the set (ISO date), or null. */
	soonest_next_run: string | null;
}

// GET /api/recurring/{id}/upcoming-schedule
export interface RecurringOccurrence {
	period_key: string;
	run_on: string;
	amount: MoneyAmount;
	currency: string;
}

export interface UpcomingSchedule {
	template_id: string;
	occurrences: RecurringOccurrence[];
}

// GET /api/recurring/{id}/history
export interface RecurringHistoryItem {
	invoice_id: string;
	invoice_number: string | null;
	period_key: string;
	amount: MoneyAmount;
	currency: string;
	status: string;
	created_at: string;
}

export interface RecurringHistory {
	template_id: string;
	items: RecurringHistoryItem[];
	/** Row count of the generated-invoice history — NOT money. */
	total: number;
}
