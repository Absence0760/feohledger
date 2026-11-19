// The supplier portal's PO-flip verdicts. Kept apart from `purchaseOrder.ts`
// because these sentences live in the PORTAL catalogue slice, and the internal
// `/purchase-orders` page reads `purchaseOrder.ts` without loading that slice
// (`slices.test.ts` holds the line).
import { refusalText, type ActionRefusal } from '#lib/api/actionVerdicts.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/**
 * The supplier portal's PO-flip refusals (`backend/app/api/portal.py::
 * po_flip_refusal`), carried per row as `flip_verdict` and answered by
 * `POST /api/portal/purchase-orders/{id}/flip`.
 */
export const PO_NOT_INVOICEABLE = 'po_not_invoiceable';
export const PO_ALREADY_INVOICED = 'po_already_invoiced';
export const PO_TOTAL_UNVERIFIABLE = 'po_total_unverifiable';

/**
 * The sentence for a flip refusal, in the reader's language when this build
 * knows the code, else the server's English (`refusalText`). The unvouched
 * total has its own note above the portal table, so it is stated there.
 */
export function poFlipRefusalText(refusal: ActionRefusal | undefined, t: Translate): string {
	if (!refusal) return '';
	if (refusal.code === PO_NOT_INVOICEABLE) return t('portal.po.refusal.notInvoiceable');
	if (refusal.code === PO_ALREADY_INVOICED) return t('portal.po.refusal.alreadyInvoiced');
	if (refusal.code === PO_TOTAL_UNVERIFIABLE) return t('portal.po.totalUnverifiableNote');
	return refusalText(refusal, t);
}
