/**
 * Action verdicts — the server's answer to "would this action be accepted?",
 * read BEFORE the page offers the control (issue #575 § One rule, one place).
 *
 * A page used to decide whether to show Approve, Execute, Void, … by restating
 * the endpoint's rule (a status list, a role list, `uploaded_by !== me`), and
 * each copy drifted from the server in its own way: the uploader shown an
 * Approve that 403s, the run's creator shown an Execute that 403s, an
 * unverified vendor's invoice shown no way to be recorded paid at all (#573).
 * The backend now computes each verdict by running the SAME gate functions the
 * endpoint enforces with, and ships it as a coded refusal (`{code, message,
 * params}` — `backend/app/api/refusals.py::coded_refusal`) or `null`.
 *
 * Rules for a page reading one:
 *
 *  - **`null` means offer it; a refusal means do not** — render the control
 *    disabled with {@link refusalText} beside it when the reader can act on the
 *    reason (a colleague must approve), or not at all when it is about the
 *    reader's role (`role_not_permitted`).
 *  - **A verdict that has not arrived is a refusal** (fail closed): offer
 *    nothing until the server has answered.
 *  - **Never restate the rule beside it.** A second check in the page is
 *    exactly the copy this replaces.
 */
import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

/** A coded refusal the endpoint would answer with, or `null` when it accepts. */
export type ActionRefusal = {
	code: string;
	message: string;
	params?: Record<string, unknown>;
} | null;

/** The code for "your role / permissions do not allow this" — a reason the
 *  reader cannot act on, so the control is hidden rather than explained. */
export const ROLE_NOT_PERMITTED = 'role_not_permitted';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** The sentence to show for a refusal: localized from its code when this build
 *  knows it, else the server's English. Empty for `null`. */
export function refusalText(refusal: ActionRefusal, t: Translate): string {
	if (!refusal) return '';
	return localizeApiDetail(refusal, t) ?? refusal.message;
}

/** Whether a control should be rendered at all: hidden only when the verdict
 *  is unknown or refuses the reader's role. */
export function isOffered(refusal: ActionRefusal | undefined): boolean {
	return refusal !== undefined && refusal?.code !== ROLE_NOT_PERMITTED;
}

/** Whether a rendered control may be pressed. */
export function isAllowed(refusal: ActionRefusal | undefined): boolean {
	return refusal === null;
}
