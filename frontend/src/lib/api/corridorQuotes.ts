// Typed helper for the multi-route corridor quote optimizer —
// `POST /api/payments/corridor-quotes` (`backend/app/api/payments.py`).
// Routes through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID
// + 401-bounce).
//
// **Advisory and read-only.** It books no `Payment`, claims no run, touches no
// invoice, and does not decide which rail pays — see `types/corridorQuote.ts`
// for the full contract and the pure display helpers.
//
// Role gate mirrors the server's `require_roles(admin, ap_manager, cfo)`, so a
// clerk never sees a control that can only 403.
import { api } from '#lib/api.ts';
import type { CorridorQuoteComparison, QuoteMode } from '#lib/types/corridorQuote.ts';

export interface CorridorQuoteRequest {
	invoiceId: string;
	/** Rail to price. Omitted → the server prices `ach`. */
	method?: string;
	mode?: QuoteMode;
}

/**
 * Price one payable invoice across every configured processor.
 *
 * 404 when the invoice is outside the caller's entity scope (the same opaque
 * 404 an unknown id gets); 409 when no configured provider can quote this
 * corridor at all. Surface the server's `detail` for both rather than
 * paraphrasing it — the 409 message names each provider's own machine reason.
 */
export function compareCorridorQuotes(
	req: CorridorQuoteRequest
): Promise<CorridorQuoteComparison> {
	return api.post<CorridorQuoteComparison>('/api/payments/corridor-quotes', {
		invoice_id: req.invoiceId,
		...(req.method ? { method: req.method } : {}),
		mode: req.mode ?? 'cheapest'
	});
}
