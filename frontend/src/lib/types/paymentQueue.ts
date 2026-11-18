/**
 * The payment queue's `blocked_reason` codes (`GET /api/payments/queue`).
 *
 * A row's `blocked_reason` is a stable, PII-free code from the backend's fixed
 * vocabulary: a `PAYMENT_BLOCKING_EXCEPTION_TYPES` member, or one of
 * `services/payment_runs`' own `REFUSAL_*` constants. The run builder 409s on
 * the same verdict, so each code here is a refusal the operator would hit.
 * `paymentQueue.test.ts` reads both backend lists and fails when a code has no
 * key here, so a new refusal cannot reach the page as the generic sentence.
 *
 * The mobile twin is `_blockedReasonLabel` in `mobile/lib/screens/payment_queue_screen.dart`.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

export const QUEUE_BLOCKED_REASON_KEYS: Record<string, MessageKey> = {
	duplicate: 'payments.queue.blocked.duplicate',
	fraud_flag: 'payments.queue.blocked.fraudFlag',
	line_total_mismatch: 'payments.queue.blocked.lineTotalMismatch',
	// A row held because an earlier payment's fate at the rail is unknown.
	payment_reconciliation: 'payments.queue.blocked.paymentReconciliation',
	quality_hold: 'payments.queue.blocked.qualityHold',
	po_mismatch: 'payments.queue.blocked.poMismatch',
	vendor_not_active: 'payments.queue.blocked.vendorNotActive',
	// The invoice records no currency; no rail can pay it until one is set.
	currency_unknown: 'payments.queue.blocked.currencyUnknown',
	applied_credit_mismatch: 'payments.queue.blocked.appliedCreditMismatch',
	fully_credited: 'payments.queue.blocked.fullyCredited',
	// Not a block: the row is pinned to the card rail, and its own sentence
	// says so rather than the "can't be paid" generic.
	live_virtual_card: 'payments.queue.blocked.liveVirtualCard'
	// `live_payment` is absent on purpose: an invoice a live payment holds is
	// not a queue row at all (`_queue_base_where`), so the queue never sends it.
};

/** The message key for a row's refusal code. An unrecognised or missing code
 *  falls back to the generic reason rather than rendering a raw identifier at
 *  the operator. */
export function queueBlockedReasonKey(code: string | null | undefined): MessageKey {
	return (code && QUEUE_BLOCKED_REASON_KEYS[code]) || 'payments.queue.blocked.generic';
}
