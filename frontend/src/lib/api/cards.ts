// Typed helpers for the virtual-card REBATE lifecycle — the three endpoints
// that let AP record what the card processor did out-of-band. Everything routes
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce), like every other `#lib/api/*` module.
//
// All three are `require_roles(admin, ap_manager, cfo)` on the server — a ROLE
// gate, not one of the granular `payment.*` permissions the recovery exits in
// `./payments.ts` use, because a rebate is bookkeeping rather than a money
// movement. The UI mirrors that with `auth.hasAnyRole(...)` so a clerk never
// sees a control that can only 403.
//
// **None of these moves money.** The card settlement webhook creates every
// rebate at `pending`; Lithic/Nium confirm and pay rebates on a periodic
// statement, not on an event we ingest, so `confirm` / `mark-paid` record a
// confirmation and a payout that already happened elsewhere. Neither requests a
// payout, and neither pays anyone.
//
// See `backend/docs/virtual-cards.md` § Rebate status lifecycle.
import { api } from '#lib/api.ts';
import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import type { CardRebate, RebateListResponse } from '#lib/types/cardRebate.ts';
import {
	CARDABLE_INVOICE_STATUSES,
	type CardGenerateResponse,
	type CardGenerateSkip
} from '#lib/types/cardGenerate.ts';
import type { InvoicePickerOption } from '#lib/utils/invoicePicker.ts';
import type { SearchPickerPage } from '#lib/utils/searchPicker.ts';

/** Mirrors `backend/app/api/pagination.py::DEFAULT_PAGE_SIZE`, so a bare call
 *  and the server's own default return the same rows. */
const DEFAULT_PAGE_SIZE = 20;

/**
 * One PAGE of the tenant's card rebates, newest first.
 *
 * Entity-scoped server-side through the `VirtualCard` join (the `X-Entity-ID`
 * header the shared client already sends), and paginated on the canonical
 * `page` / `page_size` contract like every other list here — the table it backs
 * grows by one row per settled card, forever.
 *
 * Two totals, deliberately named apart: `total` is the row COUNT of the whole
 * filtered set (what Load-more counts against) and `total_amount` is the summed
 * money over that same whole set, never over the page returned here.
 */
export function listCardRebates(
	opts: { period?: string; page?: number; pageSize?: number } = {}
): Promise<RebateListResponse> {
	const params = new URLSearchParams({
		page: String(opts.page ?? 1),
		page_size: String(opts.pageSize ?? DEFAULT_PAGE_SIZE)
	});
	if (opts.period) params.set('period', opts.period);
	return api.get<RebateListResponse>(`/api/cards/rebates?${params}`);
}

/**
 * Record the processor's confirmation — `pending` → `confirmed`.
 *
 * 409s from any other status (the lifecycle has no skips and no reversals) and
 * 404s on an unknown rebate. Writes an append-only `card_rebate.confirmed`
 * audit row. Moves no money.
 */
export function confirmCardRebate(rebateId: string): Promise<CardRebate> {
	return api.post<CardRebate>(`/api/cards/rebates/${rebateId}/confirm`, {});
}

/**
 * Record that the processor's payout landed — `confirmed` → `paid_out`.
 *
 * Requires `confirmed` first: a rebate cannot be recorded paid before it was
 * confirmed to exist. 409s otherwise. Writes an append-only
 * `card_rebate.paid_out` audit row. Moves no money — the payout it records
 * happened at the processor.
 */
export function markCardRebatePaid(rebateId: string): Promise<CardRebate> {
	return api.post<CardRebate>(`/api/cards/rebates/${rebateId}/mark-paid`, {});
}

/**
 * Mint a virtual card directly for each invoice (`POST /api/cards/generate`,
 * `payment.execute`). **This moves money** — a minted card is spendable — and
 * the server runs every gate a payment run does. `items` are the cards this
 * call minted; `skipped` (normalized to `[]` for an older backend) names every
 * other invoice and why, so a retry of a mint that already succeeded reads as
 * `card_skip_already_carded` with the card's id, never as a bare `total: 0`.
 * A 409 `invoice_locked` (nothing minted, at least one lock timed out) is
 * thrown as an `ApiError` like any other refusal.
 */
export async function generateCards(invoiceIds: string[]): Promise<CardGenerateResponse> {
	const body = await api.post<CardGenerateResponse>('/api/cards/generate', {
		invoice_ids: invoiceIds
	});
	return { ...body, skipped: body.skipped ?? [] };
}

/** One skip, in the reader's language — the server's English for a code this
 *  build predates. `m` is passed in so this stays usable from pure modules. */
export function cardGenerateSkipText(
	skip: CardGenerateSkip,
	m: (key: MessageKey, params?: Record<string, string | number>) => string
): string {
	return localizeApiDetail(skip, m) ?? skip.message;
}

/**
 * One page of the invoices the Generate dialog's picker offers — a server-paged,
 * server-searched read of `GET /api/invoices` narrowed to the statuses the mint
 * accepts (`CARDABLE_INVOICE_STATUSES`). It does not pre-judge the other gates
 * (an open exception, a live payment, an unverified vendor): the mint names
 * each of those per invoice in `skipped`, which is the point of the dialog.
 * Entity-scoped by the shared client's `X-Entity-ID`, like the mint itself.
 */
export async function listCardableInvoices(query: {
	search: string;
	page: number;
	page_size: number;
}): Promise<SearchPickerPage<InvoicePickerOption>> {
	const qs = new URLSearchParams({
		status: CARDABLE_INVOICE_STATUSES.join(','),
		page: String(query.page),
		page_size: String(query.page_size)
	});
	const search = query.search.trim();
	if (search) qs.set('search', search);
	const res = await api.get<{ items: InvoicePickerOption[]; total: number }>(`/api/invoices?${qs}`);
	return { items: res.items ?? [], total: res.total ?? 0 };
}
