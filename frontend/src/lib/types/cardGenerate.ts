// The response of `POST /api/cards/generate` — mirrors the backend's
// `schemas/virtual_card.CardGenerateResponse`. `items` / `total` are the cards
// THIS request minted (unchanged since before `skipped` existed); `skipped`
// names every other requested invoice and the gate that stopped it, in the
// coded-refusal shape. See backend/docs/virtual-cards.md § Why an invoice got
// no card.
import type { MoneyAmount } from '#lib/utils/money.ts';

/** The fields of a minted card this client reads (`CardResponse`). */
export interface GeneratedCard {
	id: string;
	invoice_id: string;
	vendor_id: string | null;
	card_provider: string;
	last_four: string | null;
	amount_limit: MoneyAmount;
	currency: string;
	status: string;
	expires_at: string | null;
}

/**
 * One invoice that got no card. `code` is one of the backend's
 * `card_issuance.CARD_SKIP_MESSAGES` keys; render through
 * `cardGenerateSkipText` (the shared `localizeApiDetail` registry), which falls
 * back to the server's English `message` for a code this build predates.
 */
export interface CardGenerateSkip {
	invoice_id: string;
	code: string;
	message: string;
	params: Record<string, unknown>;
}

export interface CardGenerateResponse {
	items: GeneratedCard[];
	total: number;
	/** Absent from a backend older than the skip codes — read as empty. */
	skipped?: CardGenerateSkip[];
}

/** The skip that names an existing live card carries its id. */
export const CARD_SKIP_ALREADY_CARDED = 'card_skip_already_carded';

/** The card a retry found already minted, or `null`. */
export function alreadyCardedCardId(skip: CardGenerateSkip): string | null {
	if (skip.code !== CARD_SKIP_ALREADY_CARDED) return null;
	const id = skip.params.card_id;
	return typeof id === 'string' && id ? id : null;
}

/**
 * The invoice statuses `POST /api/cards/generate` mints for — the backend's
 * `api/payments.PAYABLE_INVOICE_STATUSES`, which the endpoint filters on. Used
 * ONLY to narrow the Generate dialog's picker; the server re-runs every gate
 * and names whatever it refuses in `skipped`, so a stale list costs an
 * explained skip, never a card. `cardGenerate.test.ts` pins it to the backend.
 */
export const CARDABLE_INVOICE_STATUSES = ['approved', 'posted_in_erp', 'payment_scheduled'] as const;

/** One requested invoice that got no card, with the number the reader picked
 *  it by (`null` when the response names an invoice this dialog never sent). */
export interface CardGenerateSkipRow {
	invoiceId: string;
	invoiceNumber: string | null;
	skip: CardGenerateSkip;
}

/** The response's skips in the order the server listed them, each labelled
 *  with the invoice number from the reader's own selection. */
export function cardGenerateSkipRows(
	response: CardGenerateResponse,
	invoiceNumbers: ReadonlyMap<string, string>
): CardGenerateSkipRow[] {
	return (response.skipped ?? []).map((skip) => ({
		invoiceId: skip.invoice_id,
		invoiceNumber: invoiceNumbers.get(skip.invoice_id) ?? null,
		skip
	}));
}
