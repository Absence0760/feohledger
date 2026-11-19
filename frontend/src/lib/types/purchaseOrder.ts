import type { MessageKey } from '#lib/i18n/messages.ts';
import { refusalText, type ActionRefusal } from '#lib/api/actionVerdicts.ts';

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
 * The supplier portal's PO-flip refusals (`backend/app/api/portal.py::
 * po_flip_refusal`), carried per row as `flip_verdict` and answered by
 * `POST /api/portal/purchase-orders/{id}/flip`.
 */
export const PO_NOT_INVOICEABLE = 'po_not_invoiceable';
export const PO_TOTAL_UNVERIFIABLE = 'po_total_unverifiable';

/** The portal PO list / detail row's verdict on "Create invoice" (issue #575).
 *  `undefined` (an older backend, or not loaded) is a refusal: fail closed. */
export interface PortalPOFlipVerdict {
	flip_verdict?: ActionRefusal;
}

/**
 * The sentence for a flip refusal, in the reader's language when this build
 * knows the code, else the server's English (`refusalText`). The unvouched
 * total has its own note above the portal table, so it is stated there.
 */
export function poFlipRefusalText(refusal: ActionRefusal | undefined, t: Translate): string {
	if (!refusal) return '';
	if (refusal.code === PO_NOT_INVOICEABLE) return t('portal.po.refusal.notInvoiceable');
	if (refusal.code === PO_TOTAL_UNVERIFIABLE) return t('portal.po.totalUnverifiableNote');
	return refusalText(refusal, t);
}
