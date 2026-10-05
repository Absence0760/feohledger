// Typed helpers for the audit trail + auditor-export endpoints. All requests
// route through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce).
import { api } from '#lib/api.ts';
import type { AuditEntry } from '#lib/types/audit.ts';

export interface AuditExportParams {
	invoiceId?: string;
	start?: string; // ISO date (YYYY-MM-DD)
	end?: string; // ISO date (YYYY-MM-DD)
	entityType?: string;
}

function buildExportQuery(params: AuditExportParams, format: 'json' | 'csv'): string {
	const q = new URLSearchParams();
	if (params.invoiceId) q.set('invoice_id', params.invoiceId);
	if (params.start) q.set('start', params.start);
	if (params.end) q.set('end', params.end);
	if (params.entityType) q.set('entity_type', params.entityType);
	q.set('format', format);
	return q.toString();
}

// Per-invoice trail (operational, used by the invoice modal).
export function getInvoiceAuditLog(invoiceId: string): Promise<AuditEntry[]> {
	return api.get<AuditEntry[]>(`/api/invoices/${invoiceId}/audit-log`);
}

// Auditor export (JSON). Provide invoiceId OR a start/end range.
export function getAuditExport(params: AuditExportParams): Promise<AuditEntry[]> {
	return api.get<AuditEntry[]>(`/api/audit/export?${buildExportQuery(params, 'json')}`);
}

// Auditor export (CSV) as a Blob — caller wires a download anchor.
export function downloadAuditExportCsv(params: AuditExportParams): Promise<Blob> {
	return api.downloadBlob(`/api/audit/export?${buildExportQuery(params, 'csv')}`);
}
