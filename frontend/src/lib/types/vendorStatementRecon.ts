import type { MoneyAmount, MoneyString } from '$lib/utils/money';

// Types for the Vendor Statement Reconciliation surface. Mirrors the JSON
// returned by the `/api/vendor-statements` endpoints (the Pydantic contract in
// `backend/app/schemas/vendor_statement_recon.py`). Money fields arrive as
// numbers (or null); date/time fields are ISO strings (or null).

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';

// --- Run status -----------------------------------------------------------

export type ReconStatus = 'open' | 'resolved';

export const RECON_STATUSES: ReconStatus[] = ['open', 'resolved'];

/**
 * The i18n key carrying each run-status label — never the English string
 * itself. Both surfaces that render it (the `/vendor-statements` list and
 * `VendorStatementReconModal`) are inside the i18n extraction slice, so a
 * hardcoded English map here put translated chips beside an untranslated
 * `Open` badge. `Record<ReconStatus, MessageKey>` makes a new status a
 * compile error rather than a blank badge, and `vendorStatementRecon.test.ts`
 * proves every key exists in the catalogue.
 */
export const RECON_STATUS_LABEL_KEYS: Record<ReconStatus, MessageKey> = {
	open: 'vendorStatements.status.open',
	resolved: 'vendorStatements.status.resolved'
};

/** The message key for a run status, or `null` for an unrecognised one. */
export function reconStatusLabelKey(status: string): MessageKey | null {
	return RECON_STATUS_LABEL_KEYS[status as ReconStatus] ?? null;
}

// Badge tone per status, so the list page and the modal can't tint the same
// status two different shades — which is exactly what they did (.12 alpha on
// the list, .15 in the modal, off two different ambers).
export const RECON_STATUS_TONES: Record<ReconStatus, BadgeTone> = {
	// An open run is work still owed to the ledger, not an error — amber.
	open: 'warning',
	resolved: 'success'
};

// --- Intake source --------------------------------------------------------

// How the run's statement lines got here. `manual` = typed into the lines
// editor; `csv` = a deterministic parse of an uploaded CSV; `pdf` = MACHINE-READ
// off an uploaded document by the org's extraction adapter. The distinction is
// not cosmetic: a reviewer clearing a `pdf` run's lines is clearing a model's
// reading of a document, which is why the provenance block exists.
export type ReconSourceFormat = 'manual' | 'csv' | 'pdf';

// The provenance pill sits directly under the translated provenance heading
// in the modal, so this is keyed like the status map above.
export const RECON_SOURCE_FORMAT_LABEL_KEYS: Record<ReconSourceFormat, MessageKey> = {
	manual: 'vendorStatements.sourceFormat.manual',
	csv: 'vendorStatements.sourceFormat.csv',
	pdf: 'vendorStatements.sourceFormat.pdf'
};

/** The message key for a source format, or `null` for an unrecognised one. */
export function reconSourceFormatLabelKey(format: string): MessageKey | null {
	return RECON_SOURCE_FORMAT_LABEL_KEYS[format as ReconSourceFormat] ?? null;
}

// --- Line classification --------------------------------------------------

export type ReconClassification =
	| 'matched'
	| 'amount_mismatch'
	| 'missing_on_our_side'
	| 'missing_on_their_side';

// Rendered as the Classification cell of the modal's diff table, whose every
// header and action is translated — so these are keys, not literals.
export const RECON_CLASSIFICATION_LABEL_KEYS: Record<ReconClassification, MessageKey> = {
	matched: 'vendorStatements.classification.matched',
	amount_mismatch: 'vendorStatements.classification.amountMismatch',
	missing_on_our_side: 'vendorStatements.classification.missingOurSide',
	missing_on_their_side: 'vendorStatements.classification.missingTheirSide'
};

// --- Line resolution ------------------------------------------------------

export type ReconResolutionStatus = 'unresolved' | 'resolved' | 'ignored';

/** Message keys for the per-line Resolution cell — see the map above. */
export const RECON_RESOLUTION_LABEL_KEYS: Record<ReconResolutionStatus, MessageKey> = {
	unresolved: 'vendorStatements.resolution.unresolved',
	resolved: 'vendorStatements.resolution.resolved',
	ignored: 'vendorStatements.resolution.ignored'
};

// --- Response shapes ------------------------------------------------------

export interface ReconLine {
	id: string;
	classification: ReconClassification;
	resolution_status: ReconResolutionStatus;
	statement_invoice_number: string | null;
	statement_date: string | null;
	statement_amount: MoneyAmount;
	statement_status: string | null;
	matched_invoice_id: string | null;
	matched_invoice_number: string | null;
	ledger_amount: MoneyAmount;
	/**
	 * The server's own statement-vs-ledger delta. Rendered, never recomputed:
	 * subtracting the two amounts client-side is the float money math the
	 * Decimal invariant exists to prevent, and this figure is what a clerk
	 * chases.
	 */
	amount_difference: MoneyAmount;
	match_method: string | null;
	resolution_note: string | null;
	resolved_at: string | null;
}

export interface ReconSummary {
	line_count: number;
	matched_count: number;
	amount_mismatch_count: number;
	missing_our_side_count: number;
	missing_their_side_count: number;
	statement_total: MoneyAmount;
	ledger_total: MoneyAmount;
}

// Provenance for a run whose lines were MACHINE-READ off a PDF. Present only on
// the PDF intake path; `null` for CSV / pasted-lines runs, which have neither a
// provider nor a confidence to report.
export interface StatementExtractionMeta {
	method: string;
	provider: string;
	// 0..1 as the adapter reported it — render through `formatExtractionConfidence`.
	confidence: number;
	// How many open items the reader actually accepted off the document. NOT the
	// run's line count: the run also carries `missing_on_their_side` rows built
	// from our own ledger.
	line_count: number;
	// How many rows the reader recognised as an open item but REFUSED to book
	// because it could not resolve them (a second money column, a second
	// reference column). Deliberately not a count of every skipped line —
	// headers, totals and page furniture are skipped silently. Optional because
	// runs created before the reader counted them carry no such key.
	skipped_ambiguous?: number;
}

export interface Reconciliation {
	id: string;
	vendor_id: string | null;
	vendor_name: string | null;
	statement_date: string;
	statement_reference: string | null;
	currency: string;
	source_format: string;
	file_key: string | null;
	// Whether the supplier's own document was archived alongside the run —
	// fetched by run id via `GET /{id}/file`, never by key.
	has_source_file: boolean;
	extraction: StatementExtractionMeta | null;
	status: ReconStatus;
	notes: string | null;
	summary: ReconSummary;
	created_at: string;
	updated_at: string | null;
	// Lines are included on the detail response only (the list omits them).
	lines?: ReconLine[] | null;
}

export interface ReconciliationListResponse {
	items: Reconciliation[];
	total: number;
	page: number;
	page_size: number;
}

/**
 * Whole-set KPI rollup from `GET /api/vendor-statements/summary`, over the SAME
 * vendor_id / status filters the list ran with.
 *
 * The page's `openCount` filtered the LOADED page and `totalDiscrepancies`
 * reduced the per-run discrepancy counts over it — both contradicting the
 * whole-set `total`. `open_discrepancies` is that reduce, whole-set.
 */
export interface ReconciliationSummary {
	total: number;
	by_status: Record<string, number>;
	open_discrepancies: number;
}

// --- Create payloads ------------------------------------------------------

// One supplier-statement line for the manual / pasted-lines intake path.
export interface StatementLineInput {
	invoice_number?: string | null;
	invoice_date?: string | null;
	/** Request side — the exact decimal text typed, never a JSON number. */
	amount?: MoneyString | null;
	status?: string | null;
}

// POST /api/vendor-statements body (JSON, pasted-lines path).
export interface ReconciliationCreate {
	vendor_id: string;
	statement_date: string;
	statement_reference?: string | null;
	currency?: string;
	notes?: string | null;
	lines: StatementLineInput[];
}

// --- Line resolve ---------------------------------------------------------

export interface LineResolveRequest {
	resolution_status: ReconResolutionStatus;
	resolution_note?: string | null;
}

// --- Close-readiness ------------------------------------------------------

export interface CloseReadinessVendor {
	vendor_id: string | null;
	vendor_name: string | null;
	reconciliation_id: string;
	statement_date: string;
	currency: string;
	unreconciled_amount: MoneyAmount;
	missing_our_side_count: number;
	amount_mismatch_count: number;
}

export interface CloseReadinessResponse {
	materiality_threshold: MoneyAmount;
	blocking_vendors: CloseReadinessVendor[];
	is_close_ready: boolean;
}

// --- Pure display helpers -------------------------------------------------

/**
 * Render an adapter's 0..1 confidence as a whole percentage.
 *
 * Defensive because the figure crosses a network boundary: a missing / NaN /
 * non-numeric value renders the placeholder rather than `NaN%`, and an
 * out-of-range value is clamped rather than shown as `140%` — a reviewer
 * weighing a machine's reading must never be handed a nonsense number.
 * Deliberately NOT locale-formatted: it is a provenance qualifier, not a
 * measured figure, and it must not be confused with money.
 */
export function formatExtractionConfidence(
	confidence: number | null | undefined,
	placeholder = '—'
): string {
	if (typeof confidence !== 'number' || !Number.isFinite(confidence)) return placeholder;
	const clamped = Math.min(1, Math.max(0, confidence));
	return `${Math.round(clamped * 100)}%`;
}

/** Were this run's lines read off a document by an extraction adapter? */
export function isMachineRead(recon: Pick<Reconciliation, 'extraction'>): boolean {
	return recon.extraction !== null && recon.extraction !== undefined;
}

/**
 * How many supplier rows the reader saw but refused to book, or `0`.
 *
 * Crosses a network boundary and is absent on runs created before the reader
 * counted its skips, so anything that isn't a finite non-negative integer reads
 * as zero — the panel must never announce `NaN rows were skipped`, and must not
 * announce a skip warning on a run that simply predates the field. Rounded
 * rather than trusted: a count is not a measured figure.
 */
export function ambiguousSkipCount(
	extraction: StatementExtractionMeta | null | undefined
): number {
	const raw = extraction?.skipped_ambiguous;
	if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return 0;
	return Math.round(raw);
}

/**
 * Filename to save the archived supplier document under.
 *
 * Composed from the run's own metadata rather than parsed out of `file_key` —
 * the key is an internal storage detail (the API returns the `has_source_file`
 * flag precisely so a client needn't reach into it), and `source_format` already
 * says which document shape was uploaded.
 */
export function sourceStatementFilename(
	recon: Pick<Reconciliation, 'source_format' | 'statement_date' | 'vendor_name'>
): string {
	const ext = recon.source_format === 'pdf' ? 'pdf' : 'csv';
	const vendor = (recon.vendor_name ?? 'vendor')
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '');
	return `statement-${vendor || 'vendor'}-${recon.statement_date}.${ext}`;
}
