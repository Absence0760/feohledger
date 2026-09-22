/**
 * Which currency an optimizer recommendation's money is denominated in.
 *
 * `POST /api/discounts/optimize` returns three money TOTALS in one stated
 * `currency` (the org's reporting currency) plus a ranked list of
 * recommendations. Each recommendation's `roi.savings` is computed from its own
 * OFFER's `base_amount`, so it is in the OFFER's currency — equal to the
 * totals' currency only when the row is not flagged `unconvertible`.
 *
 * Each row now STATES its currency (`OptimizerRecommendation.currency`), so
 * that is what a figure is labelled with, flag or no flag. `unconvertible`
 * keeps its own separate job: it says this row's money is not in the totals'
 * currency and therefore is not counted in them, which the card reports as a
 * marker beside the (correctly labelled) figure.
 *
 * The resolution order matters, and both rungs are real:
 *
 *   1. the row's own `currency` — authoritative, whatever the flag says;
 *   2. no `currency` on the row (a response predating the field): fall back to
 *      `unconvertible === false` ⇒ the offer's currency PROVABLY equals the
 *      response's `currency`; `unconvertible === true` ⇒ the offer is in some
 *      OTHER currency the payload does not name, so there is no honest code to
 *      stamp on it and the caller renders the figure bare.
 *
 * The page used to label every card with the ORG default currency read from a
 * separate store, which is right for the convertible rows by coincidence and
 * wrong for exactly the rows the flag marks: a €412 saving rendered as
 * "Save $412.00", two lines below a banner saying those offers are in another
 * currency.
 *
 * Pure — no `$state`, no `fetch` — so it sits in `utils/` beside
 * `discountPartialSet.ts` and is unit-tested under the plain-Node vitest config.
 */

import { formatMoney } from '$lib/utils/money';

/** The projection of `DiscountRecommendation` this rule reads. Deliberately
 *  narrower than the full interface — the currency question depends on the
 *  row's own code and the exclusion flag, and nothing else. */
export interface RecommendationCurrencyFields {
	currency?: string | null;
	unconvertible?: boolean | null;
}

/**
 * The ISO 4217 code to render a recommendation's money with, or `null` when it
 * cannot be established.
 *
 * `null` is a real answer, not a failure: a caller must render the figure
 * without a currency symbol rather than pick one, because any code it picked
 * would be a claim the response does not support. It is now reachable only
 * from a response that omits the per-row `currency` AND flags the row
 * `unconvertible` — i.e. an older payload — so the client degrades to the
 * previous symbol-free rendering instead of breaking.
 */
export function recommendationCurrency(
	rec: RecommendationCurrencyFields | null | undefined,
	totalsCurrency: string | null | undefined
): string | null {
	// The row's own code wins over everything: it is the currency this row's
	// money is actually in, which is the question being asked.
	const own = normalizeCode(rec?.currency);
	if (own) return own;
	// No per-row code (older payload). `unconvertible` still tells us whether
	// the totals' currency is a safe stand-in.
	if (rec?.unconvertible) return null;
	return normalizeCode(totalsCurrency);
}

function normalizeCode(value: string | null | undefined): string | null {
	const code = (value ?? '').trim().toUpperCase();
	return /^[A-Z]{3}$/.test(code) ? code : null;
}

/**
 * A money figure formatted with grouping and two decimals but NO currency
 * symbol — the honest rendering when {@link recommendationCurrency} returns
 * `null`.
 *
 * Exactly `formatMoney` with no currency, which is what `formatMoney` renders
 * for any unprovable code (`docs/decisions.md` §198). Kept as a named export so
 * the call sites that deliberately withhold a currency say so, but it is one
 * primitive: grouping, decimals and the active locale cannot drift from the
 * labelled figures beside it.
 *
 * Null / non-finite input returns the placeholder rather than `NaN`.
 */
export function formatAmountWithoutCurrency(
	amount: number | string | null | undefined,
	placeholder = '—'
): string {
	return formatMoney(amount, { currency: null }, placeholder);
}
