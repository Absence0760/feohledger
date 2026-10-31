/**
 * Paying a run outside FeohLedger (`docs/decisions.md` §251) — the run
 * dialog's record / bank-file / bank-rejected controls.
 *
 * Whether each is offered is the server's verdict (issue #575); nothing here
 * restates a rule, this module only decides which refusals hide a control
 * rather than disable it. Pure, like `runRetry.ts`.
 */
import { isOffered, type ActionRefusal } from '#lib/api/actionVerdicts.ts';

/*
 * Paying a run outside FeohLedger — which of the run dialog's controls are
 * RENDERED, from the server's verdicts (`GET /api/payments/runs/{id}`'s
 * `actions`, built from `payment_controls.run_record_outside_refusal`,
 * `.run_nacha_export_refusal` and `.run_nacha_void_refusal` — the gates the
 * endpoints raise from). Whether a rendered control may be PRESSED is
 * `isAllowed(verdict)` alone.
 *
 * Each hides on the refusals that say the run is in the wrong state for the
 * control at all — a first-file download on a run whose file is already out, a
 * "bank rejected the file" on a run with no file — the way the dialog shows no
 * "Approve as CFO" on a run that needs no sign-off. Any other refusal (a
 * colleague must do it, the CFO has not signed, the bank file is not set up)
 * renders the control disabled beside its reason.
 */

/** `GET /runs/{id}/nacha` refusals that mean "no first file to download here". */
export const BANK_FILE_DOWNLOAD_HIDDEN = ['nacha_already_exported', 'nacha_run_not_exportable'];
/** `…/nacha?regenerate=true` refusal that means "no exported file to redo". */
export const BANK_FILE_REGENERATE_HIDDEN = ['nacha_nothing_to_regenerate'];
/** `POST /runs/{id}/nacha/void` refusal that means "no file to reject". */
export const BANK_FILE_REJECT_HIDDEN = ['nacha_run_not_exported'];
/** `POST /runs/{id}/record-outside` refusal that means "not a run to record". */
export const RUN_RECORD_HIDDEN = ['payment_run_not_recordable'];

/** Whether a run-dialog control is rendered: the verdict has arrived (fail
 *  closed), is not about the reader's role, and is not one of `hidden`. */
export function isRunControlOffered(
	verdict: ActionRefusal | undefined,
	hidden: readonly string[]
): boolean {
	return isOffered(verdict) && !(verdict && hidden.includes(verdict.code));
}
