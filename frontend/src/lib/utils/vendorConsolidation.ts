/**
 * The evidence pill on a vendor-consolidation cluster, in the reader's language.
 *
 * `GET /api/enrichment/vendors/consolidation-suggestions` states WHY vendors
 * were grouped as English evidence strings built by
 * `backend/app/services/vendor_consolidation.py::_pair_evidence`: exactly
 * `"same tax id"`, `"same vendor code"` or `"names <0.00–1.00> similar"`. This
 * maps those three shapes to message keys (the similarity becomes a localized
 * percentage). Anything else — a reason the backend adds later — is shown as
 * sent rather than blanked: an untranslated pill is visible and fixable, a
 * missing one hides the evidence.
 *
 * Pure (the caller passes `m` and the format locale) so vitest's node config
 * reaches it.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

const SIMILAR_NAMES = /^names (\d+(?:\.\d+)?) similar$/;

export function consolidationReasonLabel(reason: string, t: Translate, locale?: string): string {
	if (reason === 'same tax id') return t('vendors.consolidation.reason.sameTaxId');
	if (reason === 'same vendor code') return t('vendors.consolidation.reason.sameCode');
	const similar = SIMILAR_NAMES.exec(reason);
	if (similar) {
		const score = new Intl.NumberFormat(locale, {
			style: 'percent',
			maximumFractionDigits: 0
		}).format(Number(similar[1]));
		return t('vendors.consolidation.reason.similarNames', { score });
	}
	return reason;
}
