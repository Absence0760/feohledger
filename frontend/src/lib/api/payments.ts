// Typed helpers for the payment-path RECOVERY exits — the endpoints that exist
// to un-strand money that has already moved, or to finish a reversal whose
// best-effort leg did not land. Everything routes through the shared `api`
// client (Bearer + X-Tenant-Slug + X-Entity-ID + 401-bounce).
//
// Each mirrors its server gate in the UI so a holder-less role never sees a
// control that can only 403: `retryRunErpSync` / `acceptPaymentSettlement` are
// `payment.execute` (`PERM_PAYMENT_EXECUTE`), `voidPayment` /
// `retryVoidCardCancel` are `payment.void` (`PERM_PAYMENT_VOID`). None of them
// moves money — they report money that already moved, close out a payable the
// rail short-paid, reverse the books, or shut a card the void could not.
//
// See `backend/docs/payments.md` § ERP Payment Sync + § Settlement-amount
// verification + § Voiding a card payment.
import { api } from '#lib/api.ts';
import type { Payment, PaymentMethod } from '#lib/types/payment.ts';
import type { ExecutionModeResponse, NachaSecCode } from '#lib/types/paymentExecution.ts';
import { triggerDownload } from '#lib/utils/download.ts';

/**
 * What `POST /api/payments/runs/{run_id}/sync-erp` returns.
 *
 * Read `transitioned`, NOT `synced`, to answer "did this recover anything".
 * `synced` counts legs whose ERP-facing work completed, which stays true for a
 * settled payment whose invoice was already `paid` — so a repeat call reports
 * the same `synced` and `transitioned: 0`. The route's own docstring says so.
 */
export interface RunErpSyncResult {
	/** The run the pass ran for (echoed back as a string uuid). */
	id: string;
	/** Legs whose ERP-facing work completed. True again on a repeat call. */
	synced: number;
	/** Invoices actually moved `payment_scheduled → paid`. The success number. */
	transitioned: number;
	/** Legs the pass declined to act on (payment not `completed`, invoice past
	 *  `payment_scheduled`) — the idempotency in action, not a failure. */
	skipped: number;
	/** Legs whose settlement doesn't cover the invoice, so it stays held. Those
	 *  exit via `acceptPaymentSettlement` (or a void), not via another sync. */
	held: number;
	/** Legs that raised again. Each keeps its `erp_reconciliation` exception. */
	failed: number;
}

/**
 * Re-run the ERP sync-back for a run whose settled payments never landed.
 *
 * The exit for an invoice stranded at `payment_scheduled` after a
 * `payment_erp_sync` leg failed. Idempotent by construction (the pass skips
 * every non-`completed` payment and every invoice past `payment_scheduled`) and
 * moves no money. 409s when the run has no settled payments at all.
 *
 * Voiding is NOT the exit for that state: it returns the invoice to `approved`
 * and invites a second payment for money that already left.
 */
export function retryRunErpSync(runId: string): Promise<RunErpSyncResult> {
	return api.post<RunErpSyncResult>(`/api/payments/runs/${runId}/sync-erp`, {});
}

/**
 * Declare a short / unverifiable settlement final and release the invoice.
 *
 * The other exit from the under-settlement hold: the rail settled less than AP
 * authorized (or in a currency we never authorized), so `settlement_coverage`
 * holds the invoice at `payment_scheduled` rather than reporting it settled in
 * full. Accepting moves it to `paid` and records `reason` on the immutable
 * trail. Irreversible; the money stays where it landed.
 *
 * 409s when the settlement already covers the invoice ("nothing to accept") and
 * when the invoice is no longer held — the backend is the authority on both, so
 * surface its `detail` rather than pre-judging it client-side.
 */
export function acceptPaymentSettlement(paymentId: string, reason: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/settlement/accept`, { reason });
}

/**
 * Void a completed or in-flight payment; the invoice returns to `approved`.
 *
 * Typed because the response is load-bearing beyond "it worked": for a
 * `virtual_card` payment it carries `void_card_disposition`, the verdict on
 * whether the card was actually closed at the provider. Both the rail reversal
 * and the card close are best-effort (a provider outage must not block the
 * accounting void), so a bare 200 does NOT mean the card is shut — read the
 * disposition, and offer `retryVoidCardCancel` on `not_closed_retryable`.
 */
export function voidPayment(paymentId: string, reason: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/void`, { reason });
}

/**
 * Re-attempt ONLY the card close for an already-voided card payment.
 *
 * The remedy sits on the void rather than beside it (`docs/decisions.md` §96,
 * §132): `POST /api/cards/{id}/cancel` would also close the card, but it is
 * reachable on a LIVE payment, where it kills the card while the payment and
 * its invoice still claim money is in flight. This one 409s on anything but an
 * already-`voided` card payment, so it can only ever finish a reversal.
 *
 * Idempotent — a second retry on an already-closed card returns
 * `card_already_cancelled` (disposition `closed`), not an error. Moves no
 * money, does not re-ask the payment rail, and does not re-void.
 */
export function retryVoidCardCancel(paymentId: string): Promise<Payment> {
	return api.post<Payment>(`/api/payments/${paymentId}/void/retry-card-cancel`, {});
}

// ── The no-rail pilot (issue #517, `docs/decisions.md` §251) ──────────────
//
// A tenant's payments are either SENT by FeohLedger (`processor`) or only
// RECORDED here after the customer paid from its own bank or ERP
// (`record_only`). The helpers below are the record half; none of them moves
// money. `payment.record_external` (`PERM_PAYMENT_RECORD_EXTERNAL`) gates the
// two record writes; the NACHA file and its void are `payment.execute`, since
// uploading the file is what sends the money. The mode read admits any of
// execute / void / record_external.
// See `backend/docs/payments.md` § Paying outside FeohLedger.

/** Whether FeohLedger sends this tenant's payments or only records them. */
export function getExecutionMode(): Promise<ExecutionModeResponse> {
	return api.get<ExecutionModeResponse>('/api/payments/execution-mode');
}

/** `POST /api/payments/record-outside`. */
export interface RecordPaymentOutsideBody {
	invoice_id: string;
	method: PaymentMethod;
	/** The cheque number / bank confirmation. 1–255 characters. */
	reference: string;
	/** `YYYY-MM-DD`, not in the future (the server allows one day past UTC
	 *  today, so the reader's local "today" always passes). */
	paid_on: string;
	/**
	 * Optional exact-decimal STRING cross-check. The server binds the amount to
	 * what the invoice owes on `paid_on` (net of credit memos and any accepted
	 * early-pay discount) and 422s a disagreeing figure — omit it to record the
	 * amount owed, which is what the queue's dialog does.
	 */
	amount?: string;
}

/**
 * Record one invoice as paid outside FeohLedger. Available in BOTH execution
 * modes (a processor tenant can still pay one invoice by cheque). Idempotent:
 * replaying the same reference + date returns the existing payment (200)
 * rather than booking a second one (201). Refusals arrive coded
 * (`external_payment_*`) and are localized by `api/codedRefusals.ts`.
 */
export function recordPaymentOutside(body: RecordPaymentOutsideBody): Promise<Payment> {
	return api.post<Payment>('/api/payments/record-outside', body);
}

/** What `POST /api/payments/runs/{id}/record-outside` returns. */
export interface RecordRunOutsideResult {
	id: string;
	status: string;
	payment_count: number;
	/** Exact decimal string, in the run's own currency. */
	total_amount: string;
}

/**
 * Record a whole `draft` or `exported` run as paid by the customer's own bank —
 * typically after uploading the run's NACHA file (which left it `exported`). One refusal refuses the whole run (nothing is
 * recorded) and names the invoice in `params.invoice_number`; maker-checker and
 * CFO sign-off apply exactly as they do to Execute.
 */
export function recordRunOutside(
	runId: string,
	body: { reference: string; paid_on: string }
): Promise<RecordRunOutsideResult> {
	return api.post<RecordRunOutsideResult>(`/api/payments/runs/${runId}/record-outside`, body);
}

/** The authenticated path of a run's NACHA ACH file. `regenerate` is required
 *  to fetch a SECOND file for an already-`exported` run. */
export function runNachaPath(
	runId: string,
	effectiveDate: string,
	secCode: NachaSecCode,
	regenerate = false
): string {
	const qs = new URLSearchParams({ effective_date: effectiveDate, sec_code: secCode });
	if (regenerate) qs.set('regenerate', 'true');
	return `/api/payments/runs/${runId}/nacha?${qs.toString()}`;
}

/**
 * Fetch a run's NACHA file and save it. Gated on `payment.execute`, not
 * `payment.record_external`: uploading the file is what moves the money.
 *
 * **The first export claims the run** (`draft → exported`): it can no longer be
 * executed or cancelled until it is recorded as paid or the export is voided
 * ({@link voidRunNachaExport}). A second file for an `exported` run needs
 * `regenerate` — without it the server 409s `nacha_already_exported`, because
 * uploading two files pays every supplier twice.
 *
 * Through `api.downloadBlob` (a bare `<a href>` can't carry the Bearer / tenant
 * / entity headers), so a coded refusal (`nacha_*`) surfaces as a localized
 * `ApiError`. The file carries the vendors' bank account numbers — it is handed
 * straight to the browser's download and never kept in page state.
 */
export async function downloadRunNacha(
	runId: string,
	effectiveDate: string,
	secCode: NachaSecCode,
	regenerate = false
): Promise<void> {
	const blob = await api.downloadBlob(runNachaPath(runId, effectiveDate, secCode, regenerate));
	triggerDownload(blob, `payment-run-${runId.slice(0, 8)}-${effectiveDate}.ach`);
}

/**
 * The bank rejected (or never received) the exported file: release the run
 * back to `draft` so it can be re-exported, cancelled or executed. An
 * attestation FeohLedger cannot check, so `reason` is required (≤ 500
 * characters) and lands on the audit trail. `payment.execute`; 409 unless the
 * run is `exported`.
 */
export function voidRunNachaExport(
	runId: string,
	reason: string
): Promise<{ id: string; status: string }> {
	return api.post<{ id: string; status: string }>(`/api/payments/runs/${runId}/nacha/void`, {
		reason
	});
}
