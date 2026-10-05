import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * Which notice — if any — the cash-position card owes the reader about where
 * its opening balance came from.
 *
 * `GET /api/analytics/cash-position` returns
 * `opening_balance_provider_skipped` when a live bank balance **existed** and
 * `services/cashflow.py::resolve_opening_balance` refused it, falling through
 * to the persisted figure (or zero). Without a distinct notice that state is
 * indistinguishable on the page from "no bank is connected" — same number, same
 * silence — and the two have different remedies: one is "connect a bank", the
 * other is "your bank IS connected, in the wrong currency".
 *
 * Route-local because it is one page's presentation rule, not a shared helper;
 * pure so the fallback below can be pinned without driving a browser.
 */
const SKIP_REASON_KEYS: Record<string, MessageKey> = {
	// The only reason the backend emits today: the funding account is
	// denominated in a currency other than the org's reporting currency, and
	// seeding the curve from it would make every running balance a silent
	// two-currency mixture.
	currency_mismatch: 'cfo.position.providerSkippedCurrency'
};

/** Generic line for a reason code this build doesn't recognise. */
const GENERIC_SKIP_KEY: MessageKey = 'cfo.position.providerSkipped';

/**
 * `null` when there is nothing to say; otherwise the message key to render.
 *
 * An unrecognised reason resolves to the generic line rather than to `null`:
 * the backend can add a reason code before the frontend learns its wording, and
 * on this surface silence is the bug — the CFO would go on reading a projection
 * that isn't seeded from the bank they connected. Under-explaining beats not
 * mentioning it.
 */
export function openingBalanceSkipKey(reason: string | null | undefined): MessageKey | null {
	if (typeof reason !== 'string') return null;
	const trimmed = reason.trim();
	if (!trimmed) return null;
	return SKIP_REASON_KEYS[trimmed] ?? GENERIC_SKIP_KEY;
}
