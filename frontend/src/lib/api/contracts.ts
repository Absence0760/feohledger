// Typed helpers for the contract endpoints. All requests route through the
// shared `api` client (Bearer + X-Tenant-Slug + 401-bounce). Mirrors the
// pattern of `src/lib/api/tax.ts` / `audit.ts`.
import { api } from '#lib/api.ts';
import type { MatchingIdsResponse } from '#lib/utils/pagination.ts';
import { triggerDownload } from '#lib/utils/download.ts';
import type { MoneyString } from '#lib/utils/money.ts';
import type {
	Contract,
	ContractCreate,
	ContractListResponse
} from '#lib/types/contract.ts';

export interface ContractListParams {
	status?: string;
	contract_type?: string;
	vendor_id?: string;
	search?: string;
	sort?: string;
	order?: 'asc' | 'desc';
	page?: number;
	page_size?: number;
}

function contractQuery(params: ContractListParams): URLSearchParams {
	const qs = new URLSearchParams();
	if (params.status) qs.set('status', params.status);
	if (params.contract_type) qs.set('contract_type', params.contract_type);
	if (params.vendor_id) qs.set('vendor_id', params.vendor_id);
	if (params.search) qs.set('search', params.search);
	if (params.sort) qs.set('sort', params.sort);
	if (params.order) qs.set('order', params.order);
	return qs;
}

export function listContracts(params: ContractListParams = {}): Promise<ContractListResponse> {
	const qs = contractQuery(params);
	qs.set('page', String(params.page ?? 1));
	qs.set('page_size', String(params.page_size ?? 20));
	return api.get<ContractListResponse>(`/api/contracts?${qs}`);
}

/** Every contract id matching the current list filters — `GET
 * /api/contracts/ids`. Backs "select all N matching" (see
 * `getVendorIds`/`getExpenseIds` for the identical rationale). */
export function getContractIds(params: ContractListParams = {}): Promise<MatchingIdsResponse> {
	const qs = contractQuery(params);
	const query = qs.toString();
	return api.get<MatchingIdsResponse>(`/api/contracts/ids${query ? `?${query}` : ''}`);
}

// --- Bulk operations ---

export interface BulkSkip {
	id: string;
	reason: string;
}

export interface ContractBulkStatusResponse {
	updated: number;
	skipped: BulkSkip[];
}

export type ContractBulkAction = 'activate' | 'terminate' | 'cancel';

/** Bulk lifecycle transition over a hand-picked set of contracts — routed
 * through the same `_transition` helper the single-row activate/terminate/
 * cancel endpoints use. admin / ap_manager. */
export function bulkContractStatus(
	ids: string[],
	action: ContractBulkAction
): Promise<ContractBulkStatusResponse> {
	return api.post<ContractBulkStatusResponse>('/api/contracts/bulk/status', { ids, action });
}

/** CSV export of a hand-picked set of contracts. */
export async function exportContractsCsv(ids: string[]): Promise<void> {
	const blob = await api.downloadBlobPost('/api/contracts/bulk/export', { ids });
	triggerDownload(blob, `contracts-export.csv`);
}

export function getContract(id: string): Promise<Contract> {
	return api.get<Contract>(`/api/contracts/${id}`);
}

export function createContract(body: ContractCreate): Promise<Contract> {
	return api.post<Contract>('/api/contracts', body);
}

export function updateContract(id: string, body: Partial<ContractCreate>): Promise<Contract> {
	return api.patch<Contract>(`/api/contracts/${id}`, body);
}

export function deleteContract(id: string): Promise<void> {
	return api.delete(`/api/contracts/${id}`);
}

export function uploadContractFile(id: string, file: File): Promise<Contract> {
	return api.upload<Contract>(`/api/contracts/${id}/upload`, file);
}

// Bytes for the attached document. `<iframe>`/`<a download>` can't reach the
// endpoint directly (no Bearer header), so fetch through the auth'd client and
// render via a blob URL. Caller revokes the returned URL.
export function fetchContractFile(fileKey: string): Promise<string> {
	return api.fetchBlob(`/api/contracts/file/${fileKey}`);
}

// --- Lifecycle ---

export function activateContract(id: string): Promise<Contract> {
	return api.post<Contract>(`/api/contracts/${id}/activate`, {});
}

export function terminateContract(id: string): Promise<Contract> {
	return api.post<Contract>(`/api/contracts/${id}/terminate`, {});
}

export function cancelContract(id: string): Promise<Contract> {
	return api.post<Contract>(`/api/contracts/${id}/cancel`, {});
}

export interface RenewContractBody {
	end_date: string;
	// Request-side money — the exact decimal text typed. `schemas/contract.py`
	// declares both `Decimal`, and a fractional JSON number is already a float
	// by the time pydantic sees it.
	total_value?: MoneyString | null;
	spend_limit?: MoneyString | null;
}

export function renewContract(id: string, body: RenewContractBody): Promise<Contract> {
	return api.post<Contract>(`/api/contracts/${id}/renew`, body);
}

export interface CreatePoBody {
	po_number?: string;
	/** Request-side money — the exact decimal text typed. */
	total?: MoneyString | null;
}

export interface CreatedPo {
	id: string;
	po_number: string;
}

export function createPoFromContract(id: string, body: CreatePoBody = {}): Promise<CreatedPo> {
	return api.post<CreatedPo>(`/api/contracts/${id}/create-po`, body);
}
