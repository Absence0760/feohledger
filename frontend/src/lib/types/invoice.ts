import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount } from '$lib/utils/money';

export type InvoiceStatus =
	| 'new'
	| 'pending'
	| 'ready_for_review'
	| 'approved'
	| 'rejected'
	| 'sending_to_erp'
	| 'sent_to_erp'
	| 'posted_in_erp'
	| 'payment_scheduled'
	| 'paid'
	| 'done'
	| 'failed';

export const INVOICE_STATUSES: InvoiceStatus[] = [
	'new',
	'pending',
	'ready_for_review',
	'approved',
	'rejected',
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'payment_scheduled',
	'paid',
	'done',
	'failed'
];

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Every surface that renders an invoice status is inside the i18n extraction
 * slice — the `/invoices` filter chips and bulk-status picker, the dashboard
 * pipeline chart, `StatusBadge` (which the assistant's tool tables reuse),
 * `InvoiceModal`'s status field and `AdvancedSearchModal`'s status chips — so
 * a hardcoded English map here put a translated chip row directly beside an
 * untranslated `Ready for Review` badge. This was the LAST of the thirteen
 * label maps round 24 named; it was left behind only because its call sites
 * were owned by other agents that round.
 *
 * Keyed the same way `payment.ts::PAYMENT_STATUS_LABEL_KEYS` is;
 * `Record<InvoiceStatus, MessageKey>` makes a new status a compile error
 * rather than a blank badge, and `invoiceStatus.test.ts` proves every key
 * exists in the catalogue.
 */
export const INVOICE_STATUS_LABEL_KEYS: Record<InvoiceStatus, MessageKey> = {
	new: 'invoices.status.new',
	pending: 'invoices.status.pending',
	ready_for_review: 'invoices.status.readyForReview',
	approved: 'invoices.status.approved',
	rejected: 'invoices.status.rejected',
	sending_to_erp: 'invoices.status.sendingToErp',
	sent_to_erp: 'invoices.status.sentToErp',
	posted_in_erp: 'invoices.status.postedInErp',
	payment_scheduled: 'invoices.status.paymentScheduled',
	paid: 'invoices.status.paid',
	done: 'invoices.status.done',
	failed: 'invoices.status.failed'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). `Invoice.status` is typed against the union, but a status the
 * backend adds first still reaches a badge as a bare string — the assistant's
 * tool results carry exactly that, an unvalidated status off the wire.
 */
export function invoiceStatusLabelKey(status: string): MessageKey | null {
	return INVOICE_STATUS_LABEL_KEYS[status as InvoiceStatus] ?? null;
}

/** Statuses managed by the system — users should not select or bulk-act on these. */
export const SYSTEM_MANAGED_STATUSES: Set<InvoiceStatus> = new Set([
	'pending',
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'payment_scheduled',
	'paid',
	'done'
]);

/**
 * Statuses the server refuses to mutate or delete — the exact mirror of
 * `backend/app/api/invoices.py::IMMUTABLE_STATUSES`. `DELETE /api/invoices/{id}`
 * (and the bulk delete / bulk status endpoints) answer 409 for every one of
 * them, so a UI control that acts on such a row is a guaranteed error toast.
 *
 * Distinct from {@link SYSTEM_MANAGED_STATUSES}, which is about *selection*:
 * it additionally covers `pending` (mid-extraction — the server will happily
 * delete it, but a user shouldn't bulk-act on a row the extractor is writing).
 * Immutability is the server's rule; system-managed is the UI's. Keep this one
 * in lockstep with the backend set — a page-local copy is how the row Delete
 * action came to be offered on `posted_in_erp` / `payment_scheduled` / `paid`.
 */
export const IMMUTABLE_STATUSES: Set<InvoiceStatus> = new Set([
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'payment_scheduled',
	'paid',
	'done'
]);

/** Valid manual status transitions per source status. */
export const VALID_TRANSITIONS: Record<InvoiceStatus, InvoiceStatus[]> = {
	// Mirror the backend workflow_engine VALID_TRANSITIONS. `rejected` is only
	// reachable from `ready_for_review` (the review chokepoint) — offering it
	// from `new` or `approved` produced a guaranteed 409 on every selected row.
	new: ['ready_for_review', 'done'],
	pending: [],
	ready_for_review: ['approved', 'rejected'],
	approved: ['done'],
	rejected: ['new', 'ready_for_review'],
	sending_to_erp: [],
	sent_to_erp: [],
	posted_in_erp: [],
	payment_scheduled: [],
	paid: [],
	done: [],
	// Backend allows failed → {pending, sending_to_erp}; `new` is not reachable
	// (offering it 409s). `pending` is the user-meaningful retry.
	failed: ['pending'],
};

/**
 * Given a set of source statuses, return the status targets valid for ALL of them.
 */
export function commonTransitions(statuses: InvoiceStatus[]): InvoiceStatus[] {
	if (statuses.length === 0) return [];
	const sets = statuses.map((s) => new Set(VALID_TRANSITIONS[s]));
	const first = sets[0];
	return [...first].filter((t) => sets.every((s) => s.has(t)));
}

export interface AdvancedSearchFilters {
	vendor: string;
	invoice_number: string;
	po_number: string;
	description: string;
	amount_min: string;
	amount_max: string;
	due_date_from: string;
	due_date_to: string;
	statuses: InvoiceStatus[];
}

export const EMPTY_ADVANCED_FILTERS: AdvancedSearchFilters = {
	vendor: '',
	invoice_number: '',
	po_number: '',
	description: '',
	amount_min: '',
	amount_max: '',
	due_date_from: '',
	due_date_to: '',
	statuses: [],
};

export interface InvoiceWarning {
	type: string;
	severity: 'error' | 'warning' | 'info';
	/**
	 * The backend's own English sentence. It is the FALLBACK rendering, not the
	 * thing to render: prefer `invoiceWarningText` (`$lib/api/invoiceWarnings`),
	 * which resolves `code` + `params` into the reader's language and falls back
	 * to this when it cannot.
	 */
	message: string;
	/**
	 * Stable identity for the SENTENCE, not the category — one `type` is up to
	 * five of them (`backend/app/services/invoice_warning_catalog.py`). Absent
	 * on any warning persisted before that catalogue shipped; `refresh_warnings`
	 * re-derives it on the invoice's next write and nothing backfills.
	 */
	code?: string | null;
	/** The figures the sentence embeds, typed by the generated kind map. */
	params?: Record<string, string | number> | null;
}

export interface PoMatch {
	status: 'no_po' | 'matched' | 'mismatch' | 'partial';
	match_type: 'none' | '2-way' | '3-way' | '4-way';
	po_id: string | null;
	po_number: string | null;
	// `services/po_matching.py::_json_safe` renders every Decimal on the
	// persisted `invoices.po_match` JSONB down to a JSON number, so these
	// arrive numeric — but they are money, and the variance in particular is a
	// figure a reviewer reads, never one to recompute client-side.
	po_total: MoneyAmount;
	gr_id: string | null;
	amount_variance: MoneyAmount;
	amount_variance_pct: number;
	within_tolerance: boolean;
	inspection_id: string | null;
	inspection_result: 'pass' | 'fail' | 'partial' | null;
	inspection_accepted_quantity: number | null;
	inspection_required: boolean;
	issues: string[];
	details: Record<string, unknown>;
}

export interface Invoice {
	id: string;
	correlation_id: string;
	vendor: string;
	/**
	 * Resolved link to the vendor record. `null` when the invoice's vendor could
	 * not be established — such an invoice can NOT take a credit memo (the
	 * backend refuses it fail-closed), so any vendor-scoped picker must exclude
	 * it rather than treat it as a wildcard.
	 */
	vendor_id: string | null;
	invoice_number: string;
	amount: MoneyAmount;
	currency: string;
	invoice_date: string | null;
	received_date: string | null;
	due_date: string | null;
	payment_terms: string | null;
	status: InvoiceStatus;
	po_number: string;
	subtotal: MoneyAmount;
	tax_amount: MoneyAmount;
	discount_amount: MoneyAmount;
	shipping_amount: MoneyAmount;
	remit_to_address: string | null;
	bill_to_address: string | null;
	vendor_address: string | null;
	vendor_tax_id: string | null;
	ship_to_address: string | null;
	tax_rate: number | null;
	payment_method: string | null;
	reference_number: string | null;
	description: string;
	notes: string | null;
	approval_date: string | null;
	approved_by: string | null;
	rejected_by: string | null;
	assigned_to_id: string | null;
	assigned_to: string | null;
	gl_account: string | null;
	cost_center: string | null;
	department: string | null;
	project: string | null;
	contract_id: string | null;
	/**
	 * Inter-company routing (multi-entity). `counterparty_entity_id` names the
	 * OTHER subsidiary on an inter-company charge; `intercompany_mirror_id` links
	 * an origin invoice to its generated mirror payable (and vice-versa). Both
	 * null on an ordinary invoice. `intercompany_mirror_id` being set is the
	 * ROUTED signal — the backend only stamps a counterparty while it is null, so
	 * the UI must render the routed state rather than re-offer the action.
	 * See `backend/docs/inter-company.md`.
	 */
	counterparty_entity_id: string | null;
	intercompany_mirror_id: string | null;
	created_at: string;
	/**
	 * The row's current `updated_at`, ISO-8601. Capture this verbatim when the
	 * invoice loads and echo it back as `expected_updated_at` on the next
	 * `PATCH` — the optimistic-concurrency token. Treat it as an opaque
	 * string, never round-trip it through a JS `Date` (`toISOString()`
	 * truncates to millisecond precision, which would make an untouched
	 * invoice look "modified" against the backend's microsecond timestamp
	 * and false-positive every save with a 409).
	 */
	updated_at: string;
	file_url: string | null;
	warnings: InvoiceWarning[] | null;
	po_match: PoMatch | null;
	priors_summary: { cache: number; rag: number } | null;
}

/**
 * One-paragraph audit-log summary shown at the top of the invoice detail
 * modal. Mirrors the backend `AuditSummaryResponse`. Fetched lazily from
 * `GET /api/invoices/{id}/summary`.
 */
export interface AuditSummary {
	text: string;
	confidence_context: string | null;
	generated_at: string | null;
	stale: boolean;
}
