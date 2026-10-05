// Typed helpers for the CFO analytics endpoints that are not plain GETs.
// Requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// X-Entity-ID + 401-bounce). Mirrors the pattern of `src/lib/api/budgets.ts`.
//
// The cash-flow trio (`cashflow_forecast` / `cashflow_whatif` /
// `cash_position`) stays inline in `routes/cfo/+page.svelte` — three
// `api.get`s issued together through one request sequencer. This module exists
// for the endpoint that could NOT be a GET.
import { api } from '#lib/api.ts';
import type { ForecastVariance, ForecastVarianceInput } from '#lib/types/analytics.ts';

/**
 * Compare a caller-supplied forecast against the actual outflow per month.
 *
 * A **POST with a body**, not a GET with a query string, because the forecast
 * is the org's own figure set and is **never persisted** — the CFO pastes it
 * from their FP&A tool on each visit. The server fills in `actual` from
 * completed payments, resolved into its reporting currency, and returns the
 * variance.
 *
 * `forecast` on each row is the exact decimal STRING the user typed
 * (`utils/moneyInput.ts::normalizeMoneyInput`), never a JSON number: the body
 * is decoded by `json.loads` before any pydantic validator runs, so a
 * fractional number has already been through a binary float by then.
 *
 * Role gate is admin + CFO (`_CFO_ROLES` in `backend/app/api/analytics.py`) —
 * the same gate as the `/cfo` route that hosts the surface. Entity-scoped via
 * the ambient `X-Entity-ID` header.
 */
export function postForecastVariance(
	months: ForecastVarianceInput[]
): Promise<ForecastVariance> {
	return api.post<ForecastVariance>('/api/analytics/forecast_variance', { months });
}
