/**
 * What the ERP reported, as the backend keeps it on a reconciliation exception
 * (`backend/app/models/exception.py::erp_report`, decisions §318), and the
 * pure helpers that turn it into a pre-filled "record this payment" form.
 *
 * Pure (no Svelte runtime, no `$app/*`), so vitest reaches it directly.
 */
import type { PaymentMethod } from '#lib/types/payment.ts';
import { EXTERNAL_PAYMENT_METHODS, localToday } from '#lib/types/paymentExecution.ts';

/** `erp_report.kind` of a row whose ERP-reported payment could not be recorded. */
export const ERP_REPORT_PAID_NOT_RECORDED = 'paid_not_recorded';

/** The `erp_report` JSON on an exception row. Every field optional: the
 *  backend adds fields over time and an older row may lack one. */
export interface ErpReport {
	kind?: string;
	erp_type?: string | null;
	erp_document_id?: string | null;
	event_id?: string | null;
	method?: string | null;
	reference?: string | null;
	paid_on?: string | null;
	paid_on_reported?: boolean | null;
	refusal_code?: string | null;
}

/** The payment-queue row's `erp_reported_payment` (`api/payments.py::
 *  _queue_erp_reported_payments`): the same facts, flattened for the dialog. */
export interface QueueErpReportedPayment {
	exception_id: string;
	erp_name: string;
	method: string | null;
	reference: string | null;
	paid_on: string | null;
}

/** Does this exception carry a payment the ERP reported and FeohLedger could
 *  not record? */
export function isErpPaidReport(report: ErpReport | null | undefined): report is ErpReport {
	return !!report && report.kind === ERP_REPORT_PAID_NOT_RECORDED;
}

/** What a "record this payment" form starts from. `method` is `''` when the ERP
 *  named no rail a payment made outside FeohLedger can be recorded on, so the
 *  person must choose — never a guessed default (the dialog used to start on
 *  ACH whatever the ERP said, which was wrong for a cheque). */
export interface RecordPrefill {
	method: PaymentMethod | '';
	reference: string;
	paidOn: string;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The form pre-filled from what the ERP reported. A rail it did not state, or
 * one that cannot be recorded outside FeohLedger, leaves the method blank; a
 * missing or future date falls back to `today` (the reader's own calendar day,
 * which the server always accepts).
 */
export function erpPrefill(
	report: Pick<ErpReport, 'method' | 'reference' | 'paid_on'> | null | undefined,
	today: string = localToday()
): RecordPrefill {
	const method = (report?.method ?? '').trim().toLowerCase();
	const paidOn = (report?.paid_on ?? '').slice(0, 10);
	return {
		method: (EXTERNAL_PAYMENT_METHODS as readonly string[]).includes(method)
			? (method as PaymentMethod)
			: '',
		reference: (report?.reference ?? '').trim(),
		paidOn: ISO_DATE.test(paidOn) && paidOn <= today ? paidOn : today
	};
}
