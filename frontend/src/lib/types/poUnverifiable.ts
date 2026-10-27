import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * Why a purchase order's total is "not verifiable": the
 * `erp_adapters.base.PoSkipReason` code the ERP PO sync stamped on
 * `purchase_orders.total_unverifiable_reason` (decisions §323).
 *
 * Keyed on the code, never on server prose. `poUnverifiable.test.ts` reads the
 * backend enum, so a new skip reason fails here until it has a sentence.
 */
export const PO_UNVERIFIABLE_REASON_KEYS = {
	unpriceable: 'purchaseOrders.unverifiableReason.unpriceable',
	lines_unread: 'purchaseOrders.unverifiableReason.linesUnread',
	no_lines: 'purchaseOrders.unverifiableReason.noLines',
	lines_changed: 'purchaseOrders.unverifiableReason.linesChanged',
	local_currency_unknown: 'purchaseOrders.unverifiableReason.localCurrencyUnknown'
} as const satisfies Record<string, MessageKey>;

/** The catalogue key for a reason code; an unknown code (a newer backend) gets the generic one. */
export function poUnverifiableReasonKey(code: string | null | undefined): MessageKey {
	return (
		(PO_UNVERIFIABLE_REASON_KEYS as Record<string, MessageKey>)[code ?? ''] ??
		'purchaseOrders.unverifiableReason.unknown'
	);
}
