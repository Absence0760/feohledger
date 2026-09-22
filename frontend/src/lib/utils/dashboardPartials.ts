/**
 * When a dashboard CHART is built partly from unconverted rows.
 *
 * A foreign invoice with no locked exchange rate into the org's reporting
 * currency is summed at FACE value rather than dropped — the right call for a
 * spend figure, because dropping it would understate, but only honest if the
 * page says so (`docs/decisions.md` §35). The API carries a count on each of
 * the three chart blocks, at the grain each chart is READ at:
 *
 *   - `vendor_spend[].unconverted_count` — per vendor, because the tile RANKS
 *     vendors against each other and an unconverted total is not comparable
 *     with a converted one;
 *   - `aging_reporting.unconverted_count` — one for the whole band set;
 *   - `monthly_trend[].unconverted_count` — per month, because a trend is read
 *     bar against bar and a whole-series count would not say which step in the
 *     line not to trust.
 *
 * The KPI row's counts (`reporting`, `total_paid`, `total_pending`) are a
 * different question — two opposite rules, not one — and have their own
 * helper, {@link kpiRollupDisclosure}, below.
 *
 * Extracted for the same reason `discountPartialSet.ts` was: a `reduce` inline
 * in the template would force its unit test to restate the sum it is checking.
 * Pure — no `$state`, no `fetch` — so it unit-tests under the plain-Node vitest
 * config.
 */

/** The projection of a `vendor_spend` / `monthly_trend` entry these helpers
 *  read. Deliberately narrower than either full row type: the rule depends on
 *  the count and the label, and nothing else. */
export interface PartialSeriesEntry {
	unconverted_count: number;
}

/** Total invoices a series folded at face value.
 *
 * A negative, fractional or non-finite count is treated as zero rather than
 * propagated: the counts come off the wire, and a malformed one must not turn
 * the disclosure into a nonsense figure — or, worse, cancel a real one out.
 */
export function totalUnconverted(entries: readonly PartialSeriesEntry[] | null | undefined): number {
	if (!Array.isArray(entries)) return 0;
	return entries.reduce((n, e) => n + safeCount(e?.unconverted_count), 0);
}

/** The labels of the entries that folded at least one row at face value, in the
 *  series' own order.
 *
 * The count alone says "something here mixes currencies" and leaves the reader
 * to guess where; naming the vendors (or the months) is what makes the notice
 * act on the thing the chart is for.
 */
export function partialLabels<T extends PartialSeriesEntry>(
	entries: readonly T[] | null | undefined,
	label: (entry: T) => string
): string[] {
	if (!Array.isArray(entries)) return [];
	return entries.filter((e) => safeCount(e?.unconverted_count) > 0).map(label);
}

function safeCount(value: number | null | undefined): number {
	if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 0;
	return Math.floor(value);
}

/** The projection of `GET /api/dashboard` the KPI-row disclosure reads. */
export interface KpiRollupCounts {
	reporting: { unconverted_count: number };
	total_paid_unconverted_count: number;
	total_pending_unconverted_count: number;
}

/** A payment-side KPI that can leave rows out. */
export type ExcludingKpi = 'paid' | 'pending';

/**
 * The KPI row's two partial-conversion disclosures, which follow OPPOSITE rules
 * and so can never share one sentence (`docs/decisions.md` §200).
 *
 * - `faceValue` — invoices the **Total Amount** KPI counted at FACE value for
 *   want of a rate lock. `reporting.unconverted_count` comes from
 *   `currency_conversion.invoice_reporting_amount_sql`, which falls back to the
 *   face amount: that figure is not a floor, it mixes currencies.
 * - `excluded` — payments **Paid** / **Pending** left OUT, because
 *   `payment_reporting_amount_sql` refuses a face-value fallback (its figures
 *   are the ones a filed total is built from). Those figures ARE floors.
 *   `excludedFrom` names only the KPIs that actually lost a row, in row order,
 *   so the sentence never blames a figure that is complete.
 *
 * One banner saying "some totals above exclude rows" was right for Paid and
 * Pending and wrong for Total Amount — the one KPI a reader is most likely to
 * quote.
 */
export function kpiRollupDisclosure(data: KpiRollupCounts | null | undefined): {
	faceValue: number;
	excluded: number;
	excludedFrom: ExcludingKpi[];
} {
	const paid = safeCount(data?.total_paid_unconverted_count);
	const pending = safeCount(data?.total_pending_unconverted_count);
	const excludedFrom: ExcludingKpi[] = [];
	if (paid > 0) excludedFrom.push('paid');
	if (pending > 0) excludedFrom.push('pending');
	return {
		faceValue: safeCount(data?.reporting?.unconverted_count),
		excluded: paid + pending,
		excludedFrom
	};
}
