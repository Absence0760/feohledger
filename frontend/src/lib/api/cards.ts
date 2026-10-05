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
import type { CardRebate, RebateListResponse } from '#lib/types/cardRebate.ts';

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
