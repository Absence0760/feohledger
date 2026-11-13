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
import type {
	ErpCatalog,
	ErpOAuthStatus,
	ErpPendingConnect,
	ErpTestResult
} from '#lib/types/erpConnection.ts';

const BASE = '/api/organization/erp';

export const getErpCatalog = () => api.get<ErpCatalog>(`${BASE}/providers`);

export const getErpOAuthStatus = () => api.get<ErpOAuthStatus>(`${BASE}/oauth/status`);

/**
 * The provider's consent URL. A CREDENTIALED request (`credentials: 'include'`):
 * the response sets the HttpOnly nonce cookie on the API host that the OAuth
 * callback compares against the signed `state`, binding the connect to this
 * browser (login-CSRF — `docs/authentication.md` § ERP OAuth connections).
 * Without it the browser discards the cookie and every callback answers
 * `erp_error=browser_mismatch`.
 */
export const startErpOAuth = (provider: string) =>
	api.get<{ authorize_url: string }>(
		`${BASE}/oauth/${encodeURIComponent(provider)}/authorize`,
		{ credentials: 'include' }
	);

/**
 * The company picker (decisions §355). When a consent reaches several
 * companies the callback holds it server-side and returns with
 * `?erp_choose_company=`. All three calls are CREDENTIALED, like
 * `startErpOAuth`: the pending connect is keyed on the same HttpOnly nonce
 * cookie, so only the browser that started the connect can read or finish it.
 */
export const getErpPendingConnect = () =>
	api.get<ErpPendingConnect>(`${BASE}/oauth/pending`, { credentials: 'include' });

export const selectErpCompany = (companyId: string) =>
	api.post<{ connected: boolean; provider: string; external_tenant_id: string }>(
		`${BASE}/oauth/pending/select`,
		{ company_id: companyId },
		undefined,
		{ credentials: 'include' }
	);

export const discardErpPendingConnect = () =>
	api.delete<{ discarded: boolean }>(`${BASE}/oauth/pending`, undefined, {
		credentials: 'include'
	});

export const disconnectErpOAuth = () => api.post<unknown>(`${BASE}/oauth/disconnect`, {});

export const testErpConnection = (erp: Record<string, unknown>) =>
	api.post<ErpTestResult>('/api/organization/test-erp', erp);
