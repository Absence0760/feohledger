// Typed helpers for the vendor statement reconciliation endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce). Mirrors the pattern of `src/lib/api/recurring.ts`.
import { api } from '#lib/api.ts';
import type {
	Reconciliation,
	ReconciliationCreate,
	ReconciliationListResponse,
	ReconciliationSummary,
	LineResolveRequest,
	CloseReadinessResponse
} from '#lib/types/vendorStatementRecon.ts';

export interface ReconListParams {
	vendor_id?: string;
	status?: string;
	/** Free-text over supplier + statement reference — the two columns the row
	 *  renders. A SERVER filter: the page used to narrow the loaded rows in the
	 *  browser, so a statement on page 2 read as "nothing matched". */
	search?: string;
	page?: number;
	page_size?: number;
}

export function listReconciliations(
	params: ReconListParams = {}
): Promise<ReconciliationListResponse> {
	const qs = new URLSearchParams();
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<ReconciliationListResponse>(`/api/vendor-statements?${qs}`);
}

// Whole-set KPI rollup — status counts + total open discrepancies over the
// SAME vendor_id / status / search filters as `listReconciliations`, so
// `openCount` / `totalDiscrepancies` stop describing only the loaded page.
export function getReconciliationSummary(
	params: Pick<ReconListParams, 'vendor_id' | 'status' | 'search'> = {}
): Promise<ReconciliationSummary> {
	const qs = new URLSearchParams();
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<ReconciliationSummary>(`/api/vendor-statements/summary${suffix}`);
}

export function getReconciliation(id: string): Promise<Reconciliation> {
	return api.get<Reconciliation>(`/api/vendor-statements/${id}`);
}

// Create a reconciliation run from a pasted / normalised list of lines (JSON).
export function createReconciliation(body: ReconciliationCreate): Promise<Reconciliation> {
	return api.post<Reconciliation>('/api/vendor-statements', body);
}

// Create a reconciliation run by uploading a statement file (CSV / PDF), with
// the run metadata carried as multipart form fields alongside the file.
export function uploadReconciliation(
	file: File,
	fields: {
		vendor_id: string;
		statement_date: string;
		statement_reference?: string;
		currency?: string;
	}
): Promise<Reconciliation> {
	return api.upload<Reconciliation>('/api/vendor-statements/upload', file, {
		vendor_id: fields.vendor_id,
		statement_date: fields.statement_date,
		statement_reference: fields.statement_reference,
		currency: fields.currency
	});
}

export function deleteReconciliation(id: string): Promise<void> {
	return api.delete(`/api/vendor-statements/${id}`);
}

// The authenticated path for the archived supplier document. The shared `api`
// client adds the Bearer + tenant headers, so a bare `<a href>` can't reach it —
// callers go through `downloadSourceStatement` below.
export function sourceStatementPath(id: string): string {
	return `/api/vendor-statements/${id}/file`;
}

// Fetch + trigger a browser download of the statement this run was built from.
// Mirrors `downloadPositivePayFile`. A run whose document was never archived
// (pasted-lines path, or a best-effort storage miss) 404s — the caller gates on
// `has_source_file` and surfaces the failure rather than swallowing it.
export async function downloadSourceStatement(id: string, filename: string): Promise<void> {
	const blob = await api.downloadBlob(sourceStatementPath(id));
	const url = URL.createObjectURL(blob);
	try {
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		a.remove();
	} finally {
		URL.revokeObjectURL(url);
	}
}

// Resolve / ignore / un-resolve a single reconciliation line.
export function resolveLine(
	reconciliationId: string,
	lineId: string,
	body: LineResolveRequest
): Promise<Reconciliation> {
	return api.post<Reconciliation>(
		`/api/vendor-statements/${reconciliationId}/lines/${lineId}/resolve`,
		body
	);
}

// Month-end close-readiness: vendors whose latest run still carries a material
// unreconciled balance.
export function getCloseReadiness(materiality?: number): Promise<CloseReadinessResponse> {
	const qs = new URLSearchParams();
	if (materiality !== undefined) qs.set('materiality', String(materiality));
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<CloseReadinessResponse>(`/api/vendor-statements/close-readiness${suffix}`);
}
