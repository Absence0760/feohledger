// Typed helpers for the approval-signature verification endpoints
// (`backend/app/api/audit.py`, admin/CFO gated — the auditor privilege).
// Routes through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce).
//
// Both reads are themselves audited server-side (`audit.viewed`), so the UI
// calls them ONLY on an explicit user action — never on mount, never polled.
// A speculative fetch would write an access row nobody asked for.
import { api } from '#lib/api.ts';

/** The three verdicts `services/approval_signature.check_approval_row` returns.
 *  `invalid` and `unsigned` are deliberately DIFFERENT claims — see
 *  `backend/docs/approval-signatures.md`. Never collapse them into one count. */
export type SignatureVerdict = 'valid' | 'invalid' | 'unsigned';

/** One row that did not verify. Every field but `verdict` can be null: a row
 *  whose JSONB `details` was overwritten wholesale (the direct-DB tamper this
 *  control exists to catch) surfaces here as a single `unsigned` finding with
 *  no `signed_at` and no resolvable actor, rather than 500-ing the sweep. */
export interface SignatureFinding {
	invoice_id: string;
	invoice_number: string | null;
	audit_row_id: string;
	actor_id: string | null;
	actor?: string | null;
	signed_at: string | null;
	verdict: SignatureVerdict;
}

/** `GET /api/audit/verify-signatures` — population test over a period. The
 *  counts always cover the whole range; only `findings` is bounded by `limit`. */
export interface SignatureVerificationReport {
	start: string | null;
	end: string | null;
	/** False when `FEOH_APPROVAL_SIGNING_KEY` is empty — signing was skipped, so
	 *  every approval reads `unsigned` for a configuration reason, not a
	 *  forensic one. The UI must say so rather than showing a bare red result. */
	signing_configured: boolean;
	invoices_covered: number;
	approvals_checked: number;
	valid: number;
	invalid: number;
	unsigned: number;
	findings: SignatureFinding[];
	findings_truncated: boolean;
}

/** One approval row on the per-invoice drill-down. */
export interface InvoiceSignatureRow {
	audit_row_id: string;
	signed_at: string | null;
	actor: string | null;
	signed: boolean;
	valid: boolean;
}

/** `GET /api/audit/invoice/{id}/verify-signatures`. */
export interface InvoiceSignatureReport {
	invoice_id: string;
	signing_configured: boolean;
	approvals: InvoiceSignatureRow[];
}

export interface VerifySignaturesParams {
	start?: string; // ISO date (YYYY-MM-DD)
	end?: string; // ISO date (YYYY-MM-DD)
	/** Caps the findings list only — never the counts. Backend default 100, max 1000. */
	limit?: number;
}

/** Population sweep over a date range. At least one of start/end is required
 *  (the backend 400s otherwise); `end` is whole-day inclusive. */
export function verifySignaturesForPeriod(
	params: VerifySignaturesParams
): Promise<SignatureVerificationReport> {
	const q = new URLSearchParams();
	if (params.start) q.set('start', params.start);
	if (params.end) q.set('end', params.end);
	if (params.limit !== undefined) q.set('limit', String(params.limit));
	return api.get<SignatureVerificationReport>(`/api/audit/verify-signatures?${q.toString()}`);
}

/** Per-invoice drill-down — "is THIS approval still intact". */
export function verifyInvoiceSignatures(invoiceId: string): Promise<InvoiceSignatureReport> {
	return api.get<InvoiceSignatureReport>(`/api/audit/invoice/${invoiceId}/verify-signatures`);
}
