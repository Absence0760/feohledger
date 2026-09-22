// Types for the Bank Reconciliation surface. Mirrors the JSON returned by the
// `/api/bank-reconciliation` endpoints (the Pydantic contract in
// `backend/app/schemas/bank_reconciliation.py`). Date fields are ISO strings
// (or null); money fields are `MoneyAmount`, never `number`.
//
// PII discipline mirrors the backend: `account_identifier` is whatever the
// importer typed to name the account (a nickname or a masked tail) — the full
// account / routing numbers never leave the bank's own file, and nothing here
// carries one. Nothing in this module widens what the API already exposes.
//
// Not entity-scoped: `BankStatement` / `BankTransaction` predate multi-entity
// and cover an org-wide bank account, so there is no entity filter here.

import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MoneyAmount } from '$lib/utils/money';

// --- Match method ---------------------------------------------------------

/**
 * How a bank line came to be linked to one of our payments.
 *
 * The first four are *identity* claims of descending strength; the last three
 * are the discrepancy classes — the line is linked to a payment but does NOT
 * reconcile with it (`services/bank_reconciliation.UNRECONCILED_MATCH_METHODS`).
 */
export type MatchMethod =
	| 'provider_id'
	| 'amount_date'
	| 'fuzzy_vendor'
	| 'manual'
	| 'amount_mismatch'
	| 'currency_mismatch'
	| 'status_conflict';

/**
 * The three classes where the bank moved money the payment does not support.
 * Mirrors `UNRECONCILED_MATCH_METHODS` — a linked line carrying one of these
 * is deliberately NOT counted in `BankStatement.matched_count`.
 */
export const DISCREPANCY_METHODS: readonly string[] = [
	'amount_mismatch',
	'currency_mismatch',
	'status_conflict'
];

/** Is this match method one of the three discrepancy classes? */
export function isDiscrepancyMethod(method: string | null | undefined): boolean {
	return method !== null && method !== undefined && DISCREPANCY_METHODS.includes(method);
}

// --- Match state — the judgment a row is asking the reader to make ---------

/**
 * What a reviewer is actually looking at on one transaction row.
 *
 * Derived rather than stored, because the API answers the question across
 * four fields (`direction`, `matched_payment_id`, `match_method`,
 * `match_confidence`) and a row that renders only one of them tells a
 * half-truth. In particular a 50–70 fuzzy vendor-name hit is a *suggestion* a
 * human still owes a decision on — rendering it the same green as a reference
 * match would be the UI asserting a fact the matcher never claimed.
 *
 *  - `credit`       — not a payment we made; the matcher skips these entirely.
 *  - `unmatched`    — a debit with no payment behind it (money left the
 *                     account we have no record of authorising).
 *  - `discrepancy`  — linked to a payment, but it does not reconcile.
 *  - `confirmed`    — certain identity: an exact reference hit, or a human's
 *                     own resolve (both land at confidence 100).
 *  - `probable`     — a single amount + date candidate in the window (80–99).
 *  - `suggested`    — vendor-name similarity only (50–79), or a linked row
 *                     carrying no confidence at all. Needs a human.
 */
export type MatchState =
	| 'credit'
	| 'unmatched'
	| 'discrepancy'
	| 'confirmed'
	| 'probable'
	| 'suggested';

/** The minimal row shape {@link transactionMatchState} reads. */
export interface MatchStateInput {
	direction: string;
	matched_payment_id: string | null;
	match_method: string | null;
	match_confidence: number | null;
	is_reconciled: boolean;
}

/**
 * Collapse a transaction's four match fields into the one judgment the row
 * renders. Pure — no clock, no fetch — so it is unit-tested directly.
 *
 * Order matters. `is_reconciled` is the backend's own predicate
 * (`services/bank_reconciliation.is_reconciled`), so a linked line it calls
 * unreconciled is a discrepancy even if `match_method` is a value this
 * frontend has never heard of: an unknown method must degrade to "a human
 * needs to look", never to a clean green tick.
 */
export function transactionMatchState(tx: MatchStateInput): MatchState {
	if (tx.direction !== 'debit') return 'credit';
	if (!tx.matched_payment_id) return 'unmatched';
	if (isDiscrepancyMethod(tx.match_method) || !tx.is_reconciled) return 'discrepancy';
	const confidence = tx.match_confidence;
	// A linked, reconciled row with no confidence figure is not evidence of
	// certainty — it is the absence of evidence. Treat it as a suggestion.
	if (confidence === null || confidence === undefined) return 'suggested';
	if (confidence >= 100) return 'confirmed';
	if (confidence >= 80) return 'probable';
	return 'suggested';
}

/**
 * States where a human still owes a decision — the rows the page pulls
 * forward and the ones that get the prominent resolve affordance.
 */
export function needsHumanDecision(state: MatchState): boolean {
	return state === 'discrepancy' || state === 'suggested' || state === 'unmatched';
}

/**
 * Badge tone per match state, so the statement table and the transaction rows
 * can't tint the same state two different shades.
 *
 * `probable` is `accent` rather than `success` on purpose: an amount + date
 * coincidence in a ±5-day window is a strong identity claim, not a confirmed
 * one, and green reads as "signed off".
 */
export const MATCH_STATE_TONES: Record<MatchState, BadgeTone> = {
	credit: 'neutral',
	unmatched: 'warning',
	discrepancy: 'danger',
	confirmed: 'success',
	probable: 'accent',
	suggested: 'warning'
};

// --- Response shapes ------------------------------------------------------

export interface BankTransaction {
	id: string;
	transaction_date: string;
	posted_date: string | null;
	amount: MoneyAmount;
	currency: string;
	description: string | null;
	counterparty_name: string | null;
	reference: string | null;
	/** `debit` | `credit`. Only a debit can clear a payment. */
	direction: string;
	matched_payment_id: string | null;
	matched_invoice_number: string | null;
	match_method: string | null;
	/** A 0–100 identity score, NOT a currency amount — see `MatchState`. */
	match_confidence: number | null;
	matched_at: string | null;
	/** What the account was debited for the matched payment (the FX leg's
	 *  home-currency figure for an international payment). */
	matched_payment_amount: MoneyAmount;
	matched_payment_currency: string | null;
	matched_payment_status: string | null;
	/** Signed gap: POSITIVE means the bank took MORE than we authorised.
	 *  Null when unmatched or when the two sides are in different currencies. */
	variance_amount: MoneyAmount;
	/** False for every discrepancy class — linked, but it has not cleared. */
	is_reconciled: boolean;
}

export interface BankStatement {
	id: string;
	account_identifier: string;
	currency: string;
	period_start: string;
	period_end: string;
	source_format: string;
	file_key: string | null;
	opening_balance: MoneyAmount;
	closing_balance: MoneyAmount;
	/** Row counts, not money. */
	transaction_count: number;
	/** RECONCILED lines only — a discrepancy line is linked but not cleared. */
	matched_count: number;
	amount_mismatch_count: number;
	/** Every linked-but-unreconciled line: the single "needs a human" number. */
	discrepancy_count: number;
	imported_at: string;
	created_at: string;
	/** Present on the detail response only; the list omits them. */
	transactions: BankTransaction[] | null;
}

// --- Outstanding items (`GET /outstanding`) --------------------------------

export interface UnclearedPayment {
	payment_id: string;
	invoice_id: string;
	invoice_number: string | null;
	vendor_name: string | null;
	amount: MoneyAmount;
	/** The currency `amount` is denominated in (the invoice's). */
	currency: string | null;
	method: string | null;
	status: string;
	sent_on: string | null;
	/** Age in days — a duration, not money. */
	days_outstanding: number | null;
}

export interface UnmatchedDebit {
	transaction_id: string;
	statement_id: string;
	account_identifier: string;
	transaction_date: string;
	amount: MoneyAmount;
	currency: string;
	counterparty_name: string | null;
	reference: string | null;
	description: string | null;
}

export interface Discrepancy {
	transaction_id: string;
	statement_id: string;
	account_identifier: string;
	transaction_date: string;
	/** One of `DISCREPANCY_METHODS`. */
	classification: string;
	bank_amount: MoneyAmount;
	bank_currency: string;
	payment_amount: MoneyAmount;
	payment_currency: string | null;
	payment_status: string | null;
	/** Set for the amount class only — a cross-currency gap isn't money. */
	variance_amount: MoneyAmount;
	payment_id: string;
	invoice_number: string | null;
	counterparty_name: string | null;
}

/** One currency's slice of an outstanding-items total. `total` is an EXACT
 *  decimal string, matching every other whole-set rollup in this app. */
export interface BankReconCurrencyTotal {
	currency: string;
	total: string;
}

export interface OutstandingItems {
	as_of: string;
	older_than_days: number;
	uncleared_payments: UnclearedPayment[];
	uncleared_count: number;
	/** Grouped per currency as exact decimal strings — never one blended sum.
	 *  `Payment.amount` is invoice-currency, so a cross-currency total is
	 *  denominated in nothing real. Render via `formatCurrencyTotals`. */
	uncleared_totals: BankReconCurrencyTotal[];
	unmatched_debits: UnmatchedDebit[];
	unmatched_debit_count: number;
	/** Same rule — a statement carries its own currency. */
	unmatched_debit_totals: BankReconCurrencyTotal[];
	discrepancies: Discrepancy[];
	discrepancy_count: number;
	/** Signed sum of the amount-mismatch subset. Positive = the bank has taken
	 *  more than we authorised in aggregate. */
	/** Signed net of the amount-mismatch subset, grouped per currency. Two
	 *  mismatch lines in two currencies cannot be added, so this is never one
	 *  figure. Render via `formatCurrencyTotals`. */
	amount_mismatch_net_variances: BankReconCurrencyTotal[];
}

/**
 * Did `/outstanding` cap a bucket's rows?
 *
 * Every count and total on that response covers the FULL set while the row
 * list is capped at `?limit`, so a page that filters the rows client-side has
 * to say when it is looking at less than the count above it claims. Pure.
 */
export function isTruncated(shown: number, total: number): boolean {
	return shown < total;
}

/** The paginated statement-list envelope.
 *
 *  `total` is a pagination ROW COUNT, not money — recorded as such in
 *  `moneyTypeAudit.test.ts`'s `JUDGED_NOT_MONEY`, so the ratchet does not
 *  re-raise it every round. */
export interface BankStatementListResponse {
	items: BankStatement[];
	total: number;
	page: number;
	page_size: number;
}
