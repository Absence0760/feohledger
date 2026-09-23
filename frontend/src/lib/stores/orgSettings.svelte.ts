import { api } from '$lib/api';
import { m } from '$lib/i18n/store.svelte';
import { resolveOrgCurrency, type OrgCurrencyResponse } from '$lib/utils/reportingCurrency';

/**
 * Tenant-wide display currency for *aggregate* figures that don't carry
 * their own per-row currency code — the adaptive thresholds, the approval
 * money labels, the zero an empty selection costs. Per-row amounts (an
 * invoice, a credit memo) always render with *their own* `currency` field via
 * `<Money currency={row.currency} />`; and a rollup whose PAYLOAD names the
 * currency it is denominated in (the dashboard's `reporting.reporting_currency`,
 * the cash position's `opening_balance_currency`, CFO metrics'
 * `reporting_currency`) is labelled from that payload, not from here. This
 * store backs only the figures with nothing better to read.
 *
 * Resolved from `GET /api/organization`'s own `resolved_reporting_currency` —
 * the exact answer `currency_conversion.resolve_reporting_currency` computed
 * server-side, all four rungs included. `utils/reportingCurrency.ts`'s
 * three-rung client resolution (`settings.reporting_currency` →
 * `settings.payments.home_currency` → `settings.invoice_defaults.currency`)
 * is kept as the FALLBACK for a cached or pre-upgrade response that omits the
 * field — see `resolveOrgCurrency`.
 *
 * **`currency` is `null` until resolved, and stays `null` when both the server
 * field and the three fallback rungs miss.** The backend's fourth rung —
 * `settings.reporting_currency_default` — used to be an operator setting no
 * client could read at all, so a code substituted here would have been a
 * guess at an operator-settable value, indistinguishable on screen from one
 * the tenant configured (`docs/decisions.md` §119, §160, §200). It used to
 * start at, reset to, and degrade to `USD`, which put a `$` on every figure
 * labelled from it for any org that set nothing — and on every figure before
 * the store had loaded at all. Callers pass it straight to `formatMoney`, which
 * renders a bare grouped figure for a `null` code (§196); a FORM that needs a
 * value takes `orgCurrency.currency ?? DEFAULT_CURRENCY` itself, explicitly.
 * Mobile's `OrgCurrencyStore` has the same contract.
 *
 * Reading only `invoice_defaults.currency` — as this store once did — was a
 * mislabel, not a fallback: an org reporting in GBP while its invoice default
 * stayed USD had its converted GBP totals rendered with a `$`. The two keys
 * agree in the common case, which is exactly why it went unnoticed.
 *
 * `GET /api/organization` is open to any authenticated org user, but the
 * settings it returns are projected by role: a non-admin gets an allow-list
 * that keeps the three keys above (this store is the consumer they are listed
 * for) and drops the tenant's third-party credentials — `payments` is admitted
 * for `home_currency` ONLY, never the processor credentials beside it. See
 * `backend/app/services/org_settings_view.py`; a future field needed here has
 * to be added there on purpose.
 *
 * Resilient by design: a failed load leaves `currency` `null` (figures render
 * bare) and is not marked loaded, so a later navigation retries it. Throwing
 * would take a dashboard down over a label.
 *
 * Cached for the session after the first successful load; `reset()`
 * clears it (e.g. on logout / tenant switch).
 */

class OrgSettingsStore {
	/** The resolved ISO 4217 code, or `null` for "not proven" — not loaded yet,
	 *  or genuinely unset on the org. Never a borrowed default. */
	currency = $state<string | null>(null);
	#loaded = false;
	#inflight: Promise<void> | null = null;

	/**
	 * The currency as TEXT for a label that names what a bare number input is
	 * denominated in — "Auto-approve below (EUR)". The resolved code when there
	 * is one, else the localized noun for the concept ("reporting currency"),
	 * which is what the backend compares against whatever it resolves to.
	 *
	 * Never pass this to `formatMoney` / `<Money>`: a noun is not a currency
	 * code, and a figure takes {@link currency}, `null` included.
	 */
	get label(): string {
		return this.currency ?? m('common.reportingCurrencyUnresolved');
	}

	/**
	 * Lazy-load the tenant reporting currency once per session. Safe to call
	 * from any page's `$effect`/`onMount`; concurrent callers share one
	 * in-flight request and a failure is swallowed.
	 */
	async ensureLoaded(): Promise<void> {
		if (this.#loaded) return;
		if (this.#inflight) return this.#inflight;
		this.#inflight = (async () => {
			try {
				const org = await api.get<OrgCurrencyResponse>('/api/organization');
				// `null` when the org declares nothing usable — assigned, not
				// skipped, so the store says "unknown" rather than keeping a
				// value from anywhere else.
				this.currency = resolveOrgCurrency(org);
				this.#loaded = true;
			} catch {
				// Transient error (or a signed-out race): stay unresolved and
				// don't mark loaded, so a later navigation can still resolve it.
			} finally {
				this.#inflight = null;
			}
		})();
		return this.#inflight;
	}

	reset(): void {
		this.currency = null;
		this.#loaded = false;
		this.#inflight = null;
	}
}

export const orgCurrency = new OrgSettingsStore();
