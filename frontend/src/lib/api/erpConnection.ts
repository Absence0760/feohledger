/**
 * Organization → ERP: the provider catalogue and the OAuth connection.
 *
 * Saving the credentials themselves still goes through `PATCH
 * /api/organization` (`settings.erp`), which keeps a blank or masked secret.
 * The OAuth token block is never sent from here: the provider's callback is
 * its only writer, and these helpers only start, read and end that flow.
 */
import { api } from '#lib/api.ts';
import type { ErpCatalog, ErpOAuthStatus } from '#lib/types/erpConnection.ts';

const BASE = '/api/organization/erp';

export const getErpCatalog = () => api.get<ErpCatalog>(`${BASE}/providers`);

export const getErpOAuthStatus = () => api.get<ErpOAuthStatus>(`${BASE}/oauth/status`);

export const startErpOAuth = (provider: string) =>
	api.get<{ authorize_url: string }>(`${BASE}/oauth/${encodeURIComponent(provider)}/authorize`);

export const disconnectErpOAuth = () => api.post<unknown>(`${BASE}/oauth/disconnect`, {});

export const testErpConnection = (erp: Record<string, string>) =>
	api.post<{ success: boolean; message: string }>('/api/organization/test-erp', erp);
