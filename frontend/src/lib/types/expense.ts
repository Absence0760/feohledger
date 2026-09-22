// Types for the Expenses surface. Mirrors the JSON returned by the
// `/api/expenses` + `/api/expense-reports` endpoints (backend
// `ExpenseResponse` / `ExpenseReportResponse`). Money fields arrive as
// numbers (backend `float(...)`); date/datetime fields are ISO strings.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount, MoneyString } from '$lib/utils/money';

export type ExpenseStatus = 'draft' | 'submitted' | 'approved' | 'rejected' | 'reimbursed';

// Every value the `expenses.status` column can hold — the full mirror of the
// backend `ExpenseStatus` enum (`backend/app/models/expense.py`). Kept COMPLETE
// on purpose: a row can still arrive carrying any of them (the demo seed
// `backend/scripts/seed_extras.py` writes `rejected` + `reimbursed`, and a
// long-lived tenant may hold rows written before a transition changed), so
// every value must still render a badge. This is NOT the filter-chip list —
// see `EXPENSE_FILTER_STATUSES`.
export const EXPENSE_STATUSES: ExpenseStatus[] = [
	'draft',
	'submitted',
	'approved',
	'rejected',
	'reimbursed'
];

// Statuses no backend transition ever stamps, so a filter chip for them is a
// control that can never return a row. Every writer of `Expense.status` in the
// backend:
//   - `draft`     — the column default on insert (`models/expense.py:229`), and
//                   `reject_report` (`api/expenses.py:1386`) which puts a
//                   rejected report's children BACK to draft.
//   - `submitted` — `submit_report` (`api/expenses.py:1230`).
//   - `approved`  — `approve_report` (`api/expenses.py:1341`).
// That is the complete list under `backend/app/`. Nothing writes `rejected`
// (report rejection returns children to `draft`) and nothing writes
// `reimbursed` (there is no reimbursement transition anywhere — no route even
// sets `ExpenseReportStatus.reimbursed`). Only the demo seed
// `backend/scripts/seed_extras.py` writes them.
//
// Delete an entry here the moment its writer lands — `reject_report` stamping
// `ExpenseStatus.rejected` on its children, or a new reimbursement endpoint
// stamping `ExpenseStatus.reimbursed`.
const UNREACHABLE_EXPENSE_STATUSES: ExpenseStatus[] = ['rejected', 'reimbursed'];

// The subset offered as *filter chips* — derived by EXCLUSION from the full
// mirror, so a genuinely new status added above joins the chips by default and
// only a deliberate, justified entry in UNREACHABLE_EXPENSE_STATUSES keeps one
// out. The excluded values still live in the union and in
// EXPENSE_STATUS_LABEL_KEYS (a legacy / seeded row must still render its badge),
// and the page appends whatever status is *actively* filtered to the chip row,
// so an explicit `?status=reimbursed` is never an invisible filter.
export const EXPENSE_FILTER_STATUSES: ExpenseStatus[] = EXPENSE_STATUSES.filter(
	(s) => !UNREACHABLE_EXPENSE_STATUSES.includes(s)
);

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Every surface that renders an expense status (the `/expenses` list, the
 * report-detail line table and `ExpenseModal`) is inside the i18n extraction
 * slice, so a hardcoded English map here put translated filter chips directly
 * beside an untranslated `Reimbursed` badge. `Record<ExpenseStatus,
 * MessageKey>` makes a new status a compile error rather than a blank badge,
 * and `expense.test.ts` proves every key exists in the catalogue.
 */
export const EXPENSE_STATUS_LABEL_KEYS: Record<ExpenseStatus, MessageKey> = {
	draft: 'expenses.status.draft',
	submitted: 'expenses.status.submitted',
	approved: 'expenses.status.approved',
	rejected: 'expenses.status.rejected',
	reimbursed: 'expenses.status.reimbursed'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). `Expense.status` is typed `string` because the API is the
 * source of truth.
 */
export function expenseStatusLabelKey(status: string): MessageKey | null {
	return EXPENSE_STATUS_LABEL_KEYS[status as ExpenseStatus] ?? null;
}

/**
 * Badge tone per expense status. Hoisted out of `ExpenseModal`, which is where
 * it was first written and where it left a note saying it belonged here once
 * the list page converted. It now has three callers — the modal, the Expenses
 * table and the report-detail line table — which is exactly the shape
 * `frontend/CLAUDE.md` § Badge asks for.
 *
 * Total record: a status added to `ExpenseStatus` is a compile error here
 * rather than an untinted pill.
 *
 * `reimbursed` takes the `erp` tone — the measured purple the list page's rule
 * spelled by hand, doing the job that tone does elsewhere: handed off
 * downstream. Green would collapse it into `approved`, and "someone approved
 * this" and "the money went back" are different answers to the only question
 * an employee asks of this pill.
 */
export const EXPENSE_STATUS_TONES: Record<ExpenseStatus, BadgeTone> = {
	draft: 'accent',
	submitted: 'warning',
	approved: 'success',
	rejected: 'danger',
	reimbursed: 'erp'
};

export type ExpensePaymentMethod = 'out_of_pocket' | 'corporate_card' | 'virtual_card';

export const EXPENSE_PAYMENT_METHODS: ExpensePaymentMethod[] = [
	'out_of_pocket',
	'corporate_card',
	'virtual_card'
];

// The payment method is a `<select>` option and a table cell sitting beside
// the translated status badge, so an English literal here reads as a gap.
export const EXPENSE_PAYMENT_METHOD_LABEL_KEYS: Record<ExpensePaymentMethod, MessageKey> = {
	out_of_pocket: 'expenses.method.outOfPocket',
	corporate_card: 'expenses.method.corporateCard',
	virtual_card: 'expenses.method.virtualCard'
};

export type ExpenseReportStatus =
	| 'draft'
	| 'submitted'
	| 'pending_approval'
	| 'approved'
	| 'rejected'
	| 'reimbursed'
	| 'cancelled';

export const EXPENSE_REPORT_STATUSES: ExpenseReportStatus[] = [
	'draft',
	'submitted',
	'pending_approval',
	'approved',
	'rejected',
	'reimbursed',
	'cancelled'
];

/** Message keys for the report status pill — see EXPENSE_STATUS_LABEL_KEYS. */
export const EXPENSE_REPORT_STATUS_LABEL_KEYS: Record<ExpenseReportStatus, MessageKey> = {
	draft: 'expenses.reports.status.draft',
	submitted: 'expenses.reports.status.submitted',
	pending_approval: 'expenses.reports.status.pendingApproval',
	approved: 'expenses.reports.status.approved',
	rejected: 'expenses.reports.status.rejected',
	reimbursed: 'expenses.reports.status.reimbursed',
	cancelled: 'expenses.reports.status.cancelled'
};

/** The message key for a report status, or `null` for an unrecognised one. */
export function expenseReportStatusLabelKey(status: string): MessageKey | null {
	return EXPENSE_REPORT_STATUS_LABEL_KEYS[status as ExpenseReportStatus] ?? null;
}

/**
 * Badge tone per expense-REPORT status. Two callers on the same page — the
 * reports table and the report-detail header, which the e2e suite reads as
 * `.report-title-block .badge`.
 *
 * `submitted` and `pending_approval` share `warning` because the report is
 * waiting on someone in both, which is what the pill is for. They keep their
 * own labels (and their own filter chips), so the states stay distinguishable
 * in text — SC 1.4.1 — while the colour answers the only scannable question.
 *
 * `cancelled` is `neutral`, not a grey tint: a withdrawn report is the absence
 * of a signal rather than a weak one. That is the same call `/payments` makes
 * for a `draft` run, and the opposite of the one it makes for a `voided`
 * payment — money that moved and came back is an event; a report nobody
 * pursued is not.
 */
export const EXPENSE_REPORT_STATUS_TONES: Record<ExpenseReportStatus, BadgeTone> = {
	draft: 'accent',
	submitted: 'warning',
	pending_approval: 'warning',
	approved: 'success',
	rejected: 'danger',
	reimbursed: 'erp',
	cancelled: 'neutral'
};

// A single policy-engine finding stamped onto `Expense.policy_violations` by the
// WF3 backend engine (`evaluate_expense`). `code` is a stable machine key
// (`category_limit`, `receipt_required`, `preapproval_required`,
// `per_diem_exceeded`, `mileage_amount_mismatch`), `message` is the human
// string the badge tooltip renders.
export interface PolicyViolation {
	code: string;
	message: string;
	policy_id?: string;
	/** Exact decimal strings — money never round-trips as a JS number. */
	limit?: string;
	actual?: string;
	/**
	 * `mileage_amount_mismatch` only — the working behind `limit`, as exact
	 * strings. `miles` is a distance, not money; `rate` is per-mile in
	 * `currency`.
	 */
	miles?: string;
	rate?: string;
	/** Currency `limit` (and, when the comparison resolved, `actual`) is in. */
	currency?: string;
	/**
	 * Present only when the expense could not be expressed in `currency`, so the
	 * rule fell closed and was flagged without a comparison. `actual` is then the
	 * expense's face amount, in `expense_currency`.
	 */
	comparison?: 'unresolved';
	expense_currency?: string;
}

export interface Expense {
	id: string;
	report_id: string | null;
	expense_date: string;
	merchant: string | null;
	category: string | null;
	description: string | null;
	amount: MoneyAmount;
	currency: string;
	// Rate-locked expression of `amount` in the owning report's currency
	// (issue #157). Exact decimal STRINGS — never parse into a float for
	// arithmetic. null when the expense isn't attached to a report.
	converted_amount: string | null;
	converted_currency: string | null;
	converted_fx_rate: string | null;
	converted_fx_locked_at: string | null;
	gl_account_id: string | null;
	receipt_file_key: string | null;
	receipt_url: string | null;
	payment_method: string;
	card_transaction_id: string | null;
	policy_violations: PolicyViolation[] | null;
	status: string;
	reimbursable: boolean;
	mileage_miles: number | null;
	created_at: string;
	updated_at: string;
}

export interface ExpenseListResponse {
	items: Expense[];
	total: number;
	page: number;
	page_size: number;
}

/** One currency's slice of `GET /api/expenses/summary`. */
export interface ExpenseCurrencyTotal {
	currency: string;
	/** Exact decimal string — never parse into a float for arithmetic. */
	total: MoneyString;
	count: number;
}

/**
 * Whole-set rollup from `GET /api/expenses/summary`, over the SAME filters the
 * list ran with.
 *
 * The KPI row used to derive its figures from the loaded page, so "Period
 * total" and "Pending" described 20 rows while the "Expenses" card beside them
 * described every row. `by_currency` is grouped rather than summed — adding EUR
 * to USD produces a figure denominated in nothing (see
 * `$lib/utils/currencyGroups`).
 */
export interface ExpenseSummary {
	total: number;
	by_status: Record<string, number>;
	by_currency: ExpenseCurrencyTotal[];
}

export interface ExpenseReport {
	id: string;
	report_number: string;
	title: string | null;
	employee_user_id: string;
	status: string;
	submitted_at: string | null;
	approved_at: string | null;
	approved_by: string | null;
	total_amount: MoneyAmount;
	// Exact `total_amount` (in `currency`) as a decimal string.
	total_amount_exact: string;
	currency: string;
	// `total_amount` re-expressed in the org reporting currency at the rate
	// locked on submit — the figure the CFO threshold gate compares. null
	// before submit or when no rate was available (the gate then requires CFO).
	reporting_amount: string | null;
	reporting_currency: string | null;
	reporting_fx_rate: string | null;
	reporting_fx_locked_at: string | null;
	notes: string | null;
	expenses: Expense[];
	created_at: string;
	updated_at: string;
}

export interface ExpenseReportListResponse {
	items: ExpenseReport[];
	total: number;
	page: number;
	page_size: number;
}

// `GET /api/expense-reports/{id}/summary` — computed from the report's expenses.
export interface ExpenseSummaryBucket {
	category?: string | null;
	status?: string | null;
	total: MoneyAmount;
	// Exact `total` as a decimal string.
	total_exact: string;
	// Lines with no usable rate lock — EXCLUDED from `total`.
	unconverted_count: number;
	count: number;
}

// Per-currency split of a report's lines. `original_amount` is the face value
// in that currency; `report_amount` is its rate-locked contribution to the
// report total. Both exact decimal strings.
export interface ExpenseCurrencyBucket {
	currency: string;
	count: number;
	original_amount: string;
	report_amount: string;
	unconverted_count: number;
}

export interface ExpenseReportSummary {
	total: MoneyAmount;
	// Exact `total`, denominated in `currency` — the report's own currency.
	// Every figure here is converted at each line's LOCKED rate, never a naive
	// cross-currency sum (issue #157).
	total_exact: string;
	currency: string;
	count: number;
	// Non-zero means the displayed totals are partial: some lines have no
	// usable rate lock and were excluded (they also block submission).
	unconverted_count: number;
	by_category: ExpenseSummaryBucket[];
	by_status: ExpenseSummaryBucket[];
	by_currency: ExpenseCurrencyBucket[];
}

// Payload shapes for create / update (request side). Money goes out as a
// number — the backend coerces to Decimal. Optional fields default server-side.
export interface ExpenseCreate {
	/**
	 * Required, `YYYY-MM-DD`. The column is NOT NULL and both `ExpenseCreate`
	 * and `ExpenseUpdate` type it as a bare `date` server-side — a null here is
	 * a 422 on create and (before the schema was tightened) a 500 on PATCH.
	 * `updateExpense` takes `Partial<ExpenseCreate>`, so a PATCH may still omit
	 * the field entirely; it may never send it empty.
	 */
	expense_date: string;
	merchant: string | null;
	category: string | null;
	description: string | null;
	/** Request side — the exact decimal text typed, never a JSON number. */
	amount: MoneyString;
	currency: string;
	gl_account_id: string | null;
	payment_method: string;
	reimbursable: boolean;
	/**
	 * Distance driven. Backed by the policy engine's `mileage_rate`, which
	 * flags a claim that isn't `miles x rate` (`mileage_amount_mismatch`) —
	 * so leaving this null is what tells the backend the line isn't a trip.
	 */
	mileage_miles?: number | null;
	report_id?: string | null;
}

export interface ExpenseReportCreate {
	report_number: string;
	title: string | null;
	currency: string;
	notes: string | null;
}

// ============================ WF3: Policies ============================

export interface ExpensePolicy {
	id: string;
	name: string;
	active: boolean;
	category: string | null;
	/**
	 * Currency every money threshold on this policy is denominated in.
	 * null = "the org's reporting currency" (resolved server-side at evaluation
	 * time) — the unit a bare threshold number has always implicitly had.
	 */
	threshold_currency: string | null;
	per_diem_amount: MoneyAmount;
	per_diem_currency: string | null;
	// A money-PER-MILE rate, not an amount: the policy engine flags a claim
	// that isn't `miles x rate`, so this is a factor in that product rather
	// than a figure the UI renders as currency.
	mileage_rate: number | null;
	category_limit: MoneyAmount;
	requires_preapproval_above: MoneyAmount;
	requires_receipt_above: MoneyAmount;
	rules: unknown | null;
	created_at: string;
	updated_at: string;
}

export interface ExpensePolicyCreate {
	name: string;
	active: boolean;
	category: string | null;
	threshold_currency: string | null;
	/** Request side — the exact decimal text typed, never a JSON number. */
	category_limit: MoneyString | null;
	requires_receipt_above: MoneyString | null;
	requires_preapproval_above: MoneyString | null;
	per_diem_amount: MoneyString | null;
	mileage_rate: number | null;
}

// ========================= WF3: Pre-approvals =========================

export type ExpensePreapprovalStatus = 'pending' | 'approved' | 'rejected';

export const EXPENSE_PREAPPROVAL_STATUSES: ExpensePreapprovalStatus[] = [
	'pending',
	'approved',
	'rejected'
];

/** Message keys for the pre-approval status pill. */
export const EXPENSE_PREAPPROVAL_STATUS_LABEL_KEYS: Record<ExpensePreapprovalStatus, MessageKey> =
	{
		pending: 'expenses.preapprovals.status.pending',
		approved: 'expenses.preapprovals.status.approved',
		rejected: 'expenses.preapprovals.status.rejected'
	};

/** The message key for a pre-approval status, or `null` for an unknown one. */
export function expensePreapprovalStatusLabelKey(status: string): MessageKey | null {
	return EXPENSE_PREAPPROVAL_STATUS_LABEL_KEYS[status as ExpensePreapprovalStatus] ?? null;
}

/** Badge tone per pre-approval status — a decision pending, taken, or refused. */
export const EXPENSE_PREAPPROVAL_STATUS_TONES: Record<ExpensePreapprovalStatus, BadgeTone> = {
	pending: 'warning',
	approved: 'success',
	rejected: 'danger'
};

export interface ExpensePreapproval {
	id: string;
	requester_user_id: string;
	title: string;
	estimated_amount: MoneyAmount;
	currency: string;
	category: string | null;
	justification: string | null;
	status: string;
	decided_by: string | null;
	decided_at: string | null;
	expense_report_id: string | null;
	created_at: string;
	updated_at: string;
}

export interface ExpensePreapprovalCreate {
	title: string;
	/** Request side — the exact decimal text typed, never a JSON number. */
	estimated_amount: MoneyString;
	currency: string;
	category: string | null;
	justification: string | null;
}

// ===================== WF4: Corporate-card transactions =====================

export type ReconciliationStatus = 'unmatched' | 'matched' | 'ignored';

export const RECONCILIATION_STATUSES: ReconciliationStatus[] = [
	'unmatched',
	'matched',
	'ignored'
];

/** Message keys for the card-transaction reconciliation status pill. */
export const RECONCILIATION_STATUS_LABEL_KEYS: Record<ReconciliationStatus, MessageKey> = {
	unmatched: 'expenses.cards.status.unmatched',
	matched: 'expenses.cards.status.matched',
	ignored: 'expenses.cards.status.ignored'
};

/** The message key for a reconciliation status, or `null` for an unknown one. */
export function reconciliationStatusLabelKey(status: string): MessageKey | null {
	return RECONCILIATION_STATUS_LABEL_KEYS[status as ReconciliationStatus] ?? null;
}

/**
 * Badge tone per card-transaction reconciliation status.
 *
 * `unmatched` is `warning`, not `danger`: a charge nobody has coded yet is
 * work outstanding, not a failure — and this table has no failure state to
 * confuse it with. `ignored` is `neutral` (flat), the deliberate "no signal"
 * chip: someone has decided this line needs nothing, which is the absence of a
 * signal rather than a weak one.
 */
export const RECONCILIATION_STATUS_TONES: Record<ReconciliationStatus, BadgeTone> = {
	unmatched: 'warning',
	matched: 'success',
	ignored: 'neutral'
};

// Mirrors the backend `CorporateCardTransactionResponse`. Money fields arrive as
// numbers (backend `float(...)` on the `Numeric(15,2)` column); date fields are
// ISO date strings. PII: only `card_last_four` is ever surfaced — never a PAN.
export interface CorporateCardTransaction {
	id: string;
	card_ref: string | null;
	card_last_four: string | null;
	virtual_card_id: string | null;
	txn_date: string;
	posted_date: string | null;
	merchant: string | null;
	amount: MoneyAmount;
	currency: string;
	external_txn_id: string | null;
	matched_expense_id: string | null;
	reconciliation_status: string;
	import_batch: string | null;
	created_at: string;
	updated_at: string;
}

export interface CardTransactionListResponse {
	items: CorporateCardTransaction[];
	total: number;
	page: number;
	page_size: number;
}

// A ranked candidate expense for reconciliation (from
// GET /{id}/match-suggestions) — the Expense plus a match score.
export interface CardMatchSuggestion {
	expense: Expense;
	score: number;
}

// POST /import-csv returns the shared `ImportResult.to_dict()` — `imported` /
// `skipped` / `errors`.
export interface CardImportResult {
	imported: number;
	skipped: number;
	errors: { row: number; message: string }[];
}

// POST /sync-virtual-cards return shape — created vs already-imported.
export interface SyncVirtualCardsResult {
	created: number;
	skipped: number;
}
