/**
 * Pure helpers for the CFO page's forecast-vs-actual panel
 * (`POST /api/analytics/forecast_variance`).
 *
 * Lives beside the route, like `budgetRollupSummary.ts` and
 * `openingBalanceNotice.ts`, and is unit-tested under the plain-Node vitest
 * config — no `$state`, no `fetch`, no browser globals.
 *
 * **No money arithmetic here.** The backend owns every subtraction: `variance`
 * is `actual − forecast` in `Decimal`, `variance_pct` is its percentage. This
 * module only decides what a figure MEANS — whether a percentage is computable
 * at all, and how many months came back partial — and folds counts, which are
 * denominated in nothing.
 */

import type { ForecastVariance, ForecastVarianceInput, ForecastVarianceRow } from '#lib/types/analytics.ts';
import { isNegativeAmount, isPositiveAmount } from '#lib/utils/money.ts';
import { normalizeMoneyInput } from '#lib/utils/moneyInput.ts';

/** One row of the entry form, exactly as typed. `forecast` is RAW text. */
export interface ForecastEntry {
	month: string;
	forecast: string;
}

/**
 * The outcome of reading the entry form.
 *
 * Deliberately a discriminated refusal rather than a best-effort payload: a
 * forecast is the number a variance is measured against, so an amount we
 * cannot read is REFUSED — never repaired, never coerced to `0` (which would
 * make `variance` equal the whole actual outflow and `variance_pct` a
 * fabricated 0%).
 */
export type ForecastEntriesResult =
	| { ok: true; rows: ForecastVarianceInput[] }
	| { ok: false; reason: 'empty' | 'month' | 'amount' };

/** A calendar month the API accepts: `YYYY-MM`, months 01–12. */
const MONTH_INPUT = /^\d{4}-(0[1-9]|1[0-2])$/;

/**
 * Read the typed rows into the request body, or say why it can't be sent.
 *
 * A row left completely blank is dropped — that is an unused slot in the
 * editor, not an assertion. A row carrying only one half is an incomplete
 * assertion and refuses the whole submit, because silently dropping it would
 * quietly report a variance for fewer months than the CFO typed.
 *
 * `normalizeMoneyInput` decides the amount's shape with a regex, never
 * `Number` — the string that goes out is the string that came in
 * (`utils/moneyInput.ts`).
 */
export function collectForecastEntries(entries: ForecastEntry[]): ForecastEntriesResult {
	const rows: ForecastVarianceInput[] = [];
	for (const entry of entries) {
		const month = (entry.month ?? '').trim();
		const typed = (entry.forecast ?? '').trim();
		if (!month && !typed) continue;
		if (!MONTH_INPUT.test(month)) return { ok: false, reason: 'month' };
		const forecast = normalizeMoneyInput(typed);
		if (forecast === null) return { ok: false, reason: 'amount' };
		rows.push({ month, forecast });
	}
	if (rows.length === 0) return { ok: false, reason: 'empty' };
	return { ok: true, rows };
}

/**
 * Total completed payments left OUT of the actuals, across every month.
 *
 * A COUNT, which is why it may be folded across months when the amounts beside
 * it may not — the same reasoning as `budgetRollupSummary.overBudgetCount`.
 * Non-zero means every `actual` (and therefore every variance) below is a
 * FLOOR: a payment that could not be expressed in the reporting currency is
 * excluded rather than added at face value (`docs/decisions.md` §35).
 */
export function unconvertedTotal(result: ForecastVariance | null): number {
	if (!result) return 0;
	return result.rows.reduce((n, row) => n + (row.unconverted_count ?? 0), 0);
}

/**
 * Render a row's variance percentage, or `null` when there isn't one.
 *
 * The backend emits `0` for `variance_pct` whenever the forecast is not
 * positive, because a percentage OF ZERO is not a number — but `0%` on a CFO's
 * screen reads as "we landed exactly on plan", the most reassuring statement
 * available, over the one row carrying no information at all. Same rule as the
 * fraud-rate trend's `insufficient_data` and the budget rollup's `null`
 * utilization (`docs/decisions.md` §34): the caller renders its own
 * not-applicable state.
 */
export function variancePctLabel(row: ForecastVarianceRow): string | null {
	if (!isPositiveAmount(row.forecast)) return null;
	if (!Number.isFinite(row.variance_pct)) return null;
	return `${row.variance_pct > 0 ? '+' : ''}${row.variance_pct}%`;
}

/**
 * Which way a month went, for tinting only — never for a figure.
 *
 * `over` = we paid out MORE than forecast (the backend's positive variance),
 * `under` = came in below plan, `level` = exactly on plan or not readable.
 * The predicate reads the backend's own subtraction; it never performs one.
 */
export function varianceTone(row: ForecastVarianceRow): 'over' | 'under' | 'level' {
	if (isPositiveAmount(row.variance)) return 'over';
	if (isNegativeAmount(row.variance)) return 'under';
	return 'level';
}
