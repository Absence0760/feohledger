import { api } from '$lib/api';
import type { EligibleInvoice } from '$lib/types/creditMemo';
import type { SearchPickerPage } from '$lib/utils/searchPicker';

export type { EligibleInvoice };

type PageQuery = { search: string; page: number; page_size: number };

function pageParams(query: PageQuery, extra: Record<string, string> = {}): URLSearchParams {
	const qs = new URLSearchParams(extra);
	const search = query.search.trim();
	if (search) qs.set('search', search);
	qs.set('page', String(query.page));
	qs.set('page_size', String(query.page_size));
	return qs;
}

/**
 * The invoices `POST /api/credit-memos/{id}/apply` will accept for this memo —
 * `GET /api/credit-memos/{id}/eligible-invoices`. The server reads the memo's
 * vendor, entity, currency and amount off the row itself, so the set cannot
 * describe a memo someone has since edited (`docs/decisions.md` §202).
 */
export async function listInvoicesEligibleForMemo(
	memoId: string,
	query: PageQuery
): Promise<SearchPickerPage<EligibleInvoice>> {
	const res = await api.get<{ items: EligibleInvoice[]; total: number }>(
		`/api/credit-memos/${encodeURIComponent(memoId)}/eligible-invoices?${pageParams(query)}`
	);
	return { items: res.items ?? [], total: res.total ?? 0 };
}

/**
 * The invoices a NEW memo for `vendorId` can be linked to at creation —
 * `GET /api/credit-memos/eligible-invoices`. `amount`, once the form holds a
 * valid one, narrows to invoices that can still absorb it; pass `null` before
 * then (see `utils/invoicePicker.ts::creditAmountParam`).
 */
export async function listInvoicesEligibleForNewMemo(
	vendorId: string,
	amount: string | null,
	query: PageQuery
): Promise<SearchPickerPage<EligibleInvoice>> {
	const extra: Record<string, string> = { vendor_id: vendorId };
	if (amount) extra.amount = amount;
	const res = await api.get<{ items: EligibleInvoice[]; total: number }>(
		`/api/credit-memos/eligible-invoices?${pageParams(query, extra)}`
	);
	return { items: res.items ?? [], total: res.total ?? 0 };
}
