// Typed helpers for the Bank Reconciliation endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce).
// Mirrors `src/lib/api/positivePay.ts`.
//
// Read is admin/ap_manager/ap_clerk/cfo; every mutation below (upload,
// resolve, delete) is admin/ap_manager only — treasury-adjacent, clerks
// excluded, the same write gate as Positive Pay. The page gates its controls
// on the same split; the backend is authoritative regardless.
import { api } from '#lib/api.ts';
import type {
	BankStatement,
	BankStatementListResponse,
	OutstandingItems
} from '#lib/types/bankReconciliation.ts';

export type { BankStatementListResponse };

export interface BankStatementListParams {
	/** EXACT account identifier — the value the upload form posts. Kept for a
	 *  caller that already holds one account label; use `search` for what a
	 *  reviewer types. */
	account_identifier?: string;
	/**
	 * Free-text term, applied SERVER-side over the account identifier, the
	 * source format and the ISO period dates. It must stay a server filter:
	 * the list paginates against a server `total`, so narrowing the loaded
	 * rows in the browser would report "nothing matched" for a statement on
	 * page 2 while the footer counted the whole set.
	 */
	search?: string;
	page?: number;
	page_size?: number;
}

export function listBankStatements(
	params: BankStatementListParams = {}
): Promise<BankStatementListResponse> {
	const qs = new URLSearchParams();
	if (params.account_identifier) qs.set('account_identifier', params.account_identifier);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<BankStatementListResponse>(`/api/bank-reconciliation?${qs}`);
}

export function getBankStatement(id: string): Promise<BankStatement> {
	return api.get<BankStatement>(`/api/bank-reconciliation/${id}`);
}

export interface OutstandingParams {
	/** Only report payments sent at least this many days ago (default 0). */
	older_than_days?: number;
	search?: string;
	/**
	 * Caps the ROWS returned per bucket only — every count and total on the
	 * response still covers the full set, so a truncated page can never
	 * understate the money. The page surfaces the gap rather than hiding it.
	 */
	limit?: number;
}

export function getOutstandingItems(params: OutstandingParams = {}): Promise<OutstandingItems> {
	const qs = new URLSearchParams();
	if (params.older_than_days) qs.set('older_than_days', String(params.older_than_days));
	if (params.search) qs.set('search', params.search);
	if (params.limit) qs.set('limit', String(params.limit));
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<OutstandingItems>(`/api/bank-reconciliation/outstanding${suffix}`);
}

export interface UploadStatementFields {
	account_identifier: string;
	period_start: string;
	period_end: string;
	currency?: string;
}

/**
 * Import a bank statement CSV. Idempotent on
 * `(org, account_identifier, sha256(body))` — re-uploading the same file for
 * the same account returns the EXISTING statement (200), never a second one.
 */
export function uploadBankStatement(
	file: File,
	fields: UploadStatementFields
): Promise<BankStatement> {
	return api.upload<BankStatement>('/api/bank-reconciliation/upload', file, {
		account_identifier: fields.account_identifier,
		period_start: fields.period_start,
		period_end: fields.period_end,
		currency: fields.currency
	});
}

/**
 * Manually set (`paymentId`) or clear (`null`) a transaction's matched payment,
 * and return the refreshed statement.
 *
 * Setting a match supplies an IDENTITY the matcher could not infer — it does
 * not assert that the line reconciles. The backend runs the same
 * `classify_discrepancy` on this path as on the automatic one, so a line
 * pointed at a payment it disagrees with comes back in a discrepancy class
 * rather than as a clean match. Re-sending a transaction's EXISTING
 * `matched_payment_id` is therefore how a human confirms a low-confidence
 * auto-match: it re-runs the classifier and stamps the human's own decision.
 */
export function resolveBankTransaction(
	statementId: string,
	transactionId: string,
	paymentId: string | null
): Promise<BankStatement> {
	return api.post<BankStatement>(
		`/api/bank-reconciliation/${statementId}/transactions/${transactionId}/resolve`,
		{ matched_payment_id: paymentId }
	);
}

export function deleteBankStatement(id: string): Promise<void> {
	return api.delete(`/api/bank-reconciliation/${id}`);
}
