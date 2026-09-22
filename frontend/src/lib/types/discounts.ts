import type { MoneyAmount } from '$lib/utils/money';
import { normalizeMoneyInput } from '$lib/utils/moneyInput';
import type { BadgeTone } from '$lib/components/ui/badgeTone';

// Types for the Dynamic Discounting & Early-Payment Optimization surface.
// Mirrors the JSON the Phase-C `/api/discounts` router returns. Money fields
// arrive as JSON numbers (not string-Decimals); percentages are numbers.

/** A single sliding-scale tier: pay within `days` to earn `percent` off. */
export interface DiscountTier {
	days: number;
	percent: number;
}

export type DiscountScope = 'invoice' | 'vendor';
export type DiscountSource = 'supplier' | 'system' | 'financing';
export type DiscountStatus = 'offered' | 'accepted' | 'captured' | 'declined' | 'expired';

/**
 * Badge tone per offer status. Lives here rather than in the page because the
 * status is the type module's own union — the labels beside it are i18n keys
 * resolved per-locale, but the *tone* is a property of the state, so a second
 * surface that ever badges an offer inherits the calibration instead of
 * re-deriving it.
 *
 * `accepted` is amber and `captured` green on purpose: accepting an offer only
 * commits the AP team to pay early, and the saving is not realised until the
 * payment run funds it. Collapsing the two onto `success` would report money
 * we have not actually saved yet. `declined` and `expired` share `muted` — both
 * are an offer that will not be taken, and the label carries which.
 */
export const DISCOUNT_STATUS_TONES: Record<DiscountStatus, BadgeTone> = {
	offered: 'accent',
	accepted: 'warning',
	captured: 'success',
	declined: 'muted',
	expired: 'muted'
};

/** A discount offer on an invoice or a vendor-wide standing offer. */
export interface DiscountOffer {
	id: string;
	scope: DiscountScope;
	invoice_id: string | null;
	vendor_id: string | null;
	source: DiscountSource;
	status: DiscountStatus;
	/** Sliding-scale tiers, typically ordered soonest-deadline first. */
	tiers: DiscountTier[];
	/**
	 * Invoice (or projected) amount the discount applies to.
	 *
	 * `/discounts` previews each tier's saving as `base_amount * percent / 100`
	 * — the API exposes no per-tier savings figure — through `scaleMoney`'s
	 * exact `divideBy`, so the preview cannot disagree with what the server
	 * books on accept.
	 */
	base_amount: MoneyAmount;
	currency: string;
	valid_from: string | null;
	valid_until: string | null;
	/** The tier the AP team accepted, or null while still `offered`. */
	accepted_tier: DiscountTier | null;
	accepted_at: string | null;
	/** Actual discount captured once paid; null until captured. */
	captured_amount: MoneyAmount;
	captured_at: string | null;
	financing_provider: string | null;
	notes: string | null;
	created_at: string;
	updated_at: string;
	/** Denormalised for table display. */
	vendor_name: string | null;
	invoice_number: string | null;
}

/** Paginated offer list — matches the shared `{items,total,page,page_size}` shape. */
export interface DiscountOfferPage {
	items: DiscountOffer[];
	total: number;
	page: number;
	page_size: number;
}

/** Dashboard KPI roll-up. Tenant-wide totals carry one `currency`. */
export interface DiscountDashboard {
	captured_count: number;
	captured_amount: MoneyAmount;
	missed_count: number;
	missed_amount: MoneyAmount;
	/** Captured / (captured + missed). `null` — with `insufficient_data` true —
	 *  when nothing has been decided yet: "no discount window has closed" and
	 *  "we captured none of the ones that did" are opposite facts, and `0`
	 *  reads as the second. Matches `DiscountCaptureMetrics` on `/dashboard`. */
	capture_rate_pct: number | null;
	/** `capture_rate_pct` is null because the decided population is empty. Set
	 *  whenever it is null, and only then — branch on either. */
	insufficient_data: boolean;
	open_offer_count: number;
	projected_savings: MoneyAmount;
	currency: string;
	/** Open offers left OUT of `projected_savings` because they are denominated
	 *  in something other than `currency` and no rate bridges them. The figure
	 *  is honest only because they were excluded, so the page must say so —
	 *  otherwise a multi-currency tenant reads a quietly-low number. */
	unconvertible_offer_count: number;
	/** Captured / declined+expired offers left OUT of `captured_amount` /
	 *  `missed_amount` for being denominated in something other than
	 *  `currency`. Amounts in different currencies are not added together, so
	 *  a non-zero count means those two realised figures describe part of the
	 *  set — the same honesty `unconvertible_offer_count` provides for
	 *  `projected_savings`. */
	excluded_captured_count: number;
	excluded_missed_count: number;
}

/**
 * Per-invoice ROI / cost-of-capital comparison for accepting early payment.
 *
 * **Every horizon-relative field is nullable**, because the horizon itself can
 * be unknown: a vendor-scoped bulk offer spans many invoices and has no single
 * net due date (`schemas/discount.py::DiscountROIResponse`). `horizon_known`
 * is the marker — when it is `false`, `days_accelerated` /
 * `annualized_return_pct` / `opportunity_cost` / `net_benefit` are `null` and
 * `worthwhile` is `null` meaning *cannot rank*, which is NOT the same answer
 * as `false` (*ranked, and it loses*).
 *
 * These were typed non-nullable while the server substituted the discount
 * deadline for a missing due date, which reported a fabricated `0.00 %` APR.
 * Now that the server withholds instead, a non-nullable type here would let a
 * caller write `roi.annualized_return_pct.toFixed(1)` and render `0.0% APR`
 * for "we don't know" — the exact claim the backend change exists to stop.
 */
export interface DiscountRoi {
	base_amount: MoneyAmount;
	discount_percent: number;
	days_accelerated: number | null;
	/** Horizon-FREE — a percentage of the base amount, so it is real even when
	 *  nothing else here is. It is the one figure an unrankable offer shows. */
	savings: MoneyAmount;
	annualized_return_pct: number | null;
	cost_of_capital_pct: number;
	opportunity_cost: MoneyAmount;
	/**
	 * The server's own savings-minus-opportunity-cost verdict. Rendered, never
	 * re-derived: `worthwhile` beside it is the boolean the UI branches on —
	 * and it is tri-state, so branch on `=== true`, never on truthiness alone
	 * where `null` would silently read as "not worthwhile".
	 */
	net_benefit: MoneyAmount;
	worthwhile: boolean | null;
	/** `false` = no net due date to accelerate against; read the nulls above as
	 *  "unknown", never as zero. Optional so a response predating the field
	 *  still parses — absent means the horizon WAS known (the old contract). */
	horizon_known?: boolean;
}

/** One ranked recommendation in an optimization run. */
export interface DiscountRecommendation {
	offer_id: string;
	invoice_id: string | null;
	vendor_id: string | null;
	vendor_name: string | null;
	invoice_number: string | null;
	tier_days: number;
	discount_percent: number;
	pay_by: string | null;
	roi: DiscountRoi;
	/** The currency THIS row's money is in. `roi.savings` is computed from the
	 *  offer's own `base_amount`, so it is the OFFER's currency — equal to the
	 *  response-level `currency` only when `unconvertible` is false. Optional
	 *  because a response predating the field must still render (the client
	 *  falls back to a symbol-free figure rather than guessing a code). */
	currency?: string;
	/** Whether the optimizer selected this offer under the cash budget. */
	selected: boolean;
	/** Running cash outlay through this recommendation in the ranked list. */
	cumulative_outlay: MoneyAmount;
	/** This offer's money is in a currency the totals are NOT in, so it is
	 *  excluded from every total (and from selection when a budget binds). Its
	 *  ROI percentages stay meaningful — a rate is currency-free. */
	unconvertible: boolean;
}

/** Budget-constrained optimization result. */
export interface DiscountOptimization {
	cash_budget: MoneyAmount;
	/** The currency EVERY money total below is denominated in (the org's
	 *  reporting currency) — stated by the API rather than assumed, because the
	 *  totals sum across offers that carry their own currencies. */
	currency: string;
	cost_of_capital_pct: number;
	total_savings_available: MoneyAmount;
	total_savings_selected: MoneyAmount;
	total_outlay_selected: MoneyAmount;
	/** Ranked offers left out of the totals because they are in another
	 *  currency. Spelled differently from the dashboard's
	 *  `unconvertible_offer_count` — two responses, two field names. */
	unconvertible_count: number;
	recommendations: DiscountRecommendation[];
	/**
	 * Offers with no resolvable net due date. Their `roi.horizon_known` is
	 * `false` and their APR / verdict are `null`: they cannot be placed in an
	 * APR ranking, so the server carries them here instead of sorting them to
	 * the bottom of `recommendations` at a fabricated 0.00 %. They are in no
	 * total and were never selected; each carries a real `roi.savings`.
	 *
	 * Optional because a response predating the field must still parse — the
	 * page renders `?? []`, so an older backend shows no section rather than
	 * throwing.
	 */
	unrankable?: DiscountRecommendation[];
}

/** Status-filter keys the dashboard's FilterChips drive. `missed` maps to the
 *  backend `declined` + `expired` statuses (a derived bucket, not a status). */
export type DiscountStatusFilter = 'all' | 'offered' | 'accepted' | 'captured' | 'missed';

// --- Bulk vendor negotiation (`POST /api/discounts/bulk-negotiate`) ---

/**
 * One tier of a proposed vendor-wide offer, as the user typed it.
 *
 * Both halves stay TEXT until they go on the wire. `percent` in particular is
 * sent as the exact decimal string it was typed as, never as a JSON number:
 * `json.loads` on the server decodes the body before any validator runs, so a
 * JSON number is already a float by the time pydantic sees it. Same reasoning
 * as the optimizer's cash budget (`utils/moneyInput.ts`) — and here the
 * percent is applied to a base spanning the vendor's whole open balance.
 */
export interface BulkTierInput {
	days: string;
	percent: string;
}

/**
 * `days` as an integer inside the server's own `ge=0, le=365` range, else
 * `null`.
 *
 * Refusing client-side buys the user a sentence they can act on instead of a
 * raw pydantic 422; the server stays authoritative either way.
 */
export function normalizeTierDays(raw: string | null | undefined): number | null {
	const text = (raw ?? '').trim();
	if (!/^\d{1,3}$/.test(text)) return null;
	const days = parseInt(text, 10);
	return days >= 0 && days <= 365 ? days : null;
}

/**
 * The exact decimal string to send for a tier percent, or `null` when it is
 * not a percent the server would accept (`gt=0, lt=100`).
 *
 * `Number` is read ONLY to compare against those bounds. What comes back is
 * the untouched text — the check must never become the value, which is the
 * discipline `utils/moneyInput.ts` is built around.
 */
export function normalizeTierPercent(raw: string | null | undefined): string | null {
	const text = normalizeMoneyInput(raw);
	if (text === null) return null;
	const bound = Number(text);
	if (!(bound > 0 && bound < 100)) return null;
	return text;
}

/** A tier row the user has started filling in — either field carrying text. */
export function isTierStarted(tier: BulkTierInput): boolean {
	return tier.days.trim() !== '' || tier.percent.trim() !== '';
}
