// Typed helpers for the admin API-key management surface. Routes through the
// shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never raw fetch.
// Backend: `backend/app/api/api_keys.py` (admin + JWT gated). The mint response
// carries the plaintext `key` exactly once; everything else is metadata only.
import { api } from '#lib/api.ts';
import type { ApiKey, ApiKeyCreated, ApiKeyUsage } from '#lib/types/apiKeys.ts';

/** This org's API keys (active + revoked), newest first. Metadata only. */
export function listApiKeys(): Promise<ApiKey[]> {
	return api.get<ApiKey[]>('/api/api-keys');
}

/** Mint a new read-scoped key. The response is the ONLY place the plaintext
 *  key is returned — surface it once, then drop it. */
export function createApiKey(name: string): Promise<ApiKeyCreated> {
	return api.post<ApiKeyCreated>('/api/api-keys', { name });
}

/** Soft-revoke a key (idempotent server-side — revoking an already-revoked key
 *  is a no-op 200). The backend returns the updated metadata, but callers
 *  re-list afterwards, so we model it as void. */
export function revokeApiKey(id: string): Promise<void> {
	return api.delete(`/api/api-keys/${id}`);
}

/** Per-key usage totals + per-day breakdown over a trailing window. */
export function getApiKeyUsage(id: string, windowDays = 30): Promise<ApiKeyUsage> {
	return api.get<ApiKeyUsage>(`/api/api-keys/${id}/usage?window_days=${windowDays}`);
}
