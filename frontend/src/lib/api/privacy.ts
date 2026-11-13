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

/** Download the COMPLETE bundle for an export already requested — every
 *  section uncapped, as a JSON file (`DSARResponse.full_bundle_url`). The
 *  inline response caps each large section; this is where the rest is. Fetched
 *  through the shared client because a bare `<a href>` cannot carry the token;
 *  the server audits every download. */
export async function downloadDsarBundle(path: string, filename: string): Promise<void> {
	const blob = await api.downloadBlob(path);
	const url = URL.createObjectURL(blob);
	try {
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		a.remove();
	} finally {
		URL.revokeObjectURL(url);
	}
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
