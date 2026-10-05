// Typed helpers for the Procurement / Requisitions endpoints. All requests
// route through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID +
// 401-bounce). Mirrors the pattern of `src/lib/api/expenses.ts`.
import { api } from '#lib/api.ts';
import type {
	Requisition,
	RequisitionListResponse,
	RequisitionSummary,
	RequisitionCreate,
	RequisitionUpdate,
	ConvertToPoResult
} from '#lib/types/requisition.ts';

export interface RequisitionListParams {
	status?: string;
	search?: string;
	page?: number;
	page_size?: number;
}

export function listRequisitions(
	params: RequisitionListParams = {}
): Promise<RequisitionListResponse> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<RequisitionListResponse>(`/api/requisitions?${qs}`);
}

// Whole-set KPI rollup — status counts + per-currency value totals over the
// SAME status/search filters as `listRequisitions`, so the `pendingCount` /
// `periodTotal` KPIs can't describe only the loaded page.
export function getRequisitionSummary(
	params: Pick<RequisitionListParams, 'status' | 'search'> = {}
): Promise<RequisitionSummary> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<RequisitionSummary>(`/api/requisitions/summary${suffix}`);
}

export function getRequisition(id: string): Promise<Requisition> {
	return api.get<Requisition>(`/api/requisitions/${id}`);
}

export function createRequisition(body: RequisitionCreate): Promise<Requisition> {
	return api.post<Requisition>('/api/requisitions', body);
}

export function updateRequisition(id: string, body: RequisitionUpdate): Promise<Requisition> {
	return api.patch<Requisition>(`/api/requisitions/${id}`, body);
}

export function deleteRequisition(id: string): Promise<void> {
	return api.delete(`/api/requisitions/${id}`);
}

// --- Approval state machine ---

export function submitRequisition(id: string): Promise<Requisition> {
	return api.post<Requisition>(`/api/requisitions/${id}/submit`, {});
}

export function approveRequisition(id: string): Promise<Requisition> {
	return api.post<Requisition>(`/api/requisitions/${id}/approve`, {});
}

export function rejectRequisition(id: string, reason?: string): Promise<Requisition> {
	return api.post<Requisition>(`/api/requisitions/${id}/reject`, { reason: reason ?? null });
}

export function cancelRequisition(id: string, reason?: string): Promise<Requisition> {
	return api.post<Requisition>(`/api/requisitions/${id}/cancel`, { reason: reason ?? null });
}

// Rework loop: `rejected -> draft`. Gated `require_roles(ADMIN, AP_MANAGER,
// AP_CLERK)` on the backend — the same set that may create/submit/cancel, and
// deliberately NOT the approve/reject set (which includes the CFO but not the
// clerk): reopening is the buyer redoing their own ask, not a decision.
// `submitted_at` is cleared server-side; the rejection reason stays on the row
// as the brief for the rework.
export function reopenRequisition(id: string): Promise<Requisition> {
	return api.post<Requisition>(`/api/requisitions/${id}/reopen`, {});
}

// --- Convert to PO (idempotent) ---

export function convertRequisitionToPo(id: string): Promise<ConvertToPoResult> {
	return api.post<ConvertToPoResult>(`/api/requisitions/${id}/convert-to-po`, {});
}
