/**
 * Goods-receipt status semantics.
 *
 * `GoodsReceipt.status` is a free-form `String(30)` on the backend and nothing
 * normalises it on the way in, so the frontend cannot rely on an enum. What it
 * CAN rely on is the set the backend itself treats as "this delivery did not
 * happen": `services/po_matching.CANCELLED_GR_STATUSES`, which excludes those
 * receipts from the 3-way quantity leg.
 *
 * The page used to badge every status `success`, so a reversed receipt read as
 * a successful delivery — the same row the matcher was deliberately ignoring.
 */

/** Mirrors `backend/app/services/po_matching.py::CANCELLED_GR_STATUSES`.
 *
 *  Both spellings of "cancelled" are listed because the column is free-form and
 *  neither is normalised. Compared case-insensitively, as the backend does.
 *  `goodsReceipt.test.ts` fails if this drifts from the backend set. */
export const CANCELLED_GR_STATUSES = new Set([
	'cancelled',
	'canceled',
	'void',
	'voided',
	'reversed'
]);

/** True when the backend would exclude this receipt from PO matching. */
export function isCancelledGoodsReceipt(status: string | null | undefined): boolean {
	return CANCELLED_GR_STATUSES.has((status ?? '').trim().toLowerCase());
}

/** The `<Badge>` tone for a goods-receipt status.
 *
 *  `muted` rather than `danger` for a cancellation: a reversed receipt is a
 *  decision someone made, not a failure — the same distinction `/purchase-orders`
 *  draws between `cancelled` and a status nobody told us about. */
export function goodsReceiptTone(status: string | null | undefined): 'success' | 'muted' {
	return isCancelledGoodsReceipt(status) ? 'muted' : 'success';
}

/** Mirrors `backend/app/api/goods_receipts.py::RECEIPT_ENTRY_ROLES`. Receiving
 *  is entry work, so the clerk is in it; what stops anyone releasing their own
 *  invoice with a receipt is the server-side recorder check, not this list. */
export const RECEIPT_ENTRY_ROLES = ['admin', 'ap_manager', 'ap_clerk'] as const;

/** `GoodsReceipt.source` for a receipt recorded in FeohLedger — the only kind
 *  that can be cancelled here. Mirrors `models/procurement.GR_SOURCE_MANUAL`. */
export const GR_SOURCE_MANUAL = 'manual';

/** Mirrors `services/goods_receipts.CANCELLED_PO_STATUSES` — a PO nothing can be
 *  received against. The picker still lists such a PO (hiding it would read as
 *  "no such PO"), and the form explains why it cannot be received. */
const CANCELLED_PO_STATUSES = new Set(['cancelled', 'canceled', 'void', 'voided']);

export function isCancelledPO(status: string | null | undefined): boolean {
	return CANCELLED_PO_STATUSES.has((status ?? '').trim().toLowerCase());
}

/** What is still outstanding on a PO line — never negative (an over-receipt
 *  leaves nothing outstanding, it does not owe the supplier units back).
 *
 *  Worked in ten-thousandths, the column's scale, so `10 - 9.3` is `0.7` and
 *  not `0.6999999999999993` — this figure pre-fills an input the user submits. */
export function remainingQuantity(line: { quantity: number | null; quantity_received: number }): number {
	if (line.quantity === null) return 0;
	const scaled = Math.round(line.quantity * 10_000) - Math.round(line.quantity_received * 10_000);
	return Math.max(0, scaled) / 10_000;
}
