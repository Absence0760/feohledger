/**
 * Grouping currency-tagged money amounts into per-currency subtotals.
 *
 * The house rule for a multi-currency rollup is **be honest about what could
 * not be combined rather than render one wrong number** — the same rule
 * `/cfo` applies with `unconverted_count` and `/discounts` with
 * `unconvertible_count`. A screen that sums EUR 100 and USD 100 into "200"
 * is not reporting a total; it is reporting a number that is not denominated
 * in anything real.
 *
 * This is the display-side primitive for that rule: it never converts (an FX
 * rate fetched on a read makes the figure non-deterministic — see
 * `backend/docs/multi-currency.md`), it never adds across currencies, and it
 * sums *within* each currency through {@link sumMoney} so every subtotal stays
 * exact (Decimal strings scaled through `BigInt`, never a float reduce).
 *
 * **A row whose currency nobody established is its own group, keyed `null`.**
 * It is never filed under a real code: adding it to the org's subtotal would
 * put money of unknown denomination inside a figure that claims one
 * (`docs/decisions.md` §200), and a backend that reports a total under `""`
 * does so precisely so it is "not folded into another currency's figure"
 * (`api/bank_reconciliation.py::_currency_totals`). Dropping it would
 * understate the selection instead, so it is kept, and rendered bare —
 * `formatMoney` with a `null` code, the rendering §160 / §196 settled on for a
 * figure with no provable currency.
 *
 * Pure — no `$state`, no `fetch`, no browser globals — so it lives in `utils/`
 * beside `money.ts` and is unit-tested under the plain-Node vitest config.
 */

import { formatMoney, sumMoney, type MoneyAmount } from './money';

/** One currency's slice of a mixed-currency selection. */
export interface CurrencyGroup {
	/**
	 * Resolved ISO 4217 code, uppercased — or `null` for the rows no usable code
	 * was given for. Never a borrowed default.
	 */
	currency: string | null;
	/** Exact sum of this currency's amounts (via `sumMoney`). */
	total: number;
	/** How many rows contributed to this subtotal. */
	count: number;
}

/** A row carrying an amount and the currency it is denominated in. */
export interface CurrencyTaggedAmount {
	amount: MoneyAmount;
	currency?: string | null;
}

/**
 * Normalise a possibly-empty currency code, or `null` when there is none to
 * normalise — the same shape test `money.ts` applies, so a code this helper
 * groups under is one `formatMoney` will render a symbol for.
 */
function normaliseCurrency(currency: string | null | undefined): string | null {
	const code = (currency ?? '').trim().toUpperCase();
	return /^[A-Z]{3}$/.test(code) ? code : null;
}

/**
 * Bucket `rows` by currency and sum each bucket exactly.
 *
 * Rows with no usable currency code form ONE group of their own (`currency:
 * null`) rather than being dropped — dropping them would understate the
 * selection — and rather than joining any real currency's subtotal.
 *
 * Ordering is by currency code ascending with the unknown group last:
 * deterministic, and stable as the amounts move (a total-ordered list would
 * reshuffle mid-selection).
 *
 * Returns `[]` for an empty input — the caller decides what "nothing selected"
 * reads as, because that is a display decision, not a money one.
 */
export function groupAmountsByCurrency(rows: Iterable<CurrencyTaggedAmount>): CurrencyGroup[] {
	const buckets = new Map<string | null, MoneyAmount[]>();

	for (const row of rows) {
		const code = normaliseCurrency(row.currency);
		const existing = buckets.get(code);
		if (existing) existing.push(row.amount);
		else buckets.set(code, [row.amount]);
	}

	return [...buckets.entries()]
		.map(([currency, amounts]) => ({
			currency,
			total: sumMoney(amounts),
			count: amounts.length
		}))
		.sort(byCurrencyThenUnknown);
}

function byCurrencyThenUnknown(a: CurrencyGroup, b: CurrencyGroup): number {
	if (a.currency === b.currency) return 0;
	if (a.currency === null) return 1;
	if (b.currency === null) return -1;
	return a.currency.localeCompare(b.currency);
}

/**
 * Does this set of groups span more than one currency?
 *
 * A named predicate rather than `groups.length > 1` at the call site, because
 * the *consequence* is specific: `services/payment_runs.create_payment_run_for_invoices`
 * refuses a payment run spanning more than one currency with a 422
 * ("All invoices in a payment run must share the same currency"), so a mixed
 * selection is not merely awkward to display — it cannot be submitted at all.
 *
 * An unknown-currency group counts as a currency of its own here: nothing
 * proves it is the same as the one beside it, so a selection holding both is
 * reported as mixed — the direction that warns rather than the one that lets a
 * run fail at submit.
 */
export function spansMultipleCurrencies(groups: CurrencyGroup[]): boolean {
	return groups.length > 1;
}

/**
 * Render one formatted subtotal per currency, in the order given.
 *
 * The display half of the same rule: each figure is formatted in its OWN
 * currency and they are shown side by side — never concatenated into a single
 * number, never converted. Accepts a bare `{currency, total}` shape so it works
 * on both a locally-computed {@link CurrencyGroup} and a server rollup that
 * sends exact decimal STRINGS (`GET /api/expenses/summary`'s `by_currency`) —
 * the string path is the better one, since it never round-trips a total through
 * a float at all.
 *
 * A total with no usable code — a `null` group, or a server rollup that
 * reported an unestablished currency under `""` — renders bare, never under
 * the org's code. There is deliberately no fallback parameter: a caller that
 * has one to give is exactly the caller this helper must not trust with it.
 *
 * Returns `[]` for an empty input. What "nothing" reads as is the caller's
 * decision (a zero in the org currency on `/payments`' pay bar; a zero on the
 * `/expenses` KPI card), because that is a display choice, not a money one.
 */
export function formatCurrencyTotals(
	totals: Iterable<{ currency?: string | null; total: MoneyAmount }>
): string[] {
	// `formatMoney` applies the same shape test as `normaliseCurrency`, so `""`,
	// `null` and a malformed code all reach its bare rendering.
	return [...totals].map((t) => formatMoney(t.total, { currency: t.currency }));
}
