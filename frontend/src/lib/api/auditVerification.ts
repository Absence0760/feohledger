// Typed helpers for the approval-signature verification endpoints
// (`backend/app/api/audit.py`, admin/CFO gated — the auditor privilege).
// Routes through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce).
//
// Both reads are themselves audited server-side (`audit.viewed`), so the UI
// calls them ONLY on an explicit user action — never on mount, never polled.
// A speculative fetch would write an access row nobody asked for.
import { api } from '#lib/api.ts';

export type {
	SignatureVerdict,
	SignatureFinding,
	SignatureVerificationReport,
	InvoiceSignatureRow,
	InvoiceSignatureReport,
	VerifySignaturesParams
} from '#lib/types/auditVerification.ts';
import type {
	SignatureVerificationReport,
	InvoiceSignatureReport,
	VerifySignaturesParams
} from '#lib/types/auditVerification.ts';

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
