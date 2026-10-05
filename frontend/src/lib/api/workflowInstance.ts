// Typed helper for the per-invoice workflow-instance read endpoint
// (`GET /api/invoices/{id}/workflow`) — the chain-progress stepper's data
// source. Routes through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce).
import { api, ApiError } from '#lib/api.ts';
import type { WorkflowInstanceDetail } from '#lib/types/workflowInstance.ts';

/** Fetch the live workflow instance (steps + `state_data.approval_levels`)
 *  for an invoice. Returns `null` on a 404 — an invoice with no workflow
 *  instance (e.g. `new`, or a workflow with every step disabled) is a normal
 *  outcome here, not a load failure, so the caller renders nothing rather
 *  than an error toast. Any other failure still propagates. */
export async function getInvoiceWorkflowInstance(
	invoiceId: string
): Promise<WorkflowInstanceDetail | null> {
	try {
		return await api.get<WorkflowInstanceDetail>(`/api/invoices/${invoiceId}/workflow`);
	} catch (err) {
		if (err instanceof ApiError && err.status === 404) return null;
		throw err;
	}
}
