// Typed helpers for the Custom Report Builder surface. All requests route
// through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce). See `#lib/types/reports` for the response contracts and the
// authoritative API contract for the endpoint shapes.
//
// The client only ever sends catalog keys — never raw SQL / column / table
// names (the backend maps keys → whitelisted columns and 422s anything else).
import { api } from '#lib/api.ts';
import type {
	CreateReportBody,
	ReportCatalog,
	ReportDefinition,
	ReportListResponse,
	ReportResult,
	ReportSpec,
	UpdateReportBody
} from '#lib/types/reports.ts';

/** The field catalog that drives the whole builder UI (data sources +
 *  dimensions / measures / filters). Read by all four roles. */
export function getReportCatalog(): Promise<ReportCatalog> {
	return api.get<ReportCatalog>('/api/reports/catalog');
}

/** Saved report definitions for the active tenant / entity. Read by all roles. */
export function listReports(): Promise<ReportListResponse> {
	return api.get<ReportListResponse>('/api/reports');
}

/** A single saved definition. */
export function getReport(id: string): Promise<ReportDefinition> {
	return api.get<ReportDefinition>(`/api/reports/${id}`);
}

/** Persist a spec as a named report (admin/ap_manager/cfo). The backend
 *  validates the spec against the catalog before saving. */
export function createReport(body: CreateReportBody): Promise<ReportDefinition> {
	return api.post<ReportDefinition>('/api/reports', body);
}

/** Update a saved definition's spec / name / description (admin/ap_manager/cfo). */
export function updateReport(id: string, body: UpdateReportBody): Promise<ReportDefinition> {
	return api.patch<ReportDefinition>(`/api/reports/${id}`, body);
}

/** Delete a saved definition (admin/ap_manager/cfo). */
export function deleteReport(id: string): Promise<void> {
	return api.delete(`/api/reports/${id}`);
}

export interface RunOptions {
	page?: number;
	page_size?: number;
}

/** Run an ad-hoc (unsaved) spec. `page` defaults to 1, `page_size` to 100
 *  (server caps at 1000). Read by all roles. */
export function runReport(spec: ReportSpec, opts: RunOptions = {}): Promise<ReportResult> {
	return api.post<ReportResult>('/api/reports/run', { ...spec, ...opts });
}

/** Run a saved definition's spec. Read by all roles.
 *
 *  `page` / `page_size` go in the QUERY STRING, not the body: `POST
 *  /api/reports/{id}/run` declares both as `Query(...)` (the spec comes from the
 *  stored row, so there is no body schema to carry them). Sent as a body they
 *  were dropped on the floor and every saved-report run came back as page 1 —
 *  the ad-hoc sibling reads them from the body, hence the asymmetry. Matches how
 *  `downloadReportExport` already builds its query. */
export function runSavedReport(id: string, opts: RunOptions = {}): Promise<ReportResult> {
	const qs = new URLSearchParams();
	if (opts.page != null) qs.set('page', String(opts.page));
	if (opts.page_size != null) qs.set('page_size', String(opts.page_size));
	const query = qs.toString();
	return api.post<ReportResult>(`/api/reports/${id}/run${query ? `?${query}` : ''}`, {});
}

/** Download a saved report as a branded CSV / PDF file (all roles). Returns a
 *  Blob; the caller wires the download anchor. Export requires a saved id — an
 *  ad-hoc spec must be saved first. */
export function downloadReportExport(id: string, format: 'csv' | 'pdf'): Promise<Blob> {
	return api.downloadBlob(`/api/reports/${id}/export?format=${format}`);
}
