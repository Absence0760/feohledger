/**
 * Resolving a tenant's REPORTING (base) currency out of `GET /api/organization`.
 *
 * The backend now serves its OWN answer as a top-level field,
 * `resolved_reporting_currency` — the exact output of
 * `backend/app/services/currency_conversion.py::resolve_reporting_currency`,
 * all four rungs included (the fourth, `settings.reporting_currency_default` /
 * `FEOH_REPORTING_CURRENCY_DEFAULT`, is operator config no client could
 * previously read at all). {@link resolveOrgCurrency} reads that field first.
 *
 * The three-rung client resolution below, {@link resolveReportingCurrency},
 * stays as the FALLBACK for a cached or pre-upgrade backend response that
 * omits the field — it must keep mirroring the backend's first three rungs
 * exactly, or a stale client silently mislabels a figure the server would
 * have answered correctly. It decides what currency the API's cross-currency
 * rollups are *actually denominated in*: `/api/payments/summary`, the CFO
 * forecast + cash position, the dashboard's `reporting` block, the discount
 * dashboard.
 *
 * `orgSettings.svelte.ts` read only `invoice_defaults.currency` before this
 * module existed, which is the LAST of the three client-visible rungs. That
 * was a mislabel rather than a fallback: an org reporting in GBP while its
 * invoice default stayed USD had its converted GBP totals rendered with a
 * `$`, on every aggregate figure in the app. The two keys agree in the common
 * case, which is exactly why it went unnoticed.
 *
 * Pure — no `$state`, no `fetch` — so it lives in `utils/` beside `money.ts`
 * and is unit-tested under the plain-Node vitest config. (The store itself is
 * a `.svelte.ts` rune module and can't be imported there.)
 */

/** The settings shape the client-side fallback reads. A projection of the org
 *  response — a non-admin caller only receives these three, by the allow-list
 *  in `backend/app/services/org_settings_view.py`. */
export interface ReportingCurrencySettings {
	reporting_currency?: string | null;
	payments?: { home_currency?: string | null } | null;
	invoice_defaults?: { currency?: string | null } | null;
}

/** The shape {@link resolveOrgCurrency} reads off `GET /api/organization` —
 *  the server's own resolved answer, plus the settings the fallback needs
 *  when that answer is missing (an older/cached backend response). */
export interface OrgCurrencyResponse {
	resolved_reporting_currency?: string | null;
	settings?: ReportingCurrencySettings | null;
}

/** A usable ISO 4217 code, or `null`. */
function usableCode(value: string | null | undefined): string | null {
	const code = (value ?? '').trim();
	return code.length === 3 ? code.toUpperCase() : null;
}

/**
 * Resolve the reporting currency, or `null` when the org declares none usable
 * — the caller keeps its own platform default rather than guessing here.
 *
 * The client-side FALLBACK chain — see the module docstring. Prefer
 * {@link resolveOrgCurrency}, which reads the server's own resolved field
 * first and only falls through to this.
 */
export function resolveReportingCurrency(
	settings: ReportingCurrencySettings | null | undefined
): string | null {
	return (
		usableCode(settings?.reporting_currency) ??
		usableCode(settings?.payments?.home_currency) ??
		usableCode(settings?.invoice_defaults?.currency)
	);
}

/**
 * Resolve the reporting currency for a `GET /api/organization` response: the
 * server's own `resolved_reporting_currency` when it names one, else the
 * three-rung client fallback over `settings` (a pre-upgrade or cached
 * response that omits the field). `null` only when both come up empty.
 */
export function resolveOrgCurrency(org: OrgCurrencyResponse | null | undefined): string | null {
	return usableCode(org?.resolved_reporting_currency) ?? resolveReportingCurrency(org?.settings);
}
