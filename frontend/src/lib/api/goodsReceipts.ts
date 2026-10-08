// Typed helpers for recording and cancelling goods receipts
// (`/api/goods-receipts`) — the 3-way matching leg — plus the two purchase-order
// reads the receipt form needs. All requests route through the shared `api`
// client (Bearer + X-Tenant-Slug + X-Entity-ID + 401-bounce).
//
// RBAC mirrors `backend/app/api/goods_receipts.py`:
//   - reads — any authenticated user;
//   - `POST /api/goods-receipts` + `POST /api/goods-receipts/{id}/cancel` —
//     `RECEIPT_ENTRY_ROLES` (admin / ap_manager / ap_clerk). `/goods-receipts`
//     gates its controls on {@link RECEIPT_ENTRY_ROLES}; the backend is
//     authoritative regardless.
import { api } from '#lib/api.ts';
import type { PagedResponse } from '#lib/utils/pagination.ts';

export { RECEIPT_ENTRY_ROLES, GR_SOURCE_MANUAL } from '#lib/types/goodsReceipt.ts';

export interface GoodsReceiptLine {
	id: string;
	po_line_item_id: string | null;
	description: string | null;
	quantity_received: number | null;
}

export interface GoodsReceiptDetail {
	id: string;
	gr_number: string;
	po_id: string | null;
	po_number: string | null;
	received_date: string | null;
	status: string;
	/** `manual` = recorded in FeohLedger; `null` = arrived some other way. */
	source: string | null;
	line_items: GoodsReceiptLine[];
	created_at: string;
}

/**
 * The `POST /api/goods-receipts` body (`schemas/goods_receipt.py`).
 *
 * Quantities go up as STRINGS, so the digits typed reach the `Numeric(12, 4)`
 * column without a float round trip — the same rule the inspection form keeps.
 */
export interface GoodsReceiptCreateBody {
	po_id: string;
	received_date: string;
	gr_number?: string;
	lines: { po_line_item_id?: string; description?: string; quantity_received: string }[];
}

/** One PO as the receipt form's picker lists it (`GET /api/purchase-orders`). */
export interface ReceivablePO {
	id: string;
	po_number: string;
	vendor_name: string | null;
	status: string;
}

/** A PO line with what has arrived against it (`GET /api/purchase-orders/{id}`).
 *  Quantities are counts of goods, not money — JSON numbers are fine here. */
export interface ReceivablePOLine {
	id: string;
	description: string | null;
	quantity: number | null;
	quantity_received: number;
}

export interface ReceivablePODetail extends ReceivablePO {
	line_items: ReceivablePOLine[];
	quantity_received_total: number;
}

/**
 * Record a delivery. `idempotencyKey` is minted once per opened form, so a
 * double-click or a retry after a dropped response replays the same receipt
 * (the server answers 200 with it) instead of booking the delivery twice.
 */
export function createGoodsReceipt(
	body: GoodsReceiptCreateBody,
	idempotencyKey: string
): Promise<GoodsReceiptDetail> {
	return api.post<GoodsReceiptDetail>('/api/goods-receipts', body, {
		'Idempotency-Key': idempotencyKey
	});
}

export function cancelGoodsReceipt(id: string): Promise<GoodsReceiptDetail> {
	return api.post<GoodsReceiptDetail>(`/api/goods-receipts/${id}/cancel`, {});
}

/** Purchase orders matching `search` (PO number), for the form's picker. */
export function searchReceivablePOs(search: string, pageSize = 50): Promise<PagedResponse<ReceivablePO>> {
	const params = new URLSearchParams({ page: '1', page_size: String(pageSize) });
	if (search.trim()) params.set('search', search.trim());
	return api.get<PagedResponse<ReceivablePO>>(`/api/purchase-orders?${params}`);
}

export function getReceivablePO(id: string): Promise<ReceivablePODetail> {
	return api.get<ReceivablePODetail>(`/api/purchase-orders/${id}`);
}
