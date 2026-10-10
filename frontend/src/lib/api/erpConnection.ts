/**
 * Organization → ERP: the provider catalogue and the OAuth connection.
 *
 * The configuration is saved through `PATCH /api/organization` (`settings.erp`,
 * no secret in it); the secrets through `PUT /api/organization/credentials/erp`
 * (`#lib/api/providerCredentials.ts`, body from `buildErpSecretUpdate`). The
 * OAuth tokens are never sent from here: the provider's callback is their only
 * writer, and these helpers only start, read and end that flow.
 */
import { api } from '#lib/api.ts';
import type { ErpCatalog, ErpOAuthStatus } from '#lib/types/erpConnection.ts';

const BASE = '/api/organization/erp';

export const getErpCatalog = () => api.get<ErpCatalog>(`${BASE}/providers`);

export const getErpOAuthStatus = () => api.get<ErpOAuthStatus>(`${BASE}/oauth/status`);

export const startErpOAuth = (provider: string) =>
	api.get<{ authorize_url: string }>(`${BASE}/oauth/${encodeURIComponent(provider)}/authorize`);

export const disconnectErpOAuth = () => api.post<unknown>(`${BASE}/oauth/disconnect`, {});

export const testErpConnection = (erp: Record<string, unknown>) =>
	api.post<{ success: boolean; message: string }>('/api/organization/test-erp', erp);
