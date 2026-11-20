/**
 * Locale-aware durations: a number of days or hours, with its unit.
 *
 * A hand-built `${n.toFixed(1)}d` pins English twice: the decimal point
 * ("12.5d" where German writes "12,5 T") and the unit itself, and a
 * `${n} days` label has no singular. `Intl.NumberFormat`'s `unit` style owns
 * the separator, the unit word and its plural for every shipped locale, read
 * through the same active-locale holder money, percentages and dates use, so a
 * locale switch re-renders the figure.
 *
 * `Intl` rather than ICU plural message keys, for the reason `time.ts` gives:
 * this module stays importable under vitest's node environment, and it needs
 * no catalogue keys to stay correct in six locales.
 *
 * Pick the unit display by where the figure sits: `narrow` ("12.5d") for a
 * dense cell or KPI, `long` ("12.5 days") for an accessible name or a sentence.
 */
import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

export interface FormatDurationOptions {
	/** `narrow` "12.5d", `short` "12.5 days" / "12,5 Tg.", `long` "12.5 days". Default `narrow`. */
	unitDisplay?: 'narrow' | 'short' | 'long';
	/** Fewest decimals shown. Default 0. */
	minimumFractionDigits?: number;
	/** Most decimals shown. Default 1. */
	maximumFractionDigits?: number;
	/** Rendered for null / non-finite input. Default `—`. */
	fallback?: string;
}

function formatUnit(
	value: number | null | undefined,
	unit: 'day' | 'hour',
	options: FormatDurationOptions
): string {
	const {
		unitDisplay = 'narrow',
		minimumFractionDigits = 0,
		maximumFractionDigits,
		fallback = '—'
	} = options;
	if (value === null || value === undefined || !Number.isFinite(value)) return fallback;
	return new Intl.NumberFormat(getActiveFormatLocale(), {
		style: 'unit',
		unit,
		unitDisplay,
		minimumFractionDigits,
		maximumFractionDigits: Math.max(maximumFractionDigits ?? 1, minimumFractionDigits)
	}).format(value);
}

/** A number of days: `formatDays(12.5)` → "12.5d" (en), "12,5 T" (de). */
export function formatDays(
	days: number | null | undefined,
	options: FormatDurationOptions = {}
): string {
	return formatUnit(days, 'day', options);
}

/** A number of hours: `formatHours(1.5, { unitDisplay: 'long' })` → "1.5 hours". */
export function formatHours(
	hours: number | null | undefined,
	options: FormatDurationOptions = {}
): string {
	return formatUnit(hours, 'hour', options);
}
