// Typed helpers for the Positive Pay / Payment Fraud File endpoints. All
// requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// X-Entity-ID + 401-bounce). Mirrors `src/lib/api/vendorStatementRecon.ts`.
import { api } from '#lib/api.ts';
import type {
	PositivePayFile,
	PositivePayListResponse,
	PositivePaySummary,
	PresentedItemInput,
	ProcessReturnResponse
} from '#lib/types/positivePay.ts';

export interface PositivePayListParams {
	file_type?: string;
	status?: string;
	/** Free-text over bank format, file type and both ids as text. A SERVER
	 *  filter: the page used to narrow the loaded rows in the browser, so a file
	 *  on page 2 read as "nothing matched". The rendered row LABEL is not
	 *  searchable server-side by design — it is localised, so matching it in SQL
	 *  would make the result set depend on the browser's language. */
	search?: string;
	page?: number;
	page_size?: number;
}

export function listPositivePayFiles(
	params: PositivePayListParams = {}
): Promise<PositivePayListResponse> {
	const qs = new URLSearchParams();
	if (params.file_type) qs.set('file_type', params.file_type);
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<PositivePayListResponse>(`/api/positive-pay?${qs}`);
}

// Whole-set KPI rollup — status counts + total exported items + total flagged
// returns, over the SAME file_type / status / search filters as
// `listPositivePayFiles`, so `itemsExported` / `returnsFlagged` stop describing
// only the loaded page.
export function getPositivePaySummary(
	params: Pick<PositivePayListParams, 'file_type' | 'status' | 'search'> = {}
): Promise<PositivePaySummary> {
	const qs = new URLSearchParams();
	if (params.file_type) qs.set('file_type', params.file_type);
	if (params.status) qs.set('status', params.status);
	if (params.search) qs.set('search', params.search);
	const suffix = qs.toString() ? `?${qs}` : '';
	return api.get<PositivePaySummary>(`/api/positive-pay/summary${suffix}`);
}

export function getPositivePayFile(id: string): Promise<PositivePayFile> {
	return api.get<PositivePayFile>(`/api/positive-pay/${id}`);
}

// Generate the check-issue Positive Pay file for a payment run. Idempotent on
// (run, bank_format) — re-generating returns the existing file (HTTP 200).
export function generateCheckIssue(
	runId: string,
	bankFormat = 'csv'
): Promise<PositivePayFile> {
	return api.post<PositivePayFile>(`/api/positive-pay/payment-runs/${runId}/check-issue`, {
		bank_format: bankFormat
	});
}

// Generate a standalone ACH debit-authorization file for the org.
export function generateAchAuthorization(bankFormat = 'csv'): Promise<PositivePayFile> {
	return api.post<PositivePayFile>('/api/positive-pay/ach-authorization', {
		bank_format: bankFormat
	});
}

// Process the bank's return against a check-issue file: classifies each
// presented cheque and raises deduped fraud Exceptions on altered / not-on-file
// items.
export function processReturn(
	fileId: string,
	presentedItems: PresentedItemInput[]
): Promise<ProcessReturnResponse> {
	return api.post<ProcessReturnResponse>(`/api/positive-pay/${fileId}/process-return`, {
		presented_items: presentedItems
	});
}

export function deletePositivePayFile(id: string): Promise<void> {
	return api.delete(`/api/positive-pay/${id}`);
}

// The authenticated download path for a file's rendered bytes. The shared `api`
// client adds the Bearer + tenant headers; callers fetch it via
// `api.downloadBlob` (a bare <a href> can't carry the token). The rendered file
// legitimately contains full account numbers, so it's behind the read-role gate.
export function positivePayDownloadPath(id: string): string {
	return `/api/positive-pay/${id}/download`;
}

// Fetch + trigger a browser download of the rendered file.
export async function downloadPositivePayFile(id: string, filename: string): Promise<void> {
	const blob = await api.downloadBlob(positivePayDownloadPath(id));
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
