// Types for the Positive Pay / Payment Fraud File surface. Mirrors the JSON
// returned by the `/api/positive-pay` endpoints (the Pydantic contract in
// `backend/app/schemas/positive_pay.py`). Date/time fields are ISO strings (or
// null); money fields are `MoneyAmount` / `MoneyString`, never `number`.
//
// PII discipline mirrors the backend: no full account / routing number ever
// crosses this boundary. `account_last4` is the only account detail on a
// response; a `PresentedItem` carries only a check number + amount.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount, MoneyString } from '$lib/utils/money';

// --- File type ------------------------------------------------------------

export type PositivePayFileType = 'check_issue' | 'ach_authorization';

export const POSITIVE_PAY_FILE_TYPES: PositivePayFileType[] = [
	'check_issue',
	'ach_authorization'
];

// The file type is a table cell, a modal title AND the `{type}` interpolated
// into the translated `positivePay.fileLabel` row label — so an English
// literal here shows up mid-sentence in a German list, the same defect the
// intake type labels had.
export const POSITIVE_PAY_FILE_TYPE_LABEL_KEYS: Record<PositivePayFileType, MessageKey> = {
	check_issue: 'positivePay.fileType.checkIssue',
	ach_authorization: 'positivePay.fileType.achAuthorization'
};

/** The message key for a file type, or `null` for an unrecognised one. */
export function positivePayFileTypeLabelKey(fileType: string): MessageKey | null {
	return POSITIVE_PAY_FILE_TYPE_LABEL_KEYS[fileType as PositivePayFileType] ?? null;
}

// --- File status ----------------------------------------------------------

export type PositivePayStatus = 'generated' | 'returned_processed';

export const POSITIVE_PAY_STATUSES: PositivePayStatus[] = ['generated', 'returned_processed'];

/**
 * The i18n key carrying each status label — never the English string itself.
 * Both surfaces that render it (the `/positive-pay` list and
 * `PositivePayModal`'s detail header) sit beside translated type chips, so a
 * hardcoded English map here read as a gap in the row.
 * `Record<PositivePayStatus, MessageKey>` makes a new status a compile error
 * rather than a blank badge; `positivePay.test.ts` proves the keys exist.
 */
export const POSITIVE_PAY_STATUS_LABEL_KEYS: Record<PositivePayStatus, MessageKey> = {
	generated: 'positivePay.status.generated',
	returned_processed: 'positivePay.status.returnedProcessed'
};

/** The message key for a status, or `null` for one this frontend doesn't know. */
export function positivePayStatusLabelKey(status: string): MessageKey | null {
	return POSITIVE_PAY_STATUS_LABEL_KEYS[status as PositivePayStatus] ?? null;
}

// Badge tone per status, so the list page and the modal can't tint the same
// status two different shades — which is exactly what they did (the list's
// `.badge.generated` and the modal's differed by an alpha step).
export const POSITIVE_PAY_STATUS_TONES: Record<PositivePayStatus, BadgeTone> = {
	generated: 'success',
	// A processed return is the bank reporting an item that didn't match — an
	// outcome to read, not a failure, so amber rather than red.
	returned_processed: 'warning'
};

// Bank formatter keys the backend ships (`get_positive_pay_formatter` default
// "csv"; unknown falls back to csv). Surfaced as the format picker options.
export const BANK_FORMATS = ['csv', 'fixed_width'] as const;
export type BankFormat = (typeof BANK_FORMATS)[number];

/**
 * The i18n key carrying each formatter's label — never the English string.
 * Three surfaces read it: the generate dialog's picker, the `/positive-pay`
 * list's Format column and the detail header's meta pill. The latter two used
 * to print the RAW key (`fixed_width`), so the same file read "Fixed width" in
 * the dialog and `fixed_width` in the row one click away.
 *
 * `csv` keeps the value "CSV" in every locale — a file-format name, the same
 * data-value convention that keeps `ACH` / `BACS` verbatim.
 */
export const BANK_FORMAT_LABEL_KEYS: Record<BankFormat, MessageKey> = {
	csv: 'positivePay.bankFormat.csv',
	fixed_width: 'positivePay.bankFormat.fixedWidth'
};

/**
 * The message key for a formatter, or `null` for one this frontend doesn't
 * know (the caller renders the raw value — visible and searchable — rather
 * than a blank cell). `PositivePayFile.bank_format` is a bare `string` on the
 * wire: the backend's formatter registry is pluggable, so a per-bank adapter
 * can ship before this map catches up.
 */
export function bankFormatLabelKey(bankFormat: string): MessageKey | null {
	return BANK_FORMAT_LABEL_KEYS[bankFormat as BankFormat] ?? null;
}

// --- Response shapes ------------------------------------------------------
//
// **Money is `MoneyAmount`, never `number`.** The backend serialises
// `total_amount` as a JSON number, so `MoneyString` would be a different lie —
// but a `number` field invites `a - b` / `Math.max()` on currency. The counts
// beside it (`item_count`, `total`, `items_exported`, `returns_flagged`, and
// every field on `ReturnSummary`) stay `number`: they count cheques and rows,
// not money. See `frontend/CLAUDE.md` § Money formatting.

// The return-processing summary the backend stores under `meta.return_summary`.
export interface ReturnSummary {
	presented_count: number;
	matched_ok: number;
	amount_mismatches: number;
	not_on_file: number;
	exceptions_created: number;
}

export interface PositivePayFile {
	id: string;
	file_type: PositivePayFileType;
	bank_format: string;
	status: PositivePayStatus;
	payment_run_id: string | null;
	item_count: number;
	total_amount: MoneyAmount;
	/**
	 * Currency `total_amount` is denominated in — the one currency the file's
	 * cheques agree on (each `Payment.amount` is in its invoice's currency),
	 * stamped at generation. `null` when they carry none or disagree, and for
	 * legacy rows created before the column existed: render the total BARE
	 * then, never under the org default (`docs/decisions.md` §196).
	 */
	currency: string | null;
	account_last4: string | null;
	file_key: string | null;
	created_at: string;
	updated_at: string | null;
	// Free-form metadata: holds `issued_map`, `return_summary`, `return_history`.
	// Typed loosely (only the fields the UI reads matter).
	meta?: {
		return_summary?: ReturnSummary;
		return_history?: ReturnSummary[];
		[k: string]: unknown;
	} | null;
}

export interface PositivePayListResponse {
	items: PositivePayFile[];
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	page: number;
	page_size: number;
}

/**
 * Whole-set KPI rollup from `GET /api/positive-pay/summary`, over the SAME
 * file_type / status filters the list ran with.
 *
 * `itemsExported` and `returnsFlagged` used to reduce over the LOADED page
 * while "Files" showed the server's whole-set `total`. `items_exported` /
 * `returns_flagged` are those reduces, whole-set.
 */
export interface PositivePaySummary {
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	by_status: Record<string, number>;
	items_exported: number;
	returns_flagged: number;
}

// --- Process-return payload + response ------------------------------------

// The bank's "what I saw presented" line. `amount` is the exact decimal STRING
// read off the paste — never `parseFloat`. This figure is compared against the
// cheque we issued to decide whether it was ALTERED, so an amount we could not
// read must be refused at the paste, never degraded to "no amount" (which the
// classifier reads as a clean match).
export interface PresentedItemInput {
	check_number?: string | null;
	amount?: MoneyString | null;
}

export interface ProcessReturnResponse {
	presented_count: number;
	matched_ok: number;
	amount_mismatches: number;
	not_on_file: number;
	exceptions_created: number;
	file: PositivePayFile;
}
