/**
 * Typed helpers for the scheduled-report admin surface. Routes through the
 * shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never raw fetch.
 * Backend: `backend/app/api/analytics.py` (JWT + RBAC gated).
 *
 * RBAC, mirrored by the UI: read is `admin` + `cfo`; create / patch / delete
 * are `admin` ONLY (the backend 403s the rest, so the panel gates the mutate
 * controls on `auth.isAdmin` rather than letting a CFO click into a 403).
 *
 * `X-Entity-ID` rides every request from `api.ts` but the scheduled-report
 * routes do NOT honour it — a schedule is whole-tenant by construction. That is
 * why the panel shows no entity selector: there is nothing for it to scope.
 *
 * Errors: every helper lets the shared `ApiError` propagate. `api.ts` already
 * runs the FastAPI `detail` through `formatApiDetail`, which renders a 422
 * validation list as `field: msg` and reads ONLY `loc` + `msg` — never `input`.
 * That matters here: `input` on a recipients failure is the submitted address
 * list, and echoing it would put a recipient's email address into a toast. So a
 * caller surfaces `e.message` as-is; there is no per-message mapping table to
 * drift from the backend's own wording.
 */
import { api } from '#lib/api.ts';
import type {
	ScheduledReport,
	ScheduledReportCreate,
	ScheduledReportList,
	ScheduledReportPatch
} from '#lib/types/scheduledReport.ts';

const BASE = '/api/analytics/scheduled-reports';

/**
 * This tenant's schedules plus the two vocabularies (`report_types`,
 * `cadences`) that drive the create / edit selects.
 *
 * A 404 from THIS call is unambiguous: a list route cannot 404 on data, so it
 * means the surface is not mounted in this deployment. The caller renders a
 * "not available" empty state instead of an error (see the panel).
 */
export function listScheduledReports(): Promise<ScheduledReportList> {
	return api.get<ScheduledReportList>(BASE);
}

/** Create a schedule. Returns the SAVED row — whose `recipients` may be
 *  shorter than the submitted list, because the backend de-dupes them
 *  case-insensitively rather than rejecting duplicates. Render from this, never
 *  from what was typed. */
export function createScheduledReport(body: ScheduledReportCreate): Promise<ScheduledReport> {
	return api.post<ScheduledReport>(BASE, body);
}

/**
 * Patch a schedule — any subset; only what is sent changes.
 *
 * `{enabled: true}` is also the single call that recovers an auto-disabled
 * schedule: it re-enables AND clears `last_run_status` / `last_run_error`, so
 * the "Re-enable" affordance needs no second request.
 *
 * Same de-dupe caveat as create: the returned `recipients` is the saved list.
 */
export function updateScheduledReport(
	id: string,
	body: ScheduledReportPatch
): Promise<ScheduledReport> {
	return api.patch<ScheduledReport>(`${BASE}/${id}`, body);
}

/** Delete a schedule (204). Unknown / other-tenant id is an opaque 404. */
export function deleteScheduledReport(id: string): Promise<void> {
	return api.delete(`${BASE}/${id}`);
}
