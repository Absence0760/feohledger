import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MoneyAmount } from '$lib/utils/money';

/**
 * Card-rebate shapes and the pure helpers the `/payments` Cards tab reads.
 *
 * A `CardRebate` is the processor's kickback on a virtual-card payment. Its
 * status is a HUMAN-DRIVEN record of something that happened out-of-band:
 * `backend/app/api/cards.py` creates every rebate at `pending` from the card
 * settlement webhook and nothing advances it, because Lithic/Nium confirm and
 * pay rebates on a periodic statement rather than on an event we ingest. So
 * `confirm` and `mark-paid` record what the processor already did — neither
 * requests a payout and neither moves money. See
 * `backend/docs/virtual-cards.md` § Rebate status lifecycle.
 */

/** `card_rebates.status`, in lifecycle order. */
export type RebateStatus = 'pending' | 'confirmed' | 'paid_out';

export const REBATE_STATUSES: RebateStatus[] = ['pending', 'confirmed', 'paid_out'];

/**
 * Badge tone per rebate status.
 *
 * `pending` is `warning` for the same reason `payment.pending` is: it is money
 * the org has NOT realized (the dashboard's "Rebates earned" headline
 * deliberately excludes it), and reading it as earned is the exact
 * misreading the split exists to prevent. `confirmed` is in flight;
 * `paid_out` has landed.
 *
 * Not a `Record<RebateStatus, …>` lookup at the call site: `status` is a bare
 * string on the wire (`RebateResponse.status` is `str`), so an unrecognised
 * value must land somewhere rather than render an untinted pill — use
 * {@link rebateTone}.
 */
export const REBATE_STATUS_TONES: Record<RebateStatus, BadgeTone> = {
	pending: 'warning',
	confirmed: 'accent',
	paid_out: 'success'
};

export function rebateTone(status: string): BadgeTone {
	return REBATE_STATUS_TONES[status as RebateStatus] ?? 'neutral';
}

/** One row of `GET /api/cards/rebates` (`schemas/virtual_card.py::RebateResponse`). */
export interface CardRebate {
	id: string;
	virtual_card_id: string;
	/** `MoneyAmount`, never `number` — the backend serialises `Decimal` here. */
	amount: MoneyAmount;
	/**
	 * The negotiated rebate RATIO (0.0125 = 1.25%), not a money amount. The
	 * backend types it `MoneyAmount` only to keep it off a binary float; it is
	 * a rate, so scaling it to a percentage is not money arithmetic — see
	 * {@link formatRebateRate}.
	 */
	rate: MoneyAmount;
	/**
	 * ISO code this row's `amount` is denominated in, resolved server-side from
	 * the `virtual_cards` row that earned the rebate (`card_rebates` has no
	 * currency column of its own). Always present — a mixed-currency programme's
	 * rows each carry their own code, which is what lets every amount render
	 * through {@link formatMoney} instead of bare.
	 */
	currency: string;
	/** Bare string on the wire; widen through {@link rebateTone} / a label map. */
	status: string;
	period: string | null;
	created_at: string;
}

export interface RebateListResponse {
	items: CardRebate[];
	/**
	 * Row COUNT of the whole filtered set — the canonical list envelope, what
	 * Load-more counts against. NOT the money; that is `total_amount`.
	 */
	total: number;
	page: number;
	page_size: number;
	/**
	 * Summed rebate amount over the WHOLE filtered set (never the loaded page),
	 * denominated in {@link RebateListResponse.currency}.
	 */
	total_amount: MoneyAmount;
	/** What `total_amount` is denominated in — the org's reporting currency. */
	currency: string;
	/**
	 * Rows left OUT of `total_amount` for being denominated in something else.
	 *
	 * It no longer gates whether a row can be rendered — every row states its
	 * own currency now — but it is still what stops a single-currency total
	 * beside a mixed list from reading as complete.
	 */
	excluded_rebate_count: number;
}

/** The lifecycle step available from `status`, or `null` at a terminal one. */
export type RebateTransition = 'confirm' | 'mark-paid';

/**
 * The ONE transition the backend will accept from this status.
 *
 * Mirrors the route guards: `confirm` requires `pending`, `mark-paid` requires
 * `confirmed`, and a rebate can never skip straight to `paid_out`. Deriving the
 * offered control from this (rather than rendering both and letting the server
 * 409) is why an out-of-order transition is not something the UI can propose —
 * the backend is still the authority, and its refusal is surfaced verbatim when
 * a row goes stale under a concurrent update.
 */
export function nextRebateTransition(status: string): RebateTransition | null {
	if (status === 'pending') return 'confirm';
	if (status === 'confirmed') return 'mark-paid';
	return null;
}

/**
 * A rebate ratio as a percentage string — `0.0125` → `"1.25%"`.
 *
 * Scaling a RATIO by 100 is not money arithmetic (nothing is denominated in a
 * currency here), which is why this lives beside the money types rather than
 * being barred by them. Non-finite / absent input renders the shared dash
 * placeholder rather than `NaN%`, the same way `formatMoney` does.
 */
export function formatRebateRate(rate: MoneyAmount, placeholder = '—'): string {
	if (rate === null || rate === undefined || rate === '') return placeholder;
	const n = typeof rate === 'number' ? rate : Number(rate);
	if (!Number.isFinite(n)) return placeholder;
	// Two decimals of a percent: a negotiated card rate is quoted like "1.25%".
	return `${(n * 100).toFixed(2)}%`;
}
