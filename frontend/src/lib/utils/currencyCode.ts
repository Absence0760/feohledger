/**
 * Free-entry ISO 4217 currency codes for a form field.
 *
 * `COMMON_CURRENCIES` is a shortlist for SUGGESTIONS. A `<select>` over it
 * cannot express a currency outside those sixteen codes, so a PLN, CZK, HUF,
 * CNY, HKD or AED statement could not be entered at all (decisions §377
 * follow-up). `ui/CurrencyInput.svelte` therefore takes any three-letter code,
 * suggests the shortlist first and the rest of ISO 4217 after it, and refuses
 * a blank or malformed one before submit, the same shape check the backend's
 * `normalize_iso_currency` applies.
 */
import { DEFAULT_CURRENCY, currencyOptions } from './money.ts';

const ISO_SHAPE = /^[A-Z]{3}$/;
// Checked BEFORE uppercasing: `toUpperCase()` maps some non-ASCII letters onto
// ASCII ("ß" → "SS"), so "uß" would otherwise pass as "USS". The backend's
// `utils/currency_code` applies the same order (decisions §381).
const ASCII_SHAPE = /^[A-Za-z]{3}$/;

/** `raw` uppercased and trimmed when it has the ISO 4217 shape, else `null`. */
export function normalizeCurrencyCode(raw: string | null | undefined): string | null {
	const code = (raw ?? '').trim();
	return ASCII_SHAPE.test(code) ? code.toUpperCase() : null;
}

/**
 * `text` with its ASCII letters uppercased and everything else left alone, for
 * a field that uppercases as the user types. A full `toUpperCase()` would turn
 * a typed "ß" into "SS" under the cursor and manufacture a valid-looking code.
 */
export function upperAsciiLetters(text: string): string {
	return text.replace(/[a-z]+/g, (s) => s.toUpperCase());
}

/** Why `raw` cannot be submitted as a currency, or `null` when it can. */
export function currencyCodeProblem(raw: string | null | undefined): 'required' | 'invalid' | null {
	const text = (raw ?? '').trim();
	if (text === '') return 'required';
	return normalizeCurrencyCode(text) === null ? 'invalid' : null;
}

/**
 * Every ISO 4217 code this runtime knows, from `Intl.supportedValuesOf`.
 *
 * Empty where the runtime has no such API. That only shortens the suggestion
 * list: free entry still accepts any well-formed code.
 */
export function isoCurrencyCodes(): string[] {
	const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
	if (typeof intl.supportedValuesOf !== 'function') return [];
	try {
		return intl.supportedValuesOf('currency').filter((c) => ISO_SHAPE.test(c));
	} catch {
		return [];
	}
}

/**
 * The suggestion list a currency field offers: `preferred` first, then the
 * common shortlist, then every other ISO code alphabetically. No duplicates.
 */
export function currencySuggestions(
	preferred?: string | null,
	all: readonly string[] = isoCurrencyCodes()
): string[] {
	const head = currencyOptions(preferred);
	const seen = new Set(head);
	const rest = [...all].filter((c) => ISO_SHAPE.test(c) && !seen.has(c)).sort();
	return [...head, ...rest];
}

/**
 * The value a currency field should hold after the org's currency (re)loads.
 *
 * A field the user has typed in keeps what they typed. Otherwise it follows
 * `preferred` once that resolves, and holds `DEFAULT_CURRENCY` only while it
 * has not: a store that lands after the modal opened used to leave the field
 * on the default, so a GBP organization's statement started as USD.
 */
export function seededCurrency(
	current: string,
	preferred: string | null | undefined,
	touched: boolean
): string {
	if (touched) return current;
	return normalizeCurrencyCode(preferred) ?? (current.trim() === '' ? DEFAULT_CURRENCY : current);
}
