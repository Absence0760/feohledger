// Typed helpers for the recurring / subscription invoice endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). Mirrors the pattern of `src/lib/api/contracts.ts`.
import { api } from '#lib/api.ts';
import type {
	RecurringTemplate,
	RecurringTemplateCreate,
	RecurringListResponse,
	RecurringTemplateSummary,
	UpcomingSchedule,
	RecurringHistory
} from '#lib/types/recurring.ts';

export interface RecurringListParams {
	status?: string;
	vendor_id?: string;
	search?: string;
	page?: number;
	page_size?: number;
}

export function listRecurring(params: RecurringListParams = {}): Promise<RecurringListResponse> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<RecurringListResponse>(`/api/recurring?${qs}`);
}

// Whole-set KPI rollup — status counts + per-currency monthly-equivalent
// recurring spend + the soonest upcoming run, over the SAME filters as
// `listRecurring`. The page's `activeCount` / `soonestNextRun` /
// `monthlyRecurringTotal` all derived from the loaded page (and divided floats).
export function getRecurringSummary(
	params: Pick<RecurringListParams, 'status' | 'vendor_id' | 'search'> = {}
): Promise<RecurringTemplateSummary> {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<RecurringTemplateSummary>(`/api/recurring/summary${suffix}`);
}

export function getRecurring(id: string): Promise<RecurringTemplate> {
	return api.get<RecurringTemplate>(`/api/recurring/${id}`);
}

export function createRecurring(body: RecurringTemplateCreate): Promise<RecurringTemplate> {
	return api.post<RecurringTemplate>('/api/recurring', body);
}

export function updateRecurring(
	id: string,
	body: Partial<RecurringTemplateCreate>
): Promise<RecurringTemplate> {
	return api.patch<RecurringTemplate>(`/api/recurring/${id}`, body);
}

export function deleteRecurring(id: string): Promise<void> {
	return api.delete(`/api/recurring/${id}`);
}

// --- Lifecycle ---

export function pauseRecurring(id: string): Promise<RecurringTemplate> {
	return api.post<RecurringTemplate>(`/api/recurring/${id}/pause`, {});
}

export function resumeRecurring(id: string): Promise<RecurringTemplate> {
	return api.post<RecurringTemplate>(`/api/recurring/${id}/resume`, {});
}

export function endRecurring(id: string): Promise<RecurringTemplate> {
	return api.post<RecurringTemplate>(`/api/recurring/${id}/end`, {});
}

// Generate the current period's invoice immediately. Returns an opaque success
// summary — the caller treats it as a fire-and-toast action.
export function generateRecurringNow(id: string): Promise<unknown> {
	return api.post<unknown>(`/api/recurring/${id}/generate-now`, {});
}

// --- Read-only previews (edit-mode modal sections) ---

export function getUpcomingSchedule(id: string, count = 6): Promise<UpcomingSchedule> {
	return api.get<UpcomingSchedule>(`/api/recurring/${id}/upcoming-schedule?count=${count}`);
}

export function getGeneratedHistory(id: string): Promise<RecurringHistory> {
	return api.get<RecurringHistory>(`/api/recurring/${id}/history`);
}
