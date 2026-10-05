// Types for the Procurement → Budgets surface. Mirrors the JSON returned by the
// `/api/budgets` endpoints (backend `BudgetResponse` / `BudgetSpendResponse` /
// `BudgetCheckResponse`). Date/datetime fields are ISO strings.
//
// **Money fields are `MoneyAmount`, never `number`.** The backend serialises
// these rollups as JSON numbers (`schemas/budget.py` does `float(...)` on the
// way out), so `MoneyString` would be a different lie — but a `number` field
// invites `a - b`, `Math.max()` and `.toFixed()` on currency, which is the
// arithmetic the Decimal invariant exists to prevent. `MoneyAmount` is honest
// about the wire shape AND makes that arithmetic a type error; where a figure
// legitimately has to become a number, `parseMoneyForLayout` (geometry) and
// `isPositiveAmount` / `isNegativeAmount` (predicates) are the only routes.
// See `frontend/CLAUDE.md` § Money formatting.
//
// The `total` on the list/summary envelopes is deliberately still `number`:
// it is a ROW COUNT, not an allocation. The per-currency allocation totals live
// on `BudgetSummary.by_currency`.

import type { MoneyAmount, MoneyString } from '#lib/utils/money.ts';

export type BudgetDimension = 'department' | 'project' | 'cost_center' | 'gl_account';

export const BUDGET_DIMENSIONS: BudgetDimension[] = [
	'department',
	'project',
	'cost_center',
	'gl_account'
];

export const BUDGET_DIMENSION_LABELS: Record<BudgetDimension, string> = {
	department: 'Department',
	project: 'Project',
	cost_center: 'Cost Center',
	gl_account: 'GL Account'
};

export interface Budget {
	id: string;
	name: string;
	dimension: string;
	dimension_value: string;
	period: string | null;
	period_start: string | null;
	period_end: string | null;
	/** The allocation, denominated in `currency`. */
	amount: MoneyAmount;
	currency: string;
	notes: string | null;
	created_at: string;
	updated_at: string;
}

export interface BudgetListResponse {
	items: Budget[];
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	page: number;
	page_size: number;
}

// `GET /api/budgets/{id}/spend` — computed on read from requisitions / POs /
// invoices. `committed` = open requisitions + their converted POs; `actual` =
// realised invoice spend matched to the dimension; `remaining` =
// allocated - committed - actual (negative = overspend).
//
// The subtraction that produces `remaining` is the backend's, in Decimal.
// Never re-derive it client-side from the other three: read `remaining`, and
// use `isNegativeAmount` to decide whether it renders as an overspend.
export interface BudgetSpend {
	budget_id: string;
	name: string;
	dimension: string;
	dimension_value: string;
	currency: string;
	allocated: MoneyAmount;
	committed: MoneyAmount;
	actual: MoneyAmount;
	remaining: MoneyAmount;
	/** A percentage, not money. */
	utilization_pct: number;
	/**
	 * A COUNT, not money: requisitions / POs / invoices that match this budget
	 * but are denominated in another currency, so the spend legs refused them
	 * rather than adding unlike face values. Non-zero means `committed` /
	 * `actual` are a FLOOR — render the disclosure beside the figure.
	 */
	excluded_row_count: number;
}

/** One currency's slice of `GET /api/budgets/summary`. */
export interface BudgetCurrencyTotal {
	currency: string;
	/** Exact decimal string — never parse into a float for arithmetic. */
	total: MoneyString;
	count: number;
}

/**
 * Whole-set KPI rollup from `GET /api/budgets/summary`, over the SAME
 * dimension/period/search filters the list ran with.
 *
 * The page's `totalAllocated` used to reduce over the LOADED page and add
 * across currencies into the org default — so it contradicted the whole-set
 * row count beside it and rendered EUR + USD as one figure. `by_currency` is
 * grouped, never summed (see `#lib/utils/currencyGroups`).
 */
export interface BudgetSummary {
	/** Row count of the whole filtered set — NOT money. */
	total: number;
	by_currency: BudgetCurrencyTotal[];
}

// `GET /api/budgets/check?budget_id=&amount=` — overspend pre-check for the
// requisition flow. `would_overspend` is the backend's verdict; a caller must
// read that flag rather than comparing two of these amounts itself.
export interface BudgetCheck {
	budget_id: string;
	amount: MoneyAmount;
	allocated: MoneyAmount;
	committed: MoneyAmount;
	actual: MoneyAmount;
	remaining: MoneyAmount;
	remaining_after: MoneyAmount;
	would_overspend: boolean;
	currency: string;
}

// Request shapes. Money goes out as the EXACT DECIMAL STRING the user typed
// (`utils/moneyInput.ts::normalizeMoneyInput`), never a JSON number: the
// backend's `json.loads` decodes the body before any validator runs, so a
// fractional JSON number is already a float by the time pydantic sees it and
// no `Decimal` annotation can undo the rounding. Pydantic parses the string
// form exactly.
export interface BudgetCreate {
	name: string;
	dimension: BudgetDimension;
	dimension_value: string;
	period: string | null;
	period_start: string | null;
	period_end: string | null;
	amount: MoneyString;
	currency: string;
	notes: string | null;
}

export type BudgetUpdate = Partial<BudgetCreate>;

// `GET /api/budgets/rollup` — the org-wide, whole-set counterpart of
// `/{id}/spend`: allocated vs committed vs actual across every budget matching
// the SAME dimension/period/search filters as the list, grouped by currency.
//
// Money here is `MoneyString` — an EXACT decimal string, not a JSON number.
// The per-budget `BudgetSpend` above predates that convention and still
// arrives as `float`; these are org-wide totals a CFO reads off a dashboard, so
// they never round-trip through a binary float. Never add two rows together:
// they are denominated in different currencies and nothing converts them.

/** One currency's slice of the org-wide budget-vs-actual rollup. */
export interface BudgetCurrencyRollup {
	currency: string;
	/** A row count, not money. */
	budget_count: number;
	allocated: MoneyString;
	committed: MoneyString;
	actual: MoneyString;
	/** `allocated - committed - actual`; negative = overspend. */
	remaining: MoneyString;
	/**
	 * A percentage, not money — and `null`, never `'0.00'`, when this currency
	 * allocates nothing at all. "0% used" and "there is nothing to use" are
	 * opposite facts and 0% renders as the reassuring one.
	 */
	utilization_pct: string | null;
	/** Budgets in this currency whose `remaining` went negative. A count. */
	over_budget_count: number;
	/** A count — see `BudgetSpend.excluded_row_count`. */
	excluded_row_count: number;
}

export interface BudgetRollup {
	/** A row count, not money. */
	budget_count: number;
	by_currency: BudgetCurrencyRollup[];
	/**
	 * Whole-set total of the per-currency disclosures. Non-zero means the
	 * figures above are a floor, and the surface must SAY so beside them rather
	 * than presenting a partial total as a whole one.
	 */
	excluded_row_count: number;
	/** No budgets at all — a distinct state from a row of confident zeros. */
	insufficient_data: boolean;
}
