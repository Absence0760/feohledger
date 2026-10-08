/**
 * What to tell the operator about the live virtual cards a vendor write left
 * behind (`backend/app/services/vendor_card_revocation.py`).
 *
 * Rejecting, deactivating, blocking or sanctions-matching a vendor cancels its
 * live cards provider-first. A card the provider did not confirm closed, or one
 * behind a live payment (closed only by voiding that payment), is STILL
 * SPENDABLE — so it must reach the screen, never just the response body. Cards
 * that did close need no warning; the action's own success toast covers them.
 */
import type { VendorCardRevocation } from '#lib/types/vendor.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** One warning per kind of still-live card, summed across every vendor the
 * call touched (bulk / merge return several). Empty when nothing is live. */
export function cardRevocationWarnings(
	revocations: ReadonlyArray<VendorCardRevocation | null | undefined> | null | undefined,
	t: Translate
): string[] {
	let notClosed = 0;
	let requiresVoid = 0;
	for (const r of revocations ?? []) {
		if (!r) continue;
		notClosed += r.not_closed.length;
		requiresVoid += r.requires_payment_void.length;
	}
	const out: string[] = [];
	if (notClosed > 0) out.push(t('vendors.cards.notClosed', { n: notClosed }));
	if (requiresVoid > 0) out.push(t('vendors.cards.requiresVoid', { n: requiresVoid }));
	return out;
}
