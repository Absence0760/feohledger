// Typed helpers for the invoice routes that are not already reached through
// `stores/invoices.svelte.ts` (list/CRUD) or the modal's inline `api` calls.
// Routes through the shared `api` client (Bearer + X-Tenant-Slug + X-Entity-ID
// + 401-bounce) — never raw fetch. Backend: `backend/app/api/invoices.py`.
import { api } from '#lib/api.ts';
import type { Invoice } from '#lib/types/invoice.ts';
import type { ActionRefusal } from '#lib/api/actionVerdicts.ts';

/**
 * The invoice modal's workflow actions, each the server's verdict for the
 * caller: `null` when the endpoint would accept them, else the coded refusal it
 * would answer with.
 */
export type InvoiceActionVerdicts = {
	/** `POST /approve` — `review.review_refusals`. */
	approve: ActionRefusal;
	/** `POST /reject` — `review.review_refusals`. */
	reject: ActionRefusal;
	/** `POST /complete` (Submit / Send to ERP / Mark Complete) —
	 *  `api/workflow.workflow_action_refusals`, incl. the ERP export gate on an
	 *  approved invoice. Not covered: the required-fields check, which judges
	 *  what the reader has typed. */
	complete: ActionRefusal;
	/** `POST /send-to-erp` — the same push gates. */
	send_to_erp: ActionRefusal;
	/** `POST /retry-erp` — the same push gates, from `failed`. */
	retry_erp: ActionRefusal;
};

/** `invoice_not_completable` — `/complete` from a status it does not advance
 *  (anything but `new` / `approved`). About the status, not something the
 *  reader can act on, so the modal hides Submit rather than explaining it. */
export const INVOICE_NOT_COMPLETABLE = 'invoice_not_completable';
/** `invoice_not_erp_sendable` — `/send-to-erp` on an invoice not `approved`. */
export const INVOICE_NOT_ERP_SENDABLE = 'invoice_not_erp_sendable';
/** `invoice_not_erp_retryable` — `/retry-erp` on anything but an approved
 *  invoice's failed push. Hidden, like {@link INVOICE_NOT_COMPLETABLE}. */
export const INVOICE_NOT_ERP_RETRYABLE = 'invoice_not_erp_retryable';

/**
 * The server's verdicts on the caller's workflow actions for this invoice
 * (`GET /api/invoices/{id}/review-actions`), computed from the gates each
 * endpoint enforces. The modal offers Approve / Reject / Submit / Retry ERP
 * from this and nothing else — see `#lib/api/actionVerdicts.ts`.
 */
export function getReviewActions(invoiceId: string): Promise<InvoiceActionVerdicts> {
	return api.get(`/api/invoices/${invoiceId}/review-actions`);
}

/**
 * Route an inter-company charge — generate the mirror **payable** under the
 * counterparty entity (`POST /api/invoices/{id}/route-intercompany`).
 *
 * Returns the MIRROR invoice, not the origin: the origin's own
 * `intercompany_mirror_id` is set by the same call, so a caller that renders
 * the origin must re-read it (or the list) afterwards rather than assuming the
 * response is the row it started from.
 *
 * Gated `admin` / `ap_manager` (clerks and CFO excluded — see the router's
 * `require_roles`). The backend refuses a counterparty equal to the invoice's
 * own entity with a 400 ("an entity cannot bill itself") and an unknown entity
 * id with a 404; both arrive as an `ApiError` carrying the backend's own
 * PII-free `detail`, so render `e.message` rather than inventing copy.
 *
 * Idempotent by construction: the origin is row-locked, the service
 * short-circuits on an existing `intercompany_mirror_id` and returns the SAME
 * mirror, and `uq_invoice_intercompany_mirror` turns a genuine race into a
 * clean 409. A second deliberate call is therefore never how the UI should
 * behave — once `intercompany_mirror_id` is set, show the routed state.
 * See `backend/docs/inter-company.md`.
 */
export function routeIntercompany(
	invoiceId: string,
	counterpartyEntityId: string
): Promise<Invoice> {
	return api.post<Invoice>(`/api/invoices/${invoiceId}/route-intercompany`, {
		counterparty_entity_id: counterpartyEntityId
	});
}
