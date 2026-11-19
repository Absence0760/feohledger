/**
 * Locale-aware percentage display — the percent counterpart of
 * `money.ts::formatMoney` and `time.ts::formatDate`.
 *
 * A hand-built `${n}%` is English punctuation pinned into every locale: French
 * and German put a (narrow no-break) space before the sign ("2 %"), and the
 * decimal separator follows the locale too ("12,5 %"). `Intl.NumberFormat`
 * with `style: 'percent'` owns both, read through the same active-locale
 * holder money and dates use, so a locale switch re-renders the figure.
 *
 * ## The input is in percentage POINTS (0–100), never a ratio
 *
 * Every percentage this API sends — discount tiers, `capture_rate_pct`,
 * `annualized_return_pct`, budget `utilization_pct` — is on the 0–100 scale:
 * `12.5` means twelve and a half percent. `formatPercent(12.5)` renders
 * "12.5%". There is deliberately no ratio mode: one scale means a caller can
 * never be off by a factor of a hundred.
 *
 * ## String decimals are shifted, not divided
 *
 * `Intl`'s percent style multiplies by 100, so the points have to be divided
 * first. Dividing a float re-introduces the drift a `Decimal` string from the
 * backend was sent to avoid (`1.005 / 100` is `0.010049999…`, which rounds
 * to "1%" at two places instead of "1.01%"). So the value is moved two decimal
 * places as TEXT and handed to `format()` as a decimal string, which
 * `Intl.NumberFormat` rounds exactly (ES2023 Intl.NumberFormat v3). A number
 * input goes through its shortest round-trip string the same way.
 */

import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

export type PercentPoints = number | string | null | undefined;

export interface FormatPercentOptions {
	/** Fewest decimals shown. Default 0. */
	minimumFractionDigits?: number;
	/** Most decimals shown (rounded half-up past this). Default 1. */
	maximumFractionDigits?: number;
	/** Rendered for null / blank / unreadable input. Default `—`. */
	fallback?: string;
}

// Optional sign, digits, optional fraction. No exponent — a backend `Decimal`
// never serialises one, and a number that would is handled below.
const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?$/;

/**
 * `points` as an exact decimal string on the RATIO scale (divided by 100), or
 * `null` when it is not a readable finite number.
 */
export function pointsToRatioString(points: PercentPoints): string | null {
	if (points === null || points === undefined) return null;
	let text: string;
	if (typeof points === 'number') {
		if (!Number.isFinite(points)) return null;
		text = String(points);
		// Tiny / huge floats stringify in exponent form; those are far outside
		// any display precision, so a plain division is exact enough.
		if (/e/i.test(text)) return String(points / 100);
	} else {
		text = points.trim();
	}
	const match = DECIMAL.exec(text);
	if (!match) return null;
	const [, sign, intPart = '', fracPart = ''] = match;
	if (!intPart && !fracPart) return null;
	// Move the point two places left: pad the integer part so it always has
	// at least three digits, then split two off its end.
	const padded = intPart.padStart(3, '0');
	const whole = padded.slice(0, -2).replace(/^0+(?=\d)/, '');
	const frac = padded.slice(-2) + fracPart;
	return `${sign === '-' ? '-' : ''}${whole}.${frac}`;
}

/**
 * Format a 0–100 percentage for the active locale.
 *
 * `formatPercent(12.5)` → "12.5%" (en), "12,5 %" (fr). `formatPercent('2')`
 * → "2%" / "2 %". Null, blank, or non-numeric input renders `fallback` —
 * never `NaN%`, and never a fabricated `0%`.
 */
export function formatPercent(points: PercentPoints, options: FormatPercentOptions = {}): string {
	const { minimumFractionDigits = 0, maximumFractionDigits, fallback = '—' } = options;
	const ratio = pointsToRatioString(points);
	if (ratio === null) return fallback;
	const formatter = new Intl.NumberFormat(getActiveFormatLocale(), {
		style: 'percent',
		minimumFractionDigits,
		maximumFractionDigits: Math.max(maximumFractionDigits ?? 1, minimumFractionDigits)
	});
	// A decimal STRING, so Intl rounds the exact value (see the module note).
	return formatter.format(ratio as `${number}`);
}
