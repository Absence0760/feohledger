/**
 * Localizing an invoice warning — the client half of
 * `backend/app/services/invoice_warning_catalog.py`.
 *
 * `decisions.md` §155 shipped a localized FRAME around server-English findings
 * and said plainly that it was not the fix: each finding is composed per row
 * from that row's own data ("PO 4412 not found", a variance, an amount), so a
 * German reader got German chrome and English prose. The backend now emits a
 * stable `code` plus typed `params` beside `message`, and this module turns
 * that pair into a message key and a set of locale-formatted parameters.
 *
 * Two properties make it safe to render:
 *
 * - **Unknown code → `null`**, and the caller renders `warning.message`, the
 *   backend's own sentence. Every warning persisted before the catalogue
 *   existed has no `code` at all (`refresh_warnings` re-derives one on the
 *   invoice's next write, and nothing backfills), so the fallback is the
 *   NORMAL path for an untouched row, not an edge case.
 * - **A known code with a missing parameter → `null` too.** `interpolate.ts`
 *   leaves an unfilled `{placeholder}` intact rather than blanking it, so a
 *   client/backend skew would otherwise print braces at a reviewer. The
 *   server's complete sentence is strictly better than a broken translated one.
 *
 * Deliberately imports NOTHING from the Svelte runtime — the caller passes
 * `m` in, so this stays unit-testable under vitest's node environment. Same
 * arrangement as `einvoiceIssues.ts` under `einvoice.ts`.
 */
import {
	INVOICE_WARNING_MESSAGE_KEYS,
	INVOICE_WARNING_PARAM_KINDS,
	type WarningParamKind
} from '$lib/api/invoiceWarningMessages.generated';
import { getActiveFormatLocale } from '$lib/i18n/formatLocale';
import type { MessageKey } from '$lib/i18n/messages';
import { formatMoney } from '$lib/utils/money';
import { formatDate } from '$lib/utils/time';

/** The shape this module needs off an `InvoiceWarning` (see `types/invoice.ts`). */
export interface LocalizableWarning {
	message: string;
	code?: string | null;
	params?: Record<string, string | number> | null;
}

/** A resolved key plus its locale-formatted parameters, ready for `m()`. */
export interface LocalizedWarning {
	key: MessageKey;
	params: Record<string, string | number>;
}

/** `m` from `$lib/i18n/store.svelte`, passed in so this module stays pure. */
export type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

const KEYS: Record<string, MessageKey> = INVOICE_WARNING_MESSAGE_KEYS;
const KINDS: Record<string, Record<string, WarningParamKind>> = INVOICE_WARNING_PARAM_KINDS;

/**
 * Decimal places the backend actually measured, read off its own digits.
 *
 * The precision is data, not a formatting choice: a duplicate similarity is
 * `98` (no decimals) and a PO variance is `+20.0` (one), and re-deciding that
 * here would either lose a digit or invent one. Same trick for the sign —
 * `+20.0` is signed because the direction matters, `98` is not.
 */
function decimalsIn(raw: string): number {
	const dot = raw.indexOf('.');
	return dot < 0 ? 0 : raw.length - dot - 1;
}

function numberFormat(raw: string, extra: Intl.NumberFormatOptions): string {
	const n = Number(raw);
	if (!Number.isFinite(n)) return raw;
	const digits = decimalsIn(raw);
	try {
		return new Intl.NumberFormat(getActiveFormatLocale(), {
			minimumFractionDigits: digits,
			maximumFractionDigits: digits,
			signDisplay: raw.trimStart().startsWith('+') ? 'exceptZero' : 'auto',
			...extra
		}).format(extra.style === 'percent' ? n / 100 : n);
	} catch {
		// A malformed locale tag throws RangeError. The backend's own digits are
		// a worse-looking but correct figure — never a blank.
		return raw;
	}
}

function formatParam(
	kind: WarningParamKind,
	value: string | number,
	currency: string | undefined
): string | number {
	switch (kind) {
		case 'count':
			// Stays a number: it drives ICU plural selection in `interpolate.ts`,
			// which reads it through `Intl.PluralRules`.
			return Number(value);
		case 'money':
			// The backend sends exact decimal digits (money is `Decimal`, never
			// float). `formatMoney` takes the string straight through.
			return formatMoney(String(value), { currency });
		case 'percent':
			return numberFormat(String(value), { style: 'percent' });
		case 'number':
			return numberFormat(String(value), {});
		case 'date':
			return formatDate(String(value), String(value));
		case 'currency':
		case 'text':
			return String(value);
	}
}

/**
 * The message key + formatted params for one warning, or `null` when the code
 * is unknown, absent, or its parameters are incomplete — see the module note.
 */
export function localizeInvoiceWarning(w: LocalizableWarning): LocalizedWarning | null {
	if (!w.code) return null;
	const key = KEYS[w.code];
	if (!key) return null;
	const kinds = KINDS[w.code] ?? {};
	const raw = w.params ?? {};
	const currency = typeof raw.currency === 'string' ? raw.currency : undefined;
	const params: Record<string, string | number> = {};
	for (const [name, kind] of Object.entries(kinds)) {
		const value = raw[name];
		if (value === undefined || value === null) return null;
		params[name] = formatParam(kind, value, currency);
	}
	return { key, params };
}

/**
 * The sentence to show for one warning: localized when the code is known,
 * otherwise the backend's own `message`.
 *
 * Both render sites — the `/invoices` row icon's `aria-label`/`title` and
 * `InvoiceModal`'s warnings list — go through this, so neither can end up
 * localizing a finding the other renders in English.
 */
export function invoiceWarningText(w: LocalizableWarning, translate: Translate): string {
	const localized = localizeInvoiceWarning(w);
	return localized ? translate(localized.key, localized.params) : w.message;
}
