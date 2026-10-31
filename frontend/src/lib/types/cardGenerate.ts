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
