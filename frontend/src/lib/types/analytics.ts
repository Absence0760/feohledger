// Response shapes for the predictive cash-flow forecasting endpoints
// (backend/app/api/analytics.py). Hand-maintained — the project has no
// codegen, mirroring src/lib/types/invoice.ts.
//
// Money on the `/api/analytics/*` endpoints is `MoneyString` — an EXACT
// decimal string, never a JSON number — so no currency figure round-trips
// through a binary float.
//
// The `GET /api/dashboard` block at the bottom of this file is the exception,
// and the difference is the WIRE, not a preference: those schemas annotate
// their amounts `backend/app/schemas/money.py::MoneyAmount`, which serialises
// `Decimal` to a JSON *number*. Typing those `MoneyString` would be a
// different lie, so they are `MoneyAmount` (`string | number | null`) — honest
// about both shapes, and still enough to make raw arithmetic a type error.
// See `frontend/CLAUDE.md` § Money formatting.
//
// Either way: render with `<Money>` / `formatMoney`; the only sanctioned way
// to get a number out of one is `parseMoneyForLayout` (chart geometry +
// ordering only). A day count, a percentage and a row count are genuinely
// numbers and stay `number`.

import type { MoneyAmount, MoneyString } from '$lib/utils/money';

export type CashflowGranularity = 'day' | 'week' | 'month';

export interface CashflowForecastPeriod {
	period: string;
	period_start: string;
	period_end: string;
	scheduled_amount: MoneyString;
	committed_amount: MoneyString;
	pending_amount: MoneyString;
	discount_eligible_amount: MoneyString;
	count: number;
	/**
	 * A COUNT, not money: invoices included at face value because no exchange
	 * rate into the reporting currency could be established. Non-zero means the
	 * amounts beside it mix currencies.
	 */
	unconverted_count: number;
}

export interface CashflowForecast {
	granularity: CashflowGranularity;
	horizon_days: number;
	include_pending: boolean;
	generated_at: string;
	periods: CashflowForecastPeriod[];
	totals: {
		scheduled_amount: MoneyString;
		committed_amount: MoneyString;
		pending_amount: MoneyString;
		discount_eligible_amount: MoneyString;
		count: number;
		unconverted_count: number;
	};
}

export interface WhatIfScenario {
	scenario: 'early' | 'on_time' | 'late';
	total_outflow: MoneyString;
	total_discount_captured: MoneyString;
	/** A day count, not money. */
	weighted_avg_pay_date_days: number;
	/** A count, not money — see `CashflowForecastPeriod.unconverted_count`. */
	unconverted_count: number;
	periods: Array<{
		period: string;
		period_start: string;
		period_end: string;
		scheduled_amount: MoneyString;
		unconverted_count: number;
	}>;
}

export interface WhatIfScenarios {
	granularity: CashflowGranularity;
	horizon_days: number;
	grace_days: number;
	scenarios: {
		early: WhatIfScenario;
		on_time: WhatIfScenario;
		late: WhatIfScenario;
	};
}

export interface CashPositionPeriod {
	period: string;
	period_start: string | null;
	period_end: string | null;
	opening: MoneyString;
	outflow: MoneyString;
	inflow: MoneyString;
	closing: MoneyString;
	below_threshold: boolean;
	unconverted_count: number;
}

export interface CashPositionBreach {
	period: string;
	period_start: string | null;
	period_end: string | null;
	closing: MoneyString;
	shortfall: MoneyString;
}

export interface CashPosition {
	granularity: CashflowGranularity;
	horizon_days: number;
	opening_balance: MoneyString;
	// Provenance of the figure the curve starts from — the shared resolution
	// chain in `services/cashflow.py::resolve_opening_balance` (the same one the
	// cash-flow copilot uses, hence `explicit` rather than the endpoint's old
	// `query`). `provider` was already reachable before this union listed it.
	opening_balance_source: 'explicit' | 'provider' | 'settings' | 'none';
	// The reporting currency the whole curve is denominated in.
	opening_balance_currency: string;
	// Adapter name when a bank sync supplied the balance, else null.
	opening_balance_provider: string | null;
	// `'currency_mismatch'` when a live provider balance existed but was refused
	// because its account is in another currency than the org reports in — so a
	// fallback to `settings`/`none` isn't mistaken for "no bank is connected".
	opening_balance_provider_skipped: string | null;
	/**
	 * The OUTFLOW-side twin of `opening_balance_provider_skipped`: how many
	 * commitments entered the curve at face value because no exchange rate into
	 * the reporting currency could be established. A COUNT, not money.
	 * Non-zero means every `closing` below mixes currencies — the balance
	 * carries forward, so one such row poisons the tail.
	 */
	unconverted_count: number;
	/** `null` means no threshold is set — deliberately not `"0"`. */
	threshold: MoneyString | null;
	periods: CashPositionPeriod[];
	breaches: CashPositionBreach[];
}

// CFO metrics dashboard — `GET /api/analytics/cfo`. A tenant-wide roll-up with
// no per-row currency, rendered via the org default currency (`orgCurrency`).
// Money is `MoneyString`, like every other endpoint in this file. What is NOT
// a string here is not money: a DAY COUNT (`dpo_*`, `cash_conversion_cycle`), a
// PERCENTAGE (`*_share_pct`, `rate_pct`, `yield_pct`) and a row count are
// genuinely numbers, and stringifying one would be a bug, not compliance.

export interface CfoDpoTrendPoint {
	month: string;
	/** A day count, not money. */
	dpo: number;
}

export interface CfoAccruals {
	open_po_amount: MoneyString;
	received_amount: MoneyString;
	unposted_invoice_amount: MoneyString;
	total_accrual: MoneyString;
}

export interface CfoSupplierConcentration {
	total_spend: MoneyString;
	top_10_share_pct: number;
	top_50_share_pct: number;
	largest_vendor: string | null;
	largest_vendor_share_pct: number;
	flagged: boolean;
	/**
	 * Invoices folded into `total_spend` — and therefore into every share above,
	 * and into `flagged` — at FACE value, because no locked exchange rate bridged
	 * them into the reporting currency. A count, not money. Non-zero means these
	 * percentages are computed over a part-converted denominator; render the
	 * shared `role="alert"` skipped-rows note beside them, as the cash-position
	 * and AP-balance cards do for theirs.
	 */
	unconverted_count: number;
}

export interface CfoFraudTrendPoint {
	month: string;
	invoice_count: number;
	exception_count: number;
	/**
	 * Exceptions / invoices, as a percentage — `null` when the month booked no
	 * invoices at all, because a rate with an empty denominator is NOT
	 * COMPUTABLE. It used to arrive as `0`, which drew the most reassuring bar
	 * on the chart over the one period carrying no information (and hardest in
	 * the "no invoices but exceptions raised anyway" case). Render the
	 * `insufficient_data` state, never a zero. `docs/decisions.md` §34.
	 */
	rate_pct: number | null;
	insufficient_data: boolean;
}

export interface CfoRebateYield {
	rebates_total: MoneyString;
	total_spend: MoneyString;
	yield_pct: number;
	annualised_rebates: MoneyString;
}

/**
 * One foreign currency's open exposure.
 *
 * **The four money fields are NOT all in the same currency**, which is what
 * makes this row easy to render wrong (and it was): `open_original_amount` is
 * denominated in this row's own `currency`, the other three in the org's
 * REPORTING currency. `compute_unrealized_fx_gain_loss` skips same-currency
 * invoices outright, so `currency` here is never the reporting currency —
 * formatting the open exposure with the reporting code is always wrong, never
 * merely redundant.
 */
export interface CfoUnrealizedFxByCurrency {
	currency: string;
	/** In `currency` above — this row's own, never the reporting currency. */
	open_original_amount: MoneyString;
	/** In the REPORTING currency, at the rate locked when it was materialized. */
	booked_reporting_amount: MoneyString;
	/** In the REPORTING currency, at today's rate. */
	current_reporting_amount: MoneyString;
	/** In the REPORTING currency: booked − current (positive = gain). */
	unrealized_gain_loss: MoneyString;
	/**
	 * Open invoices in this currency with no locked exchange rate. A count, not
	 * money — they are EXCLUDED from every figure on this row rather than booked
	 * at face value, which would report the conversion itself as a gain/loss.
	 */
	unconverted_count: number;
}

export interface CfoUnrealizedFx {
	reporting_currency: string;
	total_unrealized_gain_loss: MoneyString;
	by_currency: CfoUnrealizedFxByCurrency[];
	/** A count, not money — see `CfoUnrealizedFxByCurrency.unconverted_count`. */
	unconverted_count: number;
	available: boolean;
}

export interface CfoReportingSpendByCurrency {
	currency: string;
	original_amount: MoneyString;
	reporting_amount: MoneyString;
	count: number;
	unconverted_count: number;
}

export interface CfoReportingSpend {
	reporting_currency: string;
	total_amount: MoneyString;
	total_count: number;
	unconverted_count: number;
	by_currency: CfoReportingSpendByCurrency[];
}

export interface CfoAnalytics {
	period_days: number;
	period_start: string;
	total_spend: MoneyString;
	reporting_spend: CfoReportingSpend;
	unrealized_fx: CfoUnrealizedFx;
	accounts_payable_balance: MoneyString;
	// Currency-aware counterpart — `accounts_payable_balance` above is a
	// naive cross-currency sum kept for API back-compat; this is the figure
	// to render, same shape as `reporting_spend`.
	reporting_accounts_payable_balance: CfoReportingSpend;
	/** A day count, not money. */
	dpo_current: number;
	dpo_trend: CfoDpoTrendPoint[];
	/** A day count, not money; `null` when receivables data is unavailable. */
	cash_conversion_cycle: number | null;
	accruals: CfoAccruals;
	working_capital_impact_5_days: MoneyString;
	avg_daily_outflow: MoneyString;
	supplier_concentration: CfoSupplierConcentration;
	fraud_rate_trend: CfoFraudTrendPoint[];
	rebate_yield: CfoRebateYield;
}

// Consolidated reporting ACROSS entities — `GET /api/analytics/by-entity`.
// Money fields are string-Decimal (the backend never floats currency); render
// them through the `Money` component / `formatMoney`, never `parseFloat`. This
// endpoint reports every active entity at once and intentionally ignores the
// X-Entity-ID selection.

export interface EntityMetrics {
	/** Naive cross-currency SUM, kept for back-compat. Denominated in nothing — never render it. */
	total_spend: string;
	/**
	 * Currency-aware counterpart of `total_spend`, in `reporting_currency` — the
	 * same population and rollup as `/cfo`'s `reporting_spend`. Rows with no
	 * locked rate are counted at FACE value; the count below says how many.
	 */
	reporting_total_spend: string;
	reporting_total_spend_unconverted_count: number;
	/** Naive cross-currency SUM, kept for back-compat. Never render it. */
	outstanding_amount: string;
	// Currency-aware counterpart of `outstanding_amount` — the same rollup
	// `/cfo`'s `reporting_accounts_payable_balance` uses, in
	// `reporting_currency`, unconverted rows at face value.
	reporting_outstanding_amount: string;
	reporting_currency: string;
	reporting_outstanding_unconverted_count: number;
	invoice_count: number;
	open_exceptions: number;
	/**
	 * Sum of open PO totals. `PurchaseOrder` records no currency, so this is in
	 * currencies nobody recorded and carries no code — render it bare.
	 */
	open_po_amount: string;
}

export interface EntityRollupRow extends EntityMetrics {
	entity_id: string;
	entity_name: string;
	entity_slug: string;
	/**
	 * The entity's CONFIGURED currency (`null` = the org's reporting one). Entity
	 * metadata, not a denomination: none of the figures above is in it.
	 */
	currency: string | null;
	is_default: boolean;
}

export interface AnalyticsByEntity {
	period_days: number;
	period_start: string;
	entities: EntityRollupRow[];
	consolidated: EntityMetrics;
}

// Early-payment discount capture — the `discount_capture` block of
// `GET /api/dashboard` (`services/analytics.compute_discount_capture`).
//
// **A three-way fold, not two.** An eligible invoice is `missed` only once its
// discount window has ELAPSED without being captured; while the deadline is
// still ahead it is `pending` — still fully on the table. Rendering
// captured-vs-missed alone reports live opportunities as forgone savings.
//
// The figures to RENDER are `*_amount_reporting`, denominated in
// `reporting_currency`. The bare `*_amount` fields are per-row FACE values and
// mix currencies the moment one eligible invoice is foreign — they are typed
// here so the shape is honest, not because a surface should show them.

export interface DashboardDiscountCapture {
	eligible_count: number;
	captured_count: number;
	missed_count: number;
	/** Windows still OPEN — capturable, never counted as a miss. */
	pending_count: number;
	/** Per-row face value; mixes currencies. Render the `_reporting` twin. */
	captured_amount: MoneyAmount;
	missed_amount: MoneyAmount;
	pending_amount: MoneyAmount;
	/** The currency the three `_reporting` figures below are denominated in. */
	reporting_currency: string;
	captured_amount_reporting: MoneyAmount;
	missed_amount_reporting: MoneyAmount;
	pending_amount_reporting: MoneyAmount;
	/**
	 * A COUNT, not money: eligible rows with no usable rate lock, contributing
	 * FACE value to the three `_reporting` figures rather than being dropped.
	 * Non-zero means those figures mix currencies — say so at the point of
	 * reading (`docs/decisions.md` §35).
	 */
	unconverted_count: number;
	/**
	 * Captured / (captured + missed), as a percentage — `null`, never `0`, when
	 * nothing has been DECIDED yet. "We have not missed a discount yet" and "we
	 * captured none of the discounts we could have" are opposite facts and 0%
	 * renders as the bad one. Read `insufficient_data`, never a zero.
	 */
	capture_rate_pct: number | null;
	insufficient_data: boolean;
}

// The tenant dashboard — `GET /api/dashboard`.
//
// These three lived inside `routes/+page.svelte` as un-exported local
// interfaces, so the ONLY thing that ever compared a payload against them was
// the route itself. Every e2e stub of this endpoint was a hand-maintained
// literal that drifted the moment the API gained a non-optional field, and one
// did: the stub omitted `unconverted_count`, `undefined > 0` is false, and the
// partial-conversion disclosure could only ever render its no-notice branch.
// Exported here so the fixtures can `satisfies DashboardData` — which bites
// because `tsconfig.e2e.json` typechecks the Playwright tree that `pnpm check`
// skips.
//
// Money on this endpoint is `MoneyAmount`, not `MoneyString`: it serialises
// through `backend/app/schemas/money.py::MoneyAmount`, which deliberately
// writes a JSON *number*. See `frontend/CLAUDE.md` § Money formatting for why
// the two spellings both exist and why neither is a free choice.

export interface AgingBuckets {
	current: MoneyAmount;
	days_30: MoneyAmount;
	days_60: MoneyAmount;
	days_90: MoneyAmount;
	days_90_plus: MoneyAmount;
}

export interface ReportingAgingBuckets extends AgingBuckets {
	/**
	 * Invoices summed into these five bands at FACE value because no locked
	 * rate bridged them into the reporting currency. ONE count for the band
	 * set, not five — the actionable fact is the same either way. The bare
	 * `aging` deliberately carries none: it is a face-value cross-currency sum
	 * in its entirety, so a count would understate it.
	 */
	unconverted_count: number;
}

export interface DashboardData {
	total_invoices: number;
	total_amount: MoneyAmount;
	/**
	 * Currency-aware rollup of the whole invoice book into ONE reporting
	 * currency — `total_amount` above sums raw `Invoice.amount` across
	 * currencies and is kept only for API back-compat; every KPI below renders
	 * from this instead.
	 */
	reporting: {
		reporting_currency: string;
		total_amount: MoneyAmount;
		total_count: number;
		unconverted_count: number;
	};
	total_paid: MoneyAmount;
	total_pending: MoneyAmount;
	/** Reporting-currency counterparts of `total_paid` / `total_pending`. */
	total_paid_reporting: MoneyAmount;
	total_pending_reporting: MoneyAmount;
	total_paid_unconverted_count: number;
	total_pending_unconverted_count: number;
	total_rebates: MoneyAmount;
	/**
	 * Rebates left out of `total_rebates` for being denominated in another
	 * currency — a DIFFERENT fact from the unconverted counts above, which are
	 * rows whose reporting figure could not be established at all.
	 */
	excluded_rebate_count?: number;
	touchless_rate: number;
	stale_approvals: number;
	open_exceptions: number;
	pipeline: Record<string, number>;
	vendor_spend: Array<{
		vendor: string;
		amount: MoneyAmount;
		/**
		 * This vendor's invoices that entered `amount` at face value for want
		 * of a rate lock. Per vendor, and it matters here beyond the number:
		 * the tile RANKS vendors against each other, and an unconverted total
		 * is not comparable to a converted one.
		 */
		unconverted_count: number;
	}>;
	aging: AgingBuckets;
	/** Reporting-currency counterpart of `aging`. */
	aging_reporting: ReportingAgingBuckets;
	monthly_trend: Array<{
		month: string;
		count: number;
		amount: MoneyAmount;
		reporting_amount: MoneyAmount;
		/**
		 * Per MONTH, not per series: a trend is read bar against bar, so which
		 * step in the line is part-converted is the useful fact.
		 */
		unconverted_count: number;
	}>;
	upcoming_payments: Array<{
		id: string;
		invoice_number: string;
		vendor_name: string;
		/** The invoice's FACE amount, in `currency` below — not a reporting figure. */
		amount: MoneyAmount;
		/**
		 * The invoice's own currency, which `amount` is denominated in. Labelling
		 * these rows with the page's reporting currency put a `$` on a EUR
		 * invoice (`docs/decisions.md` §200). `null` renders bare.
		 */
		currency: string | null;
		due_date: string | null;
		is_overdue: boolean;
	}>;
	/**
	 * Early-payment discount capture — a three-way captured / missed / PENDING
	 * fold with its own reporting currency and its own `unconverted_count`.
	 * See {@link DashboardDiscountCapture} for why the pending bucket and the
	 * `null` capture rate are not cosmetic.
	 */
	discount_capture: DashboardDiscountCapture;
}

// Forecast vs actual — `POST /api/analytics/forecast_variance`.
//
// A POST, not a GET, because the forecast is the CALLER's: the org sends
// `{"months": [{"month": "YYYY-MM", "forecast": "100000"}, ...]}` and the
// backend fills in the actual completed outflow per month, then the variance.
// **Forecasts are never persisted** — the CFO pastes from their FP&A tool, so
// every render starts from what was typed on this visit.

export interface ForecastVarianceInput {
	/** `YYYY-MM`. A malformed month is a 422, not a silently-dropped row. */
	month: string;
	/**
	 * The CALLER's own figure, as the exact decimal string it was typed as —
	 * never a JSON number. `json.loads` decodes the body before any validator
	 * runs, so a fractional JSON number has already been through a binary float
	 * by the time pydantic sees it (`utils/moneyInput.ts`).
	 */
	forecast: MoneyString;
}

export interface ForecastVarianceRow {
	month: string;
	/** Echoed back from the request, quantized to 2dp. */
	forecast: MoneyString;
	/**
	 * Completed payments in the month, resolved into `reporting_currency` — NOT
	 * summed off raw `Payment.amount`, which is denominated in the invoice's
	 * currency. See `unconverted_count`.
	 */
	actual: MoneyString;
	/** `actual − forecast`. Positive = we paid out MORE than planned. */
	variance: MoneyString;
	/**
	 * A percentage, not money. The backend emits `0` when the forecast is not
	 * positive — a percentage of zero is NOT COMPUTABLE, and `0%` reads as
	 * "exactly on plan". Render `variancePctLabel`'s not-applicable state
	 * instead (`docs/decisions.md` §34).
	 */
	variance_pct: number;
	/**
	 * A COUNT, not money: completed payments this month whose outflow could not
	 * be expressed in `reporting_currency`. They are EXCLUDED from `actual`
	 * rather than added at face value, so a non-zero count means `actual` — and
	 * therefore the variance — is a FLOOR (`docs/decisions.md` §35).
	 */
	unconverted_count: number;
}

export interface ForecastVariance {
	/** The currency all three money figures on every row are denominated in. */
	reporting_currency: string;
	rows: ForecastVarianceRow[];
}
