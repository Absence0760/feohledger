/**
 * Retrying a payment run's failed payments (`POST /api/payments/runs/{id}/retry-failed`).
 *
 * Whether the control is offered is the server's verdict
 * (`actions.retry_failed`, from `payment_controls.run_retry_failed_refusal` —
 * the gate the endpoint raises from). Nothing here restates that rule; this
 * module only decides which refusal is worth rendering, and how to state what
 * the endpoint reports back.
 */
import { isOffered, type ActionRefusal } from '#lib/api/actionVerdicts.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

/** The refusal for a run that has no failures to retry at all (a draft, a
 *  completed run, one still executing). Not a reason the reader can act on in
 *  this run, so the control is hidden rather than shown disabled — the way the
 *  dialog shows no "Approve as CFO" on a run that needs no sign-off. */
export const RUN_NOT_RETRYABLE = 'payment_run_not_retryable';

/** Whether the run dialog renders the Retry failed payments control at all:
 *  the verdict has arrived (fail closed), it is not about the reader's role,
 *  and the run is one that finished with failures. Whether it may be PRESSED
 *  is `isAllowed(verdict)` alone. */
export function isRunRetryOffered(verdict: ActionRefusal | undefined): boolean {
	return isOffered(verdict) && verdict?.code !== RUN_NOT_RETRYABLE;
}

/** `POST /runs/{id}/retry-failed`'s response: the dispatch pass's rollup plus
 *  what this retry booked and skipped. `skip_reasons` is the sorted, distinct
 *  set of reason codes. */
export interface RetryFailedResult {
	id: string;
	status: string;
	message: string;
	payments_completed: number;
	payments_in_flight: number;
	payments_failed: number;
	payments_retried: number;
	payments_skipped: number;
	skip_reasons: string[];
}

/** One message key per skip reason the endpoint emits — the
 *  `skipped.append("…")` literals in `backend/app/api/payments.py`
 *  (`runRetry.test.ts` fails when the backend grows one this map lacks). */
export const RETRY_SKIP_REASON_KEYS: Record<string, MessageKey> = {
	invoice_not_payable: 'paymentRuns.runDetail.retrySkip.invoiceNotPayable',
	invoice_has_blocking_exception: 'paymentRuns.runDetail.retrySkip.invoiceHasBlockingException',
	vendor_not_active: 'paymentRuns.runDetail.retrySkip.vendorNotActive',
	invoice_has_live_card: 'paymentRuns.runDetail.retrySkip.invoiceHasLiveCard',
	needs_reconciliation: 'paymentRuns.runDetail.retrySkip.needsReconciliation',
	invoice_has_live_payment: 'paymentRuns.runDetail.retrySkip.invoiceHasLivePayment',
	applied_credit_mismatch: 'paymentRuns.runDetail.retrySkip.appliedCreditMismatch',
	discount_changed: 'paymentRuns.runDetail.retrySkip.discountChanged',
	net_amount_changed: 'paymentRuns.runDetail.retrySkip.netAmountChanged'
};

/** The message key for a skip reason, or `null` for one this build predates
 *  (the caller renders the raw code rather than nothing). */
export function retrySkipReasonKey(reason: string): MessageKey | null {
	return RETRY_SKIP_REASON_KEYS[reason] ?? null;
}
