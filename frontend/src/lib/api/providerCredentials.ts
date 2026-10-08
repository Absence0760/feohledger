/**
 * Tenant provider credentials — ERP, payment-rail and card-issuer secrets
 * (admin only).
 *
 * Write-only end to end: values go out on {@link updateProviderCredentials}
 * and never come back on any read — the status lists stored path NAMES. Don't
 * add a helper that tries to fetch a value; the backend has no endpoint that
 * serves one.
 */
import { api } from '#lib/api.ts';
import type {
	CredentialBlock,
	ProviderCredentialStatus,
	ProviderCredentialUpdate
} from '#lib/types/providerCredentials.ts';

const BASE = '/api/organization/credentials';

export const getProviderCredentials = () => api.get<ProviderCredentialStatus>(BASE);

export const updateProviderCredentials = (block: CredentialBlock, body: ProviderCredentialUpdate) =>
	api.put<ProviderCredentialStatus>(`${BASE}/${block}`, body);
