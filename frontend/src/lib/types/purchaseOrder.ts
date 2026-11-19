import type { MessageKey } from '#lib/i18n/messages.ts';
import type { BadgeTone } from '#lib/components/ui/badgeTone.ts';

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

/**
 * Badge tone per PO status — shared by `/purchase-orders` and the supplier
 * portal's PO list, so one PO cannot read in two colours.
 *
 * `cancelled` keeps the tinted grey (`muted`), distinct from the flat
 * `neutral` an unrecognised status falls back to: a cancelled PO is a decision
 * somebody made, an unknown status is one nobody has told us about.
 * `PurchaseOrder.status` is a free-form column, hence the fallback.
 */
export const PO_STATUS_TONES: Record<string, BadgeTone> = {
	open: 'accent',
	// Not approved or issued in the ERP yet: it syncs, but nothing matches
	// against it until the ERP issues it (decisions §394).
	unissued: 'warning',
	closed: 'success',
	cancelled: 'muted'
};

export function poStatusTone(status: string | null | undefined): BadgeTone {
	const key = status ?? '';
	return Object.hasOwn(PO_STATUS_TONES, key) ? PO_STATUS_TONES[key] : 'neutral';
}
