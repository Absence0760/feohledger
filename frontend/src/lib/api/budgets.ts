// Typed helpers for the procurement budgets endpoints. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce). Mirrors the pattern of `src/lib/api/expenses.ts`.
import { api } from '#lib/api.ts';
import type {
	Budget,
	BudgetCheck,
	BudgetCreate,
	BudgetListResponse,
	BudgetRollup,
	BudgetSpend,
	BudgetSummary,
	BudgetUpdate
} from '#lib/types/budget.ts';
import type { MoneyString } from '#lib/utils/money.ts';

export interface BudgetListParams {
	dimension?: string;
	period?: string;
	search?: string;
	page?: number;
	page_size?: number;
}

export function listBudgets(params: BudgetListParams = {}): Promise<BudgetListResponse> {
	const qs = new URLSearchParams();
	if (params.dimension) qs.set('dimension', params.dimension);
	if (params.period) qs.set('period', params.period);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<BudgetListResponse>(`/api/budgets?${qs}`);
}

// Whole-set KPI rollup — count + per-currency allocation totals over the SAME
// dimension/period/search filters as `listBudgets`, so the KPI row can't
// contradict the table beneath it.
export function getBudgetSummary(
	params: Pick<BudgetListParams, 'dimension' | 'period' | 'search'> = {}
): Promise<BudgetSummary> {
	const qs = new URLSearchParams();
	if (params.dimension) qs.set('dimension', params.dimension);
	if (params.period) qs.set('period', params.period);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<BudgetSummary>(`/api/budgets/summary${suffix}`);
}

// Org-wide budget-vs-actual — allocated / committed / actual / remaining per
// currency across the WHOLE filtered set, computed on read. Same filters as
// `listBudgets`, so the CFO rollup and the budgets table can't describe
// different sets. Never add two `by_currency` rows together.
export function getBudgetRollup(
	params: Pick<BudgetListParams, 'dimension' | 'period' | 'search'> = {}
): Promise<BudgetRollup> {
	const qs = new URLSearchParams();
	if (params.dimension) qs.set('dimension', params.dimension);
	if (params.period) qs.set('period', params.period);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<BudgetRollup>(`/api/budgets/rollup${suffix}`);
}

export function getBudget(id: string): Promise<Budget> {
	return api.get<Budget>(`/api/budgets/${id}`);
}

export function createBudget(body: BudgetCreate): Promise<Budget> {
	return api.post<Budget>('/api/budgets', body);
}

export function updateBudget(id: string, body: BudgetUpdate): Promise<Budget> {
	return api.patch<Budget>(`/api/budgets/${id}`, body);
}

export function deleteBudget(id: string): Promise<void> {
	return api.delete(`/api/budgets/${id}`);
}

// Computed spend rollup for one budget (allocated / committed / actual /
// remaining / utilization). Computed on read by the backend.
export function getBudgetSpend(id: string): Promise<BudgetSpend> {
	return api.get<BudgetSpend>(`/api/budgets/${id}/spend`);
}

// Overspend pre-check: would committing `amount` against this budget exceed it?
// `amount` is the exact decimal STRING the caller wants to commit — it rides a
// query string, so a `number` here would only ever be re-stringified anyway,
// and typing it that way invites a float hop on the way in.
export function checkBudget(budgetId: string, amount: MoneyString): Promise<BudgetCheck> {
	const qs = new URLSearchParams({ budget_id: budgetId, amount });
	return api.get<BudgetCheck>(`/api/budgets/check?${qs}`);
}
