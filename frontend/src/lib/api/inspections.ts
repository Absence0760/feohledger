// Typed helpers for the Quality Inspection endpoints (`/api/inspections`) —
// the 4th leg of 4-way matching. All requests route through the shared `api`
// client (Bearer + X-Tenant-Slug + X-Entity-ID + 401-bounce).
//
// RBAC mirrors `backend/app/api/inspections.py`:
//   - `GET /api/inspections` + `GET /api/inspections/{id}` — any authenticated
//     user (`get_current_user`). The list is entity-scoped; the detail is not.
//   - `POST /api/inspections` + `POST /api/inspections/sync` — admin /
//     ap_manager (`require_roles`). `/goods-receipts` gates its controls on the
//     same split via `auth.isManager`; the backend is authoritative regardless.
//
// The list endpoint is paginated on the canonical `page` / `page_size`
// contract and accepts a `gr_id` filter; see {@link listInspections}.
import { api } from '#lib/api.ts';
import type { PagedResponse } from '#lib/utils/pagination.ts';

/** Mirrors `backend/app/api/pagination.py::DEFAULT_PAGE_SIZE`, so a bare call
 *  and the server's own default return the same rows. */
const DEFAULT_PAGE_SIZE = 20;

/** The result vocabulary `po_matching` acts on. Mirrors
 *  `backend/app/schemas/inspection.py::VALID_RESULTS` — the API validates the
 *  submitted value against that set and 400s on anything else, so the form
 *  must not offer a fourth option. */
export type InspectionResult = 'pass' | 'fail' | 'partial';

/** Rendering order for the result picker: the clean outcome first, then the
 *  two that change the match. */
export const INSPECTION_RESULTS: readonly InspectionResult[] = ['pass', 'fail', 'partial'];

export function isInspectionResult(value: string): value is InspectionResult {
	return (INSPECTION_RESULTS as readonly string[]).includes(value);
}

/**
 * One row as `app/api/inspections.py::_serialize` returns it.
 *
 * `result` is typed `string`, not `InspectionResult`: a row can also arrive
 * from the QMS sync, and while `qms_sync.normalize_disposition` refuses
 * anything outside the vocabulary today, the column itself is free-form. The
 * page resolves an unknown value to a neutral badge rather than mis-tinting it.
 *
 * The quantities come back as JSON numbers (the backend `float()`s its
 * `Numeric(12, 4)` columns at the serializer). They are counts of goods, not
 * money — no `Decimal` invariant applies to them. Writes still go up as
 * strings; see `InspectionCreateBody`.
 */
export interface Inspection {
	id: string;
	inspection_number: string;
	po_id: string | null;
	gr_id: string | null;
	/**
	 * The goods receipt's human-readable number, resolved server-side by an
	 * outer join. `null` when the inspection is tied to no receipt — a different
	 * fact from "linked to a receipt we could not name", which is why the page
	 * renders the two differently.
	 *
	 * It exists because the row used to carry `gr_id` alone: `/goods-receipts`
	 * fetched a 100-row page of receipts alongside every inspection load purely
	 * to build an id→number map, and any receipt outside that window still
	 * rendered unlabelled.
	 */
	gr_number: string | null;
	result: string;
	inspected_date: string | null;
	inspector: string | null;
	accepted_quantity: number | null;
	rejected_quantity: number | null;
	deviation_notes: string | null;
	status: string;
	created_at: string;
}

/**
 * The `POST /api/inspections` body (`schemas/inspection.py::InspectionCreate`).
 *
 * The quantities are sent as STRINGS. Pydantic parses them straight into the
 * `Decimal(12, 4)` the column stores, so a value typed into the form reaches
 * the database as the digits the inspector typed — a JSON number would round-
 * trip through a float first. `accepted_quantity` is what the matcher renders
 * into its "Partial acceptance: N of ordered quantity accepted" issue, so the
 * figure is read by a human downstream.
 */
export interface InspectionCreateBody {
	inspection_number: string;
	gr_id?: string;
	po_id?: string;
	result: InspectionResult;
	inspected_date?: string;
	inspector?: string;
	accepted_quantity?: string;
	rejected_quantity?: string;
	deviation_notes?: string;
}

/** What `POST /api/inspections/sync` answers with — the counts
 *  `services/qms_sync.sync_tenant_inspections` returns verbatim.
 *
 *  `unchanged` and `skipped` matter as much as the other three: `unchanged` is
 *  the difference between "the sync is doing nothing" and "the sync has
 *  nothing to do", and `skipped` counts records whose disposition never mapped
 *  onto pass/fail/partial — those land NO inspection row at all, so a sync that
 *  reports only `created`/`updated` would read as a clean run while the QMS's
 *  rejections were silently dropped. */
export interface InspectionSyncResult {
	fetched: number;
	created: number;
	updated: number;
	unchanged: number;
	skipped: number;
}

/**
 * One PAGE of the tenant's quality inspections, newest first.
 *
 * Paginated like every other list here — the table it backs only ever grows
 * (one row per inspection recorded or synced from the QMS). `grId` narrows to a
 * single goods receipt, which is what the receipt detail modal wants: it used
 * to load the whole list and filter it in the browser because the server
 * offered no way to ask.
 */
export function listInspections(
	opts: { grId?: string; page?: number; pageSize?: number } = {}
): Promise<PagedResponse<Inspection>> {
	const params = new URLSearchParams({
		page: String(opts.page ?? 1),
		page_size: String(opts.pageSize ?? DEFAULT_PAGE_SIZE)
	});
	if (opts.grId) params.set('gr_id', opts.grId);
	return api.get<PagedResponse<Inspection>>(`/api/inspections?${params}`);
}

export function getInspection(id: string): Promise<Inspection> {
	return api.get<Inspection>(`/api/inspections/${id}`);
}

export function createInspection(body: InspectionCreateBody): Promise<Inspection> {
	return api.post<Inspection>('/api/inspections', body);
}

/**
 * Pull inspections from the org's configured QMS. Idempotent — the upsert is
 * keyed on `(organization_id, inspection_number)`, so a repeat call updates in
 * place and reports the re-fetched rows as `unchanged` rather than duplicating
 * them.
 *
 * Deliberately a FULL re-pull: the route passes no `since` cursor and advances
 * none (that is the background sweep's job), because a human asking to sync now
 * usually suspects the incremental window missed something.
 *
 * Refuses with 409 — surfaced by the shared client as an `ApiError` carrying
 * the backend's own `detail` — when the org has no `settings.qms` block, or
 * names a provider with no registered adapter. Both are configuration states
 * the caller must be told about; neither is an empty result.
 *
 * The route takes no request body; the empty object is only what `api.post`
 * serializes, and FastAPI ignores it.
 */
export function syncInspections(): Promise<InspectionSyncResult> {
	return api.post<InspectionSyncResult>('/api/inspections/sync', {});
}
