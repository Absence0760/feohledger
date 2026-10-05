// Typed helpers for the retention-policy admin surface. Routes through the
// shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never raw fetch.
// Backend: `backend/app/api/retention.py` (admin-gated).
import { api } from '#lib/api.ts';
import type { RetentionPolicyResponse } from '#lib/types/retention.ts';

/** Effective per-class retention windows, the platform default, and whether
 *  the enforcement sweep is running. */
export function getRetentionPolicy(): Promise<RetentionPolicyResponse> {
	return api.get<RetentionPolicyResponse>('/api/retention-policy');
}

/** Update one or more per-class windows (months, > 0). Omitted classes are
 *  left unchanged server-side. */
export function updateRetentionPolicy(
	policy: Record<string, number>
): Promise<RetentionPolicyResponse> {
	return api.put<RetentionPolicyResponse>('/api/retention-policy', { policy });
}
