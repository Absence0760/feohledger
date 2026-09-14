// Typed helpers for the chart-of-accounts endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce), so every call here is tenant- and entity-scoped by the client
// the same way the backend scopes it.
import { api } from '$lib/api';
import type { GlAccount } from '$lib/types/glAccount';

export interface GlAccountListParams {
	/** Server-side ILIKE over code + name. Never filter the loaded rows instead. */
	search?: string;
	account_type?: string;
	/**
	 * Defaults to `true` on the backend, so an inactive account is hidden
	 * unless this is explicitly `false`. The list page has to send it
	 * either way — omitting it is not the same as `false`.
	 */
	active_only?: boolean;
}

/**
 * The whole chart, as a bare array — `GET /api/gl-accounts` deliberately does
 * NOT take the paginated `{items, total, …}` envelope (see the endpoint's own
 * docstring and `backend/tests/test_pagination.py::test_gl_accounts_stays_unpaginated`).
 * Filters are applied server-side, so the returned rows are the whole answer
 * for the filters passed, not one page of it.
 */
export function listGlAccounts(params: GlAccountListParams = {}): Promise<GlAccount[]> {
	const qs = new URLSearchParams();
	if (params.search) qs.set('search', params.search);
	if (params.account_type) qs.set('account_type', params.account_type);
	if (params.active_only !== undefined) qs.set('active_only', String(params.active_only));
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<GlAccount[]>(`/api/gl-accounts${suffix}`);
}

export interface GlAccountCreate {
	code: string;
	name: string;
	account_type?: string | null;
	parent_code?: string | null;
}

/**
 * `POST /api/gl-accounts` — admin / ap_manager.
 *
 * Which chart the new account lands in is decided by the CURRENT entity
 * selection, not by this body: consolidated → a shared account (NULL
 * `entity_id`, visible to every entity), an entity selected → that entity's
 * own. The backend reads it off the `X-Entity-ID` header the api client
 * attaches, so any caller must tell the user which of the two they are about
 * to do. Returns only `{id, code, name}`.
 */
export function createGlAccount(
	body: GlAccountCreate
): Promise<Pick<GlAccount, 'id' | 'code' | 'name'>> {
	return api.post<Pick<GlAccount, 'id' | 'code' | 'name'>>('/api/gl-accounts', body);
}

export interface GlAccountSyncResult {
	success: boolean;
	message: string;
	created: number;
	updated: number;
	adapter: string;
}

/**
 * `POST /api/gl-accounts/sync-erp` — admin / ap_manager. Pulls the chart from
 * the connected ERP adapter into the same chart `createGlAccount` would write
 * to (shared when consolidated, the selected entity's own otherwise).
 */
export function syncGlAccountsFromErp(): Promise<GlAccountSyncResult> {
	return api.post<GlAccountSyncResult>('/api/gl-accounts/sync-erp', {});
}
