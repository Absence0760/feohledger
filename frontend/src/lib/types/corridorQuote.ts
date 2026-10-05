// Types + PURE display helpers for the multi-route corridor quote optimizer
// (`POST /api/payments/corridor-quotes`). Mirrors the response the handler in
// `backend/app/api/payments.py::compare_corridor_quotes` assembles.
//
// The endpoint prices ONE payable invoice across every processor the org has
// configured and ranks them. It is **advisory and read-only**: it books no
// `Payment`, claims no run, touches no invoice, and does not decide which rail
// pays. Which bank actually moves the money still comes from
// `payment_corridor.pick_corridor` + the org's configured provider. The
// response says so itself (`advisory: true`), and the UI must never imply that
// looking at a comparison selected anything.
//
// Kept free of `#lib/api` (and therefore of `$app/*`) so the helpers below stay
// unit-testable under vitest's plain-node environment — the same split
// `types/recurring.ts` and `types/vendorStatementRecon.ts` use.
//
// See `backend/docs/international-payments.md` § Multi-route quote optimization.
import type { MoneyString } from '#lib/utils/money.ts';
import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

/** How the auction ranks the routes. Mirrors the backend `OptimizeMode`. */
export type QuoteMode = 'cheapest' | 'fastest';

/**
 * One processor's answer for this payment.
 *
 * `available` is the load-bearing field. An adapter with no published fee
 * schedule fails CLOSED (`PaymentAdapter.quote_payment`'s base implementation
 * reports `available: false` / `unavailable_reason: "no_quote_endpoint"`)
 * rather than fabricating a free, instant quote — so it is **dropped from the
 * ranking**. `modern_treasury` is exactly that case today.
 *
 * That is why an unavailable quote must still be RENDERED: a tenant whose own
 * rail can't quote would otherwise watch an auction its rail never entered and
 * read the winner as "the best route available to us". A ranking that hides its
 * own gaps is worse than no ranking. `total_cost` is `null` for such a quote
 * (server-side it is `Decimal("Infinity")`, which is not a figure to render).
 */
export interface CorridorQuote {
	provider: string;
	method: string;
	available: boolean;
	unavailable_reason: string | null;
	/** Realised cost for this payment, exact decimal string. `null` when unavailable. */
	total_cost: MoneyString | null;
	/** Fixed component of the fee, exact decimal string. */
	flat_fee: MoneyString;
	/** Proportional component as a RATIO (e.g. "0.005" = 0.5%), not a percentage. */
	pct_fee: string;
	eta_business_days: number;
	/** Locked FX rate the processor quoted, when the corridor has an FX leg. */
	fx_rate: string | null;
}

export interface CorridorQuoteComparison {
	invoice_id: string;
	mode: QuoteMode;
	/** The payment's source currency — every figure in the response is in it. */
	currency: string;
	amount: MoneyString;
	winner: CorridorQuote;
	runners_up: CorridorQuote[];
	/** Winner's saving over the next-best AVAILABLE route, exact decimal string. */
	savings_vs_runner_up: MoneyString;
	/** Always true. The server states its own advisory nature in the payload. */
	advisory: boolean;
}

/**
 * Every quote the server returned, winner first — ranked and unranked alike.
 *
 * Deliberately NOT filtered: the caller splits it with {@link isRankedQuote} so
 * the non-quoting processors are shown as such instead of silently vanishing.
 */
export function allQuotes(cmp: CorridorQuoteComparison): CorridorQuote[] {
	return [cmp.winner, ...cmp.runners_up];
}

/** A quote that actually took part in the ranking. */
export function isRankedQuote(q: CorridorQuote): boolean {
	return q.available === true;
}

/** The processors that could not quote, so could not be ranked. */
export function unrankedQuotes(cmp: CorridorQuoteComparison): CorridorQuote[] {
	return allQuotes(cmp).filter((q) => !isRankedQuote(q));
}

/**
 * i18n key explaining WHY a processor is not in the ranking, or `null` when the
 * reason is not one we hold copy for.
 *
 * The reason vocabulary is open — concrete adapters emit their own sentences
 * (`"method 'sepa' not supported by column"`) alongside the small set of
 * machine codes the aggregator and the base class produce. Only the machine
 * codes are mapped; anything else must render VERBATIM, because the adapter's
 * own sentence is more informative than a generic bucket and inventing one for
 * an unmapped code would misreport it.
 *
 * The prefixed forms (`adapter_error:<Class>`, `provider_not_configured:<Class>`)
 * carry an exception CLASS after the colon — matched on the prefix so a new
 * class needs no new key.
 */
export function quoteReasonKey(reason: string | null | undefined): string | null {
	if (!reason) return null;
	if (reason === 'no_quote_endpoint') return 'payments.quotes.reason.noQuoteEndpoint';
	if (reason === 'provider_not_supported') return 'payments.quotes.reason.notSupported';
	if (reason === 'disabled_in_config') return 'payments.quotes.reason.disabled';
	if (reason === 'not_configured') return 'payments.quotes.reason.notConfigured';
	if (reason.startsWith('provider_not_configured:')) return 'payments.quotes.reason.notConfigured';
	if (reason.startsWith('adapter_error:')) return 'payments.quotes.reason.adapterError';
	return null;
}

/**
 * Render a proportional fee RATIO as a percentage string.
 *
 * `pct_fee` is a rate, not money — turning "0.005" into "0.5%" is a display
 * conversion of a dimensionless ratio, not arithmetic on an amount. No money
 * value is ever coerced to a number here (`frontend/CLAUDE.md` § Money
 * formatting: never add, subtract or compare two amounts client-side).
 *
 * Non-finite / unparseable provider input renders as the placeholder rather
 * than `NaN%` — the same clamp-and-guard rule `formatExtractionConfidence`
 * applies to provider-supplied confidence.
 */
export function formatFeeRate(pct: string | null | undefined, locale?: string): string {
	if (pct === null || pct === undefined || String(pct).trim() === '') return '—';
	const n = Number(pct);
	if (!Number.isFinite(n)) return '—';
	return new Intl.NumberFormat(locale ?? getActiveFormatLocale(), {
		style: 'percent',
		minimumFractionDigits: 0,
		maximumFractionDigits: 3
	}).format(n);
}
