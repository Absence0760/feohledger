// Typed helpers for the GDPR/CCPA privacy admin surface. Routes through the
// shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never raw fetch.
// Backend: `backend/app/api/privacy.py` (admin-gated). The DSAR bundle + the
// erasure confirmation are the two destructive/sensitive operations here —
// see `backend/docs/privacy.md`.
import { api } from '#lib/api.ts';
import type {
	DataSubjectRequestList,
	DSARRequest,
	DSARResponse,
	ErasureRequest,
	ErasureResponse
} from '#lib/types/privacy.ts';

/** Assemble a portable bundle of everything held about a data subject.
 *  Non-destructive — safe to re-run. The bundle is returned in the response
 *  only; it is never logged or persisted server-side. */
export function submitDsar(body: DSARRequest): Promise<DSARResponse> {
	return api.post<DSARResponse>('/api/privacy/dsar', body);
}

/** Irreversibly redact a subject's PII while preserving the money trail +
 *  the append-only audit log. `confirm: true` is required by the backend.
 *  Idempotent — a repeat call on an already-erased subject is a safe no-op
 *  (`status: "noop"`). */
export function submitErasure(body: ErasureRequest): Promise<ErasureResponse> {
	return api.post<ErasureResponse>('/api/privacy/erasure', body);
}

/** The privacy officer's request history for this tenant — PII-free. */
export function listPrivacyRequests(): Promise<DataSubjectRequestList> {
	return api.get<DataSubjectRequestList>('/api/privacy/requests');
}
