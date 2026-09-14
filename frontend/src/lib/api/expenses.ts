// Typed helpers for the expense + expense-report endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce). Mirrors the pattern of `src/lib/api/contracts.ts`.
import { api, formatApiDetail } from '$lib/api';
import { getApiBase, getTenantSlug } from '$lib/tenant';
import { getSelectedEntityId } from '$lib/entity';
import type { MatchingIdsResponse } from '$lib/utils/pagination';
import type {
	Expense,
	ExpenseCreate,
	ExpenseListResponse,
	ExpenseSummary,
	ExpenseReport,
	ExpenseReportCreate,
	ExpenseReportListResponse,
	ExpenseReportSummary,
	ExpensePolicy,
	ExpensePolicyCreate,
	ExpensePreapproval,
	ExpensePreapprovalCreate,
	PolicyViolation,
	CorporateCardTransaction,
	CardTransactionListResponse,
	CardMatchSuggestion,
	CardImportResult,
	SyncVirtualCardsResult
} from '$lib/types/expense';

/**
 * Query shape shared by `listExpenses` and `exportExpensesCsv` — but the two
 * endpoints do NOT accept the same params, and FastAPI silently drops the ones
 * it doesn't declare:
 *
 *   - `GET /api/expenses`        — `status`, `report_id`, `search`, `sort`,
 *                                  `order`, `page`, `page_size`.
 *   - `GET /api/expenses/export` — `status`, `report_id`, `search`, `category`,
 *                                  `date_from`, `date_to` (no pagination).
 *
 * The three filters a user can set from the list surface — `status`,
 * `report_id`, `search` — are on BOTH, and the export runs them through the
 * same `_expense_list_filters` builder the list and the KPI rollup use, so
 * "export what I'm looking at" means the rows on screen. `category` /
 * `date_from` / `date_to` remain **list-ignored** (export-only period
 * slicing), and `sort` / `order` remain ignored by `/export`, `/summary` and
 * `/ids`. Sending a param to an endpoint that doesn't declare it is a silent
 * no-op, not a filter — wire the backend leg first.
 */
export interface ExpenseListParams {
	status?: string;
	/** Export only — ignored by `GET /api/expenses`. */
	category?: string;
	search?: string;
	/** Export only — ignored by `GET /api/expenses`. */
	date_from?: string;
	/** Export only — ignored by `GET /api/expenses`. */
	date_to?: string;
	report_id?: string;
	/** List only — ignored by `/export`, `/summary`, and `/ids`: none of the
	 *  three declares a `sort` leg, so it's a silent no-op on them. */
	sort?: string;
	order?: 'asc' | 'desc';
	page?: number;
	page_size?: number;
}

/** Paginated list envelope returned by every list endpoint
 *  (`{items, total, page, page_size}` — see frontend/CLAUDE.md § Pagination).
 *  The policy + pre-approval tabs don't paginate, so their helpers unwrap to
 *  `.items`; expenses/reports/cards keep the full envelope for Load-More. */
interface Paginated<T> {
	items: T[];
	total: number;
	page: number;
	page_size: number;
}

// The GL picker's option type + loader are owned by `api/glAccounts.ts` /
// `types/glAccount.ts` (the chart-of-accounts surface), re-exported here so the
// expense / requisition pages keep one import.
export type { GlAccountOption } from '$lib/types/glAccount';

function expenseQuery(params: ExpenseListParams): URLSearchParams {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.category) qs.set('category', params.category);
	if (params.search) qs.set('search', params.search);
	if (params.date_from) qs.set('date_from', params.date_from);
	if (params.date_to) qs.set('date_to', params.date_to);
	if (params.report_id) qs.set('report_id', params.report_id);
	if (params.sort) qs.set('sort', params.sort);
	if (params.order) qs.set('order', params.order);
	return qs;
}

export function listExpenses(params: ExpenseListParams = {}): Promise<ExpenseListResponse> {
	const qs = expenseQuery(params);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<ExpenseListResponse>(`/api/expenses?${qs}`);
}

/**
 * Whole-set rollup for the KPI row — `GET /api/expenses/summary`.
 *
 * Takes the same params as `listExpenses` (minus pagination, which it has none
 * of) and the backend runs them through the same filter builder, so the cards
 * and the table always describe one set. Deriving the KPIs from
 * `listExpenses().items` instead described only the loaded page.
 */
export function getExpenseSummary(params: ExpenseListParams = {}): Promise<ExpenseSummary> {
	const qs = expenseQuery(params);
	const query = qs.toString();
	return api.get<ExpenseSummary>(`/api/expenses/summary${query ? `?${query}` : ''}`);
}

/**
 * Every expense id matching the current list filters — `GET /api/expenses/ids`.
 *
 * Backs the "select all N matching" affordance on the expenses list page:
 * the header checkbox only ever selects the currently-LOADED page
 * (`expenseStore.all`), so a bulk action over "select all" silently skipped
 * every row past it. Same params as `listExpenses` (minus pagination), so
 * the resolved id set always matches what the table is showing.
 */
export function getExpenseIds(params: ExpenseListParams = {}): Promise<MatchingIdsResponse> {
	const qs = expenseQuery(params);
	const query = qs.toString();
	return api.get<MatchingIdsResponse>(`/api/expenses/ids${query ? `?${query}` : ''}`);
}

export function getExpense(id: string): Promise<Expense> {
	return api.get<Expense>(`/api/expenses/${id}`);
}

export function createExpense(body: ExpenseCreate): Promise<Expense> {
	return api.post<Expense>('/api/expenses', body);
}

export function updateExpense(id: string, body: Partial<ExpenseCreate>): Promise<Expense> {
	return api.patch<Expense>(`/api/expenses/${id}`, body);
}

export function deleteExpense(id: string): Promise<void> {
	return api.delete(`/api/expenses/${id}`);
}

export function uploadReceipt(id: string, file: File): Promise<Expense> {
	return api.upload<Expense>(`/api/expenses/${id}/receipt`, file);
}

// Bytes for an expense receipt. `<img>`/`<a download>` can't reach the endpoint
// directly (no Bearer header), so fetch through the auth'd client and render
// via a blob URL. Caller revokes the returned URL.
export function receiptUrl(fileKey: string): Promise<string> {
	return api.fetchBlob(`/api/expenses/receipt/${fileKey}`);
}

// --- Reports ---

export function listExpenseReports(
	params: { status?: string; page?: number; page_size?: number } = {}
): Promise<ExpenseReportListResponse> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<ExpenseReportListResponse>(`/api/expense-reports?${qs}`);
}

export function getExpenseReport(id: string): Promise<ExpenseReport> {
	return api.get<ExpenseReport>(`/api/expense-reports/${id}`);
}

export function createExpenseReport(body: ExpenseReportCreate): Promise<ExpenseReport> {
	return api.post<ExpenseReport>('/api/expense-reports', body);
}

export function updateExpenseReport(
	id: string,
	body: { report_number?: string; title?: string | null; currency?: string; notes?: string | null }
): Promise<ExpenseReport> {
	return api.patch<ExpenseReport>(`/api/expense-reports/${id}`, body);
}

export function attachExpenses(
	reportId: string,
	expenseIds: string[],
	detach = false
): Promise<ExpenseReport> {
	return api.post<ExpenseReport>(`/api/expense-reports/${reportId}/expenses`, {
		expense_ids: expenseIds,
		detach
	});
}

export function expenseReportSummary(id: string): Promise<ExpenseReportSummary> {
	return api.get<ExpenseReportSummary>(`/api/expense-reports/${id}/summary`);
}

// --- Bulk GL code (WF2) ---

/** One id `bulk-gl-code` couldn't apply the GL code to, and why (invalid id
 *  shape or not found in this entity scope) — mirrors the invoice bulk
 *  endpoints' `{updated, skipped}` partial-success contract. */
export interface ExpenseBulkGlCodeSkip {
	id: string;
	reason: string;
}

export interface ExpenseBulkGlCodeResponse {
	updated: number;
	skipped: ExpenseBulkGlCodeSkip[];
}

export function bulkGlCode(
	expenseIds: string[],
	glAccountId: string | null
): Promise<ExpenseBulkGlCodeResponse> {
	return api.post<ExpenseBulkGlCodeResponse>('/api/expenses/bulk-gl-code', {
		expense_ids: expenseIds,
		gl_account_id: glAccountId
	});
}

// --- GL accounts (reused from the existing chart-of-accounts endpoint) ---

export { listGlAccounts } from '$lib/api/glAccounts';

// --- CSV export ---

function triggerDownload(blob: Blob, filename: string) {
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	a.click();
	URL.revokeObjectURL(url);
}

/** Streamed `text/csv` expense register; the backend stamps the same filename
 *  via Content-Disposition. Goes through `downloadBlob` so the Bearer + tenant
 *  + entity headers ride along (a bare `<a href>` can't carry the JWT). */
export async function exportExpensesCsv(params: ExpenseListParams = {}): Promise<void> {
	const qs = expenseQuery(params);
	const blob = await api.downloadBlob(`/api/expenses/export?${qs}`);
	const today = new Date().toISOString().slice(0, 10);
	triggerDownload(blob, `expenses_${today}.csv`);
}

// =========================== WF3: Policies ===========================

export async function listPolicies(): Promise<ExpensePolicy[]> {
	const res = await api.get<Paginated<ExpensePolicy>>('/api/expense-policies');
	return res.items;
}

export function createPolicy(body: ExpensePolicyCreate): Promise<ExpensePolicy> {
	return api.post<ExpensePolicy>('/api/expense-policies', body);
}

export function updatePolicy(
	id: string,
	body: Partial<ExpensePolicyCreate>
): Promise<ExpensePolicy> {
	return api.patch<ExpensePolicy>(`/api/expense-policies/${id}`, body);
}

export function deletePolicy(id: string): Promise<void> {
	return api.delete(`/api/expense-policies/${id}`);
}

// ========================= WF3: Pre-approvals =========================

export async function listPreapprovals(
	params: { status?: string; requester_user_id?: string } = {}
): Promise<ExpensePreapproval[]> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.requester_user_id) qs.set('requester_user_id', params.requester_user_id);
	const res = await api.get<Paginated<ExpensePreapproval>>(`/api/expense-preapprovals?${qs}`);
	return res.items;
}

export function createPreapproval(body: ExpensePreapprovalCreate): Promise<ExpensePreapproval> {
	return api.post<ExpensePreapproval>('/api/expense-preapprovals', body);
}

export function getPreapproval(id: string): Promise<ExpensePreapproval> {
	return api.get<ExpensePreapproval>(`/api/expense-preapprovals/${id}`);
}

export function approvePreapproval(id: string): Promise<ExpensePreapproval> {
	return api.post<ExpensePreapproval>(`/api/expense-preapprovals/${id}/approve`, {});
}

export function rejectPreapproval(id: string, reason?: string): Promise<ExpensePreapproval> {
	return api.post<ExpensePreapproval>(`/api/expense-preapprovals/${id}/reject`, {
		reason: reason ?? null
	});
}

// ===================== WF3: Report transitions =====================

/**
 * Discriminated result for the report `submit` action. The submit route can
 * 422 with a structured violation list (blocking policy violations) — the
 * shared `api` client flattens every non-OK body into a single `Error` message
 * (readably, via `formatApiDetail`, but still a string), losing the STRUCTURE of
 * a list-shaped `detail`. So submit hand-rolls its own `fetch` (the
 * `api.ts::downloadBlob` private-fetch idiom: Bearer + tenant + entity headers)
 * to read the raw 422 JSON and surface the violations to the caller intact.
 */
export type SubmitReportResult =
	| { ok: true; report: ExpenseReport }
	| { ok: false; violations: PolicyViolation[]; message: string };

/** Normalise whatever the backend put in the 422 body into a violation list.
 *  Supports `detail` = list-of-violations OR `detail` = {message, violations}
 *  OR a plain string `detail`. */
function parseSubmitViolations(body: unknown): { violations: PolicyViolation[]; message: string } {
	const detail = (body as { detail?: unknown } | null)?.detail;
	if (Array.isArray(detail)) {
		const violations = detail as PolicyViolation[];
		return { violations, message: violations.map((v) => v.message).join('; ') };
	}
	if (detail && typeof detail === 'object') {
		const obj = detail as { message?: string; violations?: PolicyViolation[] };
		const violations = Array.isArray(obj.violations) ? obj.violations : [];
		return {
			violations,
			message: obj.message ?? violations.map((v) => v.message).join('; ') ?? 'Submit blocked'
		};
	}
	if (typeof detail === 'string') {
		return { violations: [], message: detail };
	}
	return { violations: [], message: 'Submit blocked by policy.' };
}

export async function submitReport(id: string): Promise<SubmitReportResult> {
	const base = getApiBase();
	const token = typeof window !== 'undefined' ? localStorage.getItem('auth_token') : null;
	const headers: Record<string, string> = { 'Content-Type': 'application/json' };
	if (token) headers['Authorization'] = `Bearer ${token}`;
	const tenant = getTenantSlug();
	if (tenant) headers['X-Tenant-Slug'] = tenant;
	const entity = getSelectedEntityId();
	if (entity) headers['X-Entity-ID'] = entity;

	// Sanctioned api-layer exception (downloadBlob idiom): the shared api.ts
	// flattens a list-shaped 422 `detail` into one Error message, destroying the
	// policy-violation list the UI panel renders. We hand-roll the same Bearer +
	// X-Tenant-Slug + X-Entity-ID headers + 401-bounce so the raw 422 JSON
	// survives. Not a component; not a bypass.
	const res = await fetch(`${base}/api/expense-reports/${id}/submit`, { // noqa: raw-fetch-in-component
		method: 'POST',
		headers,
		body: JSON.stringify({})
	});

	if (res.status === 401) {
		// Mirror the api-client stale-session bounce.
		if (token) {
			localStorage.removeItem('auth_token');
			window.location.href = '/login';
		}
		throw new Error('Unauthorized');
	}

	if (res.status === 422) {
		const body = await res.json().catch(() => ({}));
		return { ok: false, ...parseSubmitViolations(body) };
	}

	if (!res.ok) {
		const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
		throw new Error(formatApiDetail(body.detail, `API error ${res.status}`));
	}

	const report = (await res.json()) as ExpenseReport;
	return { ok: true, report };
}

export function approveReport(id: string): Promise<ExpenseReport> {
	return api.post<ExpenseReport>(`/api/expense-reports/${id}/approve`, {});
}

export function rejectReport(id: string, reason: string): Promise<ExpenseReport> {
	return api.post<ExpenseReport>(`/api/expense-reports/${id}/reject`, { reason });
}

// ================= WF4: Corporate-card transactions =================

export interface CardTxnListParams {
	reconciliation_status?: string;
	virtual_card_id?: string;
	date_from?: string;
	date_to?: string;
	page?: number;
	page_size?: number;
}

export function listCardTransactions(
	params: CardTxnListParams = {}
): Promise<CardTransactionListResponse> {
	const qs = new URLSearchParams();
	if (params.reconciliation_status) qs.set('reconciliation_status', params.reconciliation_status);
	if (params.virtual_card_id) qs.set('virtual_card_id', params.virtual_card_id);
	if (params.date_from) qs.set('date_from', params.date_from);
	if (params.date_to) qs.set('date_to', params.date_to);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<CardTransactionListResponse>(`/api/corporate-card-transactions?${qs}`);
}

/** Multipart CSV import. Returns the shared `ImportResult` (`imported` +
 *  `skipped` dedupe count) so the page can Toast it. Uses `api.upload` (the
 *  FormData idiom — browser sets the multipart boundary). */
export function importCardCsv(file: File): Promise<CardImportResult> {
	return api.upload<CardImportResult>('/api/corporate-card-transactions/import-csv', file);
}

/** Pull this tenant's charged virtual cards into card-transaction rows.
 *  Idempotent — re-syncs skip already-imported cards (external_txn_id dedupe). */
export function syncVirtualCards(): Promise<SyncVirtualCardsResult> {
	return api.post<SyncVirtualCardsResult>(
		'/api/corporate-card-transactions/sync-virtual-cards',
		{}
	);
}

export function cardMatchSuggestions(id: string): Promise<CardMatchSuggestion[]> {
	return api.get<CardMatchSuggestion[]>(
		`/api/corporate-card-transactions/${id}/match-suggestions`
	);
}

export function matchCardTxn(id: string, expenseId: string): Promise<CorporateCardTransaction> {
	return api.post<CorporateCardTransaction>(`/api/corporate-card-transactions/${id}/match`, {
		expense_id: expenseId
	});
}

export function unmatchCardTxn(id: string): Promise<CorporateCardTransaction> {
	return api.post<CorporateCardTransaction>(`/api/corporate-card-transactions/${id}/unmatch`, {});
}

export function ignoreCardTxn(id: string): Promise<CorporateCardTransaction> {
	return api.post<CorporateCardTransaction>(`/api/corporate-card-transactions/${id}/ignore`, {});
}

/** Create a new Expense from the txn and match it (both sides linked). */
export function createExpenseFromCard(id: string): Promise<CorporateCardTransaction> {
	return api.post<CorporateCardTransaction>(
		`/api/corporate-card-transactions/${id}/create-expense`,
		{}
	);
}
