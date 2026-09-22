/**
 * Load-more append for offset-paginated lists.
 *
 * Append the next page onto the already-loaded rows, skipping any id already
 * present. Offset pagination can re-surface a row when the underlying set
 * shifts between fetches (e.g. a new invoice inserted, a notification arriving
 * between page loads); a duplicate id would crash the keyed
 * `{#each ... (item.id)}` rendering the list (Svelte 5 `each_key_duplicate`).
 *
 * Semantics (matching the original invoice-store inline guard):
 * - The existing row wins — an incoming duplicate is dropped, never replacing
 *   the already-rendered row.
 * - Order is preserved: existing rows first (in order), then the incoming
 *   page's new rows in their server order.
 */
export function appendUnique<T extends { id: string }>(existing: T[], incoming: T[]): T[] {
	const seen = new Set(existing.map((item) => item.id));
	return [...existing, ...incoming.filter((item) => !seen.has(item.id))];
}

/**
 * Response shape of every list endpoint's `/ids` sibling (`GET
 * /api/invoices/ids`, `/api/exceptions/ids`, `/api/expenses/ids` —
 * `backend/app/api/pagination.py::MatchingIdsResponse`).
 *
 * Backs the "select all N matching" affordance: a list page's header
 * checkbox only ever selects the currently-LOADED page of rows (the bulk
 * endpoints take an explicit id list, and the page only has what it fetched
 * client-side) — this is the whole filtered set instead, resolved
 * server-side so a bulk action can't silently skip rows past the first page.
 * `truncated` is true when the match count exceeded the server's cap
 * (`MAX_SELECT_ALL_IDS`), so a partial selection is never presented as
 * complete.
 */
export interface MatchingIdsResponse {
	ids: string[];
	total: number;
	truncated: boolean;
}

/** Envelope every paginated list endpoint returns (`backend/app/api/pagination.py`). */
export interface PagedResponse<T> {
	items: T[];
	total: number;
}

/**
 * The server's `MAX_PAGE_SIZE` (`backend/app/api/pagination.py`). A request for
 * more is clamped, so asking for more is not a way to skip paging.
 */
export const MAX_PAGE_SIZE = 100;
