// Typed helpers for the payment-path RECOVERY exits — the endpoints that exist
// to un-strand money that has already moved, or to finish a reversal whose
// best-effort leg did not land. Everything routes through the shared `api`
// client (Bearer + X-Tenant-Slug + X-Entity-ID + 401-bounce).
//
// Each mirrors its server gate in the UI so a holder-less role never sees a
// control that can only 403: `retryRunErpSync` / `acceptPaymentSettlement` are
// `payment.execute` (`PERM_PAYMENT_EXECUTE`), `voidPayment` /
// `retryVoidCardCancel` are `payment.void` (`PERM_PAYMENT_VOID`). None of them
// moves money — they report money that already moved, close out a payable the
// rail short-paid, reverse the books, or shut a card the void could not.
//
// See `backend/docs/payments.md` § ERP Payment Sync + § Settlement-amount
// verification + § Voiding a card payment.
import { api } from '#lib/api.ts';
import type { Payment } from '#lib/types/payment.ts';

/**
 * What `POST /api/payments/runs/{run_id}/sync-erp` returns.
 *
 * Read `transitioned`, NOT `synced`, to answer "did this recover anything".
 * `synced` counts legs whose ERP-facing work completed, which stays true for a
 * settled payment whose invoice was already `paid` — so a repeat call reports
 * the same `synced` and `transitioned: 0`. The route's own docstring says so.
 */
export interface RunErpSyncResult {
	/** The run the pass ran for (echoed back as a string uuid). */
	id: string;
	/** Legs whose ERP-facing work completed. True again on a repeat call. */
	synced: number;
	/** Invoices actually moved `payment_scheduled → paid`. The success number. */
	transitioned: number;
	/** Legs the pass declined to act on (payment not `completed`, invoice past
	 *  `payment_scheduled`) — the idempotency in action, not a failure. */
	skipped: number;
	/** Legs whose settlement doesn't cover the invoice, so it stays held. Those
	 *  exit via `acceptPaymentSettlement` (or a void), not via another sync. */
	held: number;
	/** Legs that raised again. Each keeps its `erp_reconciliation` exception. */
	failed: number;
}

/**
 * Re-run the ERP sync-back for a run whose settled payments never landed.
 *
 * The exit for an invoice stranded at `payment_scheduled` after a
 * `payment_erp_sync` leg failed. Idempotent by construction (the pass skips
 * every non-`completed` payment and every invoice past `payment_scheduled`) and
 * moves no money. 409s when the run has no settled payments at all.
 *
 * Voiding is NOT the exit for that state: it returns the invoice to `approved`
 * and invites a second payment for money that already left.
 */
export function retryRunErpSync(runId: string): Promise<RunErpSyncResult> {
	return api.post<RunErpSyncResult>(`/api/payments/runs/${runId}/sync-erp`, {});
}

/**
 * Declare a short / unverifiable settlement final and release the invoice.
 *
 * The other exit from the under-settlement hold: the rail settled less than AP
 * authorized (or in a currency we never authorized), so `settlement_coverage`
 * holds the invoice at `payment_scheduled` rather than reporting it settled in
 * full. Accepting moves it to `paid` and records `reason` on the immutable
 * trail. Irreversible; the money stays where it landed.
 *
 * 409s when the settlement already covers the invoice ("nothing to accept") and
 * when the invoice is no longer held — the backend is the authority on both, so
 * surface its `detail` rather than pre-judging it client-side.
 */
export function acceptPaymentSettlement(paymentId: string, reason: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/settlement/accept`, { reason });
}

/**
 * Void a completed or in-flight payment; the invoice returns to `approved`.
 *
 * Typed because the response is load-bearing beyond "it worked": for a
 * `virtual_card` payment it carries `void_card_disposition`, the verdict on
 * whether the card was actually closed at the provider. Both the rail reversal
 * and the card close are best-effort (a provider outage must not block the
 * accounting void), so a bare 200 does NOT mean the card is shut — read the
 * disposition, and offer `retryVoidCardCancel` on `not_closed_retryable`.
 */
export function voidPayment(paymentId: string, reason: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/void`, { reason });
}

/**
 * Re-attempt ONLY the card close for an already-voided card payment.
 *
 * The remedy sits on the void rather than beside it (`docs/decisions.md` §96,
 * §132): `POST /api/cards/{id}/cancel` would also close the card, but it is
 * reachable on a LIVE payment, where it kills the card while the payment and
 * its invoice still claim money is in flight. This one 409s on anything but an
 * already-`voided` card payment, so it can only ever finish a reversal.
 *
 * Idempotent — a second retry on an already-closed card returns
 * `card_already_cancelled` (disposition `closed`), not an error. Moves no
 * money, does not re-ask the payment rail, and does not re-void.
 */
export function retryVoidCardCancel(paymentId: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/void/retry-card-cancel`, {});
}
