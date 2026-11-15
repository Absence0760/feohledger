/**
 * What the ERP reported, as the backend keeps it on a reconciliation exception
 * (`backend/app/models/exception.py::erp_report`, decisions §319), and the
 * pure helpers that turn it into a pre-filled "record this payment" form.
 *
 * Pure (no Svelte runtime, no `$app/*`), so vitest reaches it directly.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { erpRefusalText } from '#lib/api/erpRefusals.ts';
import type { PaymentMethod } from '#lib/types/payment.ts';
import { EXTERNAL_PAYMENT_METHODS, localToday } from '#lib/types/paymentExecution.ts';

/** `erp_report.kind` of a row whose ERP-reported payment could not be recorded. */
export const ERP_REPORT_PAID_NOT_RECORDED = 'paid_not_recorded';

/** `erp_report.kind` of an open PO-match hold an ERP PO sync's re-match no
 *  longer finds (decisions §323, amended): the sync never clears a hold, so
 *  this is what tells a reviewer the PO changed and the re-check now matches. */
export const ERP_REPORT_PO_REMATCH_CLEARS = 'po_rematch_clears';

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
	/** The cash the ERP states was paid, as an exact decimal string, and its
	 *  ISO 4217 code (Xero and QuickBooks, decisions §386, §393); null when not
	 *  stated (a QuickBooks row kept before §393, whose bill the server reads
	 *  again, or the generic ERP webhook). The one-click record books the
	 *  stated figure or nothing. */
	amount?: string | null;
	amount_currency?: string | null;
	/** How the ERP says `amount` was paid, one entry per cash payment: its rail
	 *  (null = not stated) and the exact decimal string it paid, in
	 *  `amount_currency` (decisions §396). Stated only beside `amount`, which it
	 *  breaks down; absent or null when the ERP stated no breakdown. Whether the
	 *  parts add up is the server's call (`erp_payment_split_stands` on the
	 *  exception row), never summed here. */
	payment_parts?: ErpPaymentPart[] | null;
	refusal_code?: string | null;
	/** `po_rematch_clears`: the PO number matched, and the UTC day seen. */
	po_number?: string | null;
	observed_on?: string | null;
	/** The payment write-back rows (`payment_writeback_failed`,
	 *  `payment_void_writeback_failed`): the ERP's coded refusal as
	 *  `{code, params}` (`erp_refusal_record`), or null when the call failed
	 *  without one. Also `payment_id` / `payment_run_id` there and on
	 *  `payment_sync_back_failed` — machine detail, never shown as prose. */
	refusal?: unknown;
	payment_id?: string | null;
	payment_run_id?: string | null;
}

/** One part of an ERP-stated payment breakdown, as the row keeps it. */
export interface ErpPaymentPart {
	method?: string | null;
	amount?: string | null;
}

/** A breakdown part ready to render: a rail a payment made outside FeohLedger
 *  can be recorded on, or `null` for "not stated", and its exact amount. */
export interface RailPartView {
	rail: PaymentMethod | null;
	amount: string;
}

/** `erp_report.kind` of the rows the payment write-back opens with a refusal. */
const WRITEBACK_REFUSAL_KINDS = new Set(['payment_writeback_failed', 'payment_void_writeback_failed']);

/** The payment-queue row's `erp_reported_payment` (`api/payments.py::
 *  _queue_erp_reported_payments`): the same facts, flattened for the dialog. */
export interface QueueErpReportedPayment {
	exception_id: string;
	erp_name: string;
	method: string | null;
	reference: string | null;
	paid_on: string | null;
	/** The cash the ERP states and its code, as on the row's `erp_report`. */
	amount?: string | null;
	amount_currency?: string | null;
	/** The ERP's breakdown as the row keeps it (`ErpReport.payment_parts`). */
	payment_parts?: ErpPaymentPart[] | null;
	/** The server's verdict on that breakdown, the one the exception row
	 *  carries as `erp_payment_split_stands` (`erp_status_sync.
	 *  kept_split_stands`). `true`: the queue's record books through the row's
	 *  one-click, since `record-outside` refuses it (decisions §426). */
	split_stands?: boolean | null;
}

/** Does this exception carry a payment the ERP reported and FeohLedger could
 *  not record? */
export function isErpPaidReport(report: ErpReport | null | undefined): report is ErpReport {
	return !!report && report.kind === ERP_REPORT_PAID_NOT_RECORDED;
}

/** Is this a hold whose PO changed in the ERP so that a re-check now matches? */
export function isPoRematchClears(report: ErpReport | null | undefined): report is ErpReport {
	return !!report && report.kind === ERP_REPORT_PO_REMATCH_CLEARS;
}

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/**
 * The one line the exceptions queue shows for a `po_rematch_clears` row, or
 * `null` for any other report. `erp` is the ERP's name (or the reader's "your
 * ERP"); `date` renders `observed_on` in the reader's locale. A row with no PO
 * number or day still says what changed, without inventing either.
 */
export function poRematchNote(
	report: ErpReport | null | undefined,
	erp: string,
	t: Translate,
	date: (isoDay: string) => string
): string | null {
	if (!isPoRematchClears(report)) return null;
	const po = (report.po_number ?? '').trim();
	const day = (report.observed_on ?? '').slice(0, 10);
	if (po && ISO_DATE.test(day)) {
		return t('exceptions.poRematch.note', { po, erp, date: date(day) });
	}
	return po
		? t('exceptions.poRematch.noteNoDate', { po, erp })
		: t('exceptions.poRematch.noteNoPo', { erp });
}

/**
 * Why the ERP refused a payment write-back, in the reader's language and with
 * what to do about it (`erpRefusals.ts`), or `null` — for any other row, a
 * failure the ERP gave no code for, or a code this build cannot state. The
 * row's own sentence says only that the write-back failed; the cause used to be
 * a raw reason code spliced into it (decisions §331 follow-up).
 */
export function erpRefusalNote(
	report: ErpReport | null | undefined,
	t: Translate
): string | null {
	if (!report || !WRITEBACK_REFUSAL_KINDS.has(report.kind ?? '')) return null;
	return erpRefusalText(report.refusal, t);
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

/**
 * The ERP's breakdown of a reported payment, part by part, as the server books
 * it: a rail it cannot record on (`virtual_card`, an unknown string) reads as
 * not stated, the rule of `erp_status_sync.reported_payment_parts`. A malformed
 * breakdown (a part with no exact amount string) reads as none at all, never as
 * a partial list that would look like the whole (`payment_methods.
 * rail_parts_from_json`). Amounts are passed through untouched, never parsed.
 */
export function erpPaymentParts(report: Pick<ErpReport, 'payment_parts'> | null | undefined): RailPartView[] {
	const parts = report?.payment_parts;
	if (!Array.isArray(parts)) return [];
	const out: RailPartView[] = [];
	for (const part of parts) {
		if (!part || typeof part !== 'object' || typeof part.amount !== 'string' || !part.amount.trim()) {
			return [];
		}
		const rail = typeof part.method === 'string' ? part.method.trim().toLowerCase() : '';
		out.push({
			rail: (EXTERNAL_PAYMENT_METHODS as readonly string[]).includes(rail)
				? (rail as PaymentMethod)
				: null,
			amount: part.amount
		});
	}
	return out;
}

/**
 * Does this breakdown span more than one rail? A part with no rail stated
 * counts as one of its own, as the server counts it. Rails only: no amount is
 * compared or added, which is the server's job.
 */
export function spansSeveralRails(parts: readonly RailPartView[]): boolean {
	return new Set(parts.map((p) => p.rail)).size > 1;
}

/**
 * What the note under a shown split says, from the server's three-state
 * verdict (decisions §413): `true`, it stands and is the record; `false`, it
 * was judged and does not add up, so the chosen rail is; `null` / absent, not
 * judged (no stated figure, parts the server's stricter parser rejects, or an
 * older server), so only the record decides. Shared by `/exceptions` and the
 * payment queue's dialog, so the two cannot say different things.
 */
export function splitNoteKey(stands: boolean | null | undefined): MessageKey {
	if (stands === true) return 'exceptions.erpPayment.splitStands';
	if (stands === false) return 'exceptions.erpPayment.splitDropped';
	return 'exceptions.erpPayment.splitUnjudged';
}
