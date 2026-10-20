// Typed helpers for the chart-of-accounts endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce), so every call here is tenant- and entity-scoped by the client
// the same way the backend scopes it.
import { api } from '#lib/api.ts';
import type { GlAccount } from '#lib/types/glAccount.ts';

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
	/**
	 * Ask for ONE entity's chart — the shared accounts plus that entity's own —
	 * whatever the sidebar has selected. See {@link listInvoiceChart}.
	 */
	chart_entity_id?: string;
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
	if (params.chart_entity_id) qs.set('chart_entity_id', params.chart_entity_id);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<GlAccount[]>(`/api/gl-accounts${suffix}`);
}

/**
 * The chart an invoice filed under `entityId` may be coded against — the
 * codes the backend will accept on every invoice GL write
 * (`backend/app/services/gl_chart.py`), and so the only ones the two invoice
 * pickers offer.
 *
 * An invoice's GL code resolves in the chart of the entity the INVOICE belongs
 * to, which is not the sidebar's view: the consolidated view returns every
 * subsidiary's chart at once (so subsidiary B's `6000` used to be offered for
 * a subsidiary-A invoice), and a deep link can open another entity's invoice
 * while one is selected. So the chart is asked for by entity, not by header.
 *
 * `null` is an invoice no entity was ever stamped on, which resolves against
 * the shared chart alone — the list is fetched in whatever view is current
 * and narrowed to its shared rows, since `chart_entity_id` names an entity.
 */
export async function listInvoiceChart(entityId: string | null): Promise<GlAccount[]> {
	if (entityId) return listGlAccounts({ chart_entity_id: entityId });
	return (await listGlAccounts()).filter((a) => !a.entity_id);
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

/**
 * The fields `PATCH /api/gl-accounts/{id}` accepts. `code` and `entity_id` are
 * deliberately absent — the backend refuses to change either (an invoice
 * records the code as TEXT, and the chart a row sits in is its meaning); a
 * move between charts is a create plus a retire. Send only what changed:
 * unset fields are left alone.
 */
export interface GlAccountUpdate {
	name?: string;
	account_type?: string | null;
	parent_code?: string | null;
	/** `false` retires the account (there is no DELETE); `true` reactivates it. */
	is_active?: boolean;
}

/**
 * `PATCH /api/gl-accounts/{id}` — admin / ap_manager. Correct or retire one
 * account; returns the row in the list's shape.
 *
 * With an entity selected only that entity's OWN rows are editable — a shared
 * row belongs to every entity, so the backend 403s and names the fix (switch to
 * the consolidated view). `/gl-accounts` mirrors that rule before offering the
 * actions (`types/glAccount.ts::canEditGlAccount`), but the server stays the
 * authority.
 */
export function updateGlAccount(id: string, body: GlAccountUpdate): Promise<GlAccount> {
	return api.patch<GlAccount>(`/api/gl-accounts/${id}`, body);
}

/** One ERP code the sync refused to link (`services/gl_account_sync`). */
export interface GlAccountSyncConflict {
	code: string;
	reason:
		| 'duplicate_code'
		| 'duplicate_erp_id'
		| 'code_taken'
		| 'ambiguous_legacy'
		| 'code_too_long'
		| 'missing_erp_id';
	erp_account_ids: string[];
}

export interface GlAccountSyncResult {
	success: boolean;
	message: string;
	created: number;
	updated: number;
	unchanged: number;
	linked: number;
	conflict_count: number;
	/** Capped at 50; `conflict_count` is exact. */
	conflicts: GlAccountSyncConflict[];
	unlinked_count: number;
	/** Codes of rows whose ERP link the sync removed. Capped at 50. */
	unlinked: string[];
	code_drift_count: number;
	code_drift: { code: string; erp_code: string }[];
	/** Where each row's bills posted before and after; `to` null = unlinked. */
	link_changes: { code: string; from: string | null; to: string | null }[];
	/** Pre-0115 links this pull confirmed and stamped with the connection. */
	realm_adopted?: number;
	deactivated_count?: number;
	/** Codes retired because the ERP made their account inactive. Capped at 50. */
	deactivated?: string[];
	parent_code_dropped_count?: number;
	/** Codes synced without their parent (its code did not fit). Capped at 50. */
	parent_code_dropped?: string[];
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

/**
 * `POST /api/gl-accounts/{id}/erp-link` — admin / ap_manager. Say which of the
 * row's `erp_link_candidates` it means. The backend accepts only an id the
 * last sync offered, under the connection it offered it from, links the row,
 * marks the link confirmed so later syncs do not re-judge it, and audits the
 * choice. Returns the row in the list's shape.
 */
export function confirmGlAccountErpLink(id: string, erpAccountId: string): Promise<GlAccount> {
	return api.post<GlAccount>(`/api/gl-accounts/${id}/erp-link`, { erp_account_id: erpAccountId });
}
