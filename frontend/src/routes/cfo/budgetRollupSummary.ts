/**
 * Pure display helpers for the CFO page's org-wide budget-vs-actual rollup
 * (`GET /api/budgets/rollup`).
 *
 * Lives beside the route, like `openingBalanceNotice.ts`, and is unit-tested
 * under the plain-Node vitest config — no `$state`, no `fetch`, no browser
 * globals, and deliberately no money arithmetic: the backend owns every total
 * and every subtraction (`remaining`), in `Decimal`.
 */

import type { BudgetRollup } from '#lib/types/budget.ts';

/**
 * How many budgets across the whole rollup have gone over their allocation.
 *
 * A COUNT of rows, not money — which is exactly why it may be added across
 * currencies when the amounts beside it may not. Folding the per-currency
 * `over_budget_count`s is the only cross-currency sum this surface performs,
 * and it is safe because a budget count is denominated in nothing.
 */
export function overBudgetCount(rollup: BudgetRollup | null): number {
	if (!rollup) return 0;
	return rollup.by_currency.reduce((n, row) => n + row.over_budget_count, 0);
}

/**
 * Render a per-currency utilization figure, or `null` when there isn't one.
 *
 * `null` in, `null` out — the backend sends `null`, never `"0.00"`, when a
 * currency allocates nothing at all, because "0% of the budget is used" and
 * "there is no budget to use" are opposite facts and 0% reads as the
 * reassuring one (`docs/decisions.md` §34). The caller renders its own
 * not-applicable state rather than a percentage.
 *
 * A blank or non-numeric string is also refused: a provider figure that can't
 * be read must not become `NaN%` on a CFO's dashboard.
 */
export function formatUtilization(pct: string | null | undefined): string | null {
	if (pct === null || pct === undefined) return null;
	const trimmed = pct.trim();
	if (!trimmed || !Number.isFinite(Number(trimmed))) return null;
	return `${trimmed}%`;
}
