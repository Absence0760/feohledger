/**
 * Reading a goods quantity typed into a form — receipts and inspections.
 *
 * The API takes a quantity as a decimal STRING with a dot, straight into a
 * `Numeric(12, 4)` column, so nothing here goes through a JS number. What the
 * user types is in their own locale, though: a German, French, Spanish or
 * Brazilian clerk writes `9,5`. Only the reader's OWN decimal separator is
 * accepted, never both — `1.000` is a thousand in German and `1,000` is a
 * thousand in English, so guessing would turn one into the other. Grouping
 * separators are refused outright for the same reason.
 */
import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

export type ParsedQuantity =
	| { kind: 'empty' }
	| { kind: 'valid'; value: string }
	| { kind: 'invalid' };

/** The decimal separator of `locale` (the active format locale by default). */
export function decimalSeparator(locale: string | undefined = getActiveFormatLocale()): string {
	try {
		const part = new Intl.NumberFormat(locale)
			.formatToParts(1.5)
			.find((p) => p.type === 'decimal');
		return part?.value ?? '.';
	} catch {
		return '.';
	}
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Parse `raw` as a non-negative quantity of at most 8 integer and 4 decimal
 *  digits, in `locale`'s notation. A valid one comes back dot-normalised. */
export function parseQuantity(
	raw: string,
	locale: string | undefined = getActiveFormatLocale()
): ParsedQuantity {
	const text = raw.trim();
	if (text === '') return { kind: 'empty' };
	const sep = decimalSeparator(locale);
	const shape = new RegExp(`^(\\d{1,8})(?:${escape(sep)}(\\d{1,4}))?$`);
	const m = shape.exec(text);
	if (!m) return { kind: 'invalid' };
	return { kind: 'valid', value: m[2] ? `${m[1]}.${m[2]}` : m[1] };
}

/** True for a valid quantity above zero. */
export function isPositiveQuantity(parsed: ParsedQuantity): boolean {
	return parsed.kind === 'valid' && /[1-9]/.test(parsed.value);
}

/** A dot-decimal quantity as the reader writes it — used to pre-fill a field. */
export function formatQuantityInput(
	value: number,
	locale: string | undefined = getActiveFormatLocale()
): string {
	const fixed = String(Math.round(value * 10_000) / 10_000);
	return fixed.replace('.', decimalSeparator(locale));
}
