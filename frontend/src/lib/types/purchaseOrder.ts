import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * Purchase-order status labels — the one map every page that shows a PO's
 * status reads, so `open` (a database token) reads "Open" and translates.
 *
 * `PurchaseOrder.status` is a free-form column written by the ERP feed and the
 * requisition conversion (`backend/app/models/procurement.py`), so a status not
 * listed here is shown as-is rather than guessed at: {@link poStatusLabel}.
 */
export const PO_STATUS_LABEL_KEYS = {
	open: 'purchaseOrders.status.open',
	// Not approved or issued in the ERP yet (decisions §394). The portal never
	// sees one; the internal list does.
	unissued: 'purchaseOrders.status.unissued',
	partially_received: 'purchaseOrders.status.partiallyReceived',
	received: 'purchaseOrders.status.received',
	closed: 'purchaseOrders.status.closed',
	cancelled: 'purchaseOrders.status.cancelled',
	voided: 'purchaseOrders.status.voided'
} as const satisfies Record<string, MessageKey>;

/** The catalogue key for a PO status, or `null` for one this build has no label for. */
export function poStatusLabelKey(status: string | null | undefined): MessageKey | null {
	return (PO_STATUS_LABEL_KEYS as Record<string, MessageKey>)[status ?? ''] ?? null;
}

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** A PO status as a reader sees it: localized when known, else the raw value. */
export function poStatusLabel(status: string, t: Translate): string {
	const key = poStatusLabelKey(status);
	return key ? t(key) : status;
}
