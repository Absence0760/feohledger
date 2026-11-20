/**
 * Locale-aware count display: the count counterpart of `money.ts::formatMoney`,
 * `percent.ts::formatPercent` and `time.ts::formatDate`.
 *
 * A count (rows, invoices, requests, tokens, the N and M of "N of M") is a
 * whole number, and its digit grouping belongs to the reader's language:
 * "12,345" in English, "12.345" in German, "12 345" in French. A raw `{n}`
 * renders "12345" in every locale, and a bare `toLocaleString()` follows the
 * BROWSER's locale rather than the in-app one, so a German UI on an English
 * browser printed "12,345" inside a German sentence. This reads the same
 * active-locale holder money, dates and percentages use, so a locale switch
 * re-renders the figure.
 *
 * Counts are whole by definition, so nothing past the decimal point is shown
 * (`maximumFractionDigits: 0`). A figure that can carry a fraction is a
 * quantity or an amount, and has its own helper (`quantity.ts`, `money.ts`).
 *
 * The API sends some counters as strings (billing usage); a numeric string is
 * read, anything unreadable renders `fallback`, never `NaN` and never a
 * fabricated `0`.
 */

import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

export type CountValue = number | string | null | undefined;

export interface FormatCountOptions {
	/** Rendered for null / blank / unreadable input. Default `—`. */
	fallback?: string;
	/** BCP-47 locale. Default: the active in-app locale. */
	locale?: string;
}

const formatters = new Map<string, Intl.NumberFormat>();

function formatterFor(locale: string | undefined): Intl.NumberFormat {
	const key = locale ?? '';
	let fmt = formatters.get(key);
	if (!fmt) {
		try {
			fmt = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
		} catch {
			// A malformed locale tag throws RangeError; fall back to the runtime's.
			fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
		}
		formatters.set(key, fmt);
	}
	return fmt;
}

/**
 * Format a count for the active locale.
 *
 * `formatCount(12345)` → "12,345" (en), "12.345" (de), "12 345" (fr).
 * `formatCount('42')` → "42". `formatCount(null)` → "—".
 */
export function formatCount(n: CountValue, options: FormatCountOptions = {}): string {
	const { fallback = '—' } = options;
	if (n === null || n === undefined) return fallback;
	let value: number;
	if (typeof n === 'number') {
		value = n;
	} else {
		const text = n.trim();
		if (!/^[+-]?\d+(\.\d+)?$/.test(text)) return fallback;
		value = Number(text);
	}
	if (!Number.isFinite(value)) return fallback;
	// `getActiveFormatLocale()` is read on every call (not cached) so a template
	// that formats a count subscribes to locale switches.
	const locale = options.locale ?? getActiveFormatLocale();
	return formatterFor(locale).format(value);
}
