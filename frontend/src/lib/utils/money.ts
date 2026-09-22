/**
 * Locale-aware money formatting.
 *
 * Single source of truth for rendering currency across the app. Every
 * amount carries its own ISO 4217 currency code (USD, GBP, EUR, ZAR, …)
 * so a multi-currency tenant sees each line in its native symbol — never
 * a hardcoded `$`. Use `formatMoney` in scripts/`$derived`, and the
 * `<Money>` component in markup.
 *
 * Locale resolution: we pass `undefined` as the locale to
 * `Intl.NumberFormat`, which makes it honour the user's browser locale
 * (the runtime default). The currency *code* drives the symbol and the
 * minor-unit count; the *locale* drives grouping/decimal separators and
 * symbol placement. So a GBP amount renders "£1,234.50" for an en-US
 * visitor and "1.234,50 £" for a de-DE visitor — both correct.
 */

import { getActiveFormatLocale } from '$lib/i18n/formatLocale';

/**
 * The ISO 4217 code a FORM starts from — a picker's default, a new record's
 * initial value — when nothing better is known.
 *
 * Never a rendering fallback. `formatMoney` used to substitute this for any
 * code it was not given, which put a `$` on every figure whose currency the
 * server had deliberately declined to name (`docs/decisions.md` §79/§82, §160,
 * §198): a payment whose invoice carried no code read as dollars on the web
 * while mobile rendered it bare. A figure with no provable currency now renders
 * bare; see {@link formatMoney}.
 */
export const DEFAULT_CURRENCY = 'USD';

/**
 * Currencies offered in a picker, in the order they are shown.
 *
 * Deliberately a SHORTLIST, not all of ISO 4217: a 180-entry select is worse
 * to use than a short one, and the backend validates the code itself rather
 * than checking it against a frontend list — so this constrains the UI, never
 * the data. Always render it through {@link currencyOptions}, which folds in
 * the tenant's own reporting currency: a shortlist that can't express the
 * currency the org actually reports in would be a dead end, which is exactly
 * the failure mode the hardcoded `"USD"` on the credit-memo create had.
 */
export const COMMON_CURRENCIES = [
	'USD',
	'EUR',
	'GBP',
	'CAD',
	'AUD',
	'JPY',
	'CHF',
	'ZAR',
	'INR',
	'BRL',
	'MXN',
	'SGD',
	'NZD',
	'SEK',
	'NOK',
	'DKK'
] as const;

/**
 * {@link COMMON_CURRENCIES} with `preferred` guaranteed present and first.
 *
 * Pure and order-stable so a picker's option list is deterministic. An empty
 * or malformed `preferred` is ignored rather than inserted, so a store that
 * hasn't loaded yet can't put a blank option at the top of the list.
 */
export function currencyOptions(preferred?: string | null): string[] {
	const head = (preferred ?? '').trim().toUpperCase();
	const rest = COMMON_CURRENCIES.filter((c) => c !== head);
	return head.length === 3 ? [head, ...rest] : [...COMMON_CURRENCIES];
}

/**
 * An exact decimal money amount as the API serialises it — `"1234.50"`.
 *
 * The backend holds money as `Decimal` and writes it to JSON as a string so
 * no figure ever round-trips through a binary float. Type an API money field
 * as this, never `number`: a `number` field silently invites `.toFixed()`,
 * `a - b`, and `Math.max()` on currency, which is exactly the arithmetic the
 * Decimal invariant exists to prevent.
 */
export type MoneyString = string;

/**
 * Every shape a money amount arrives in at a render site: the exact decimal
 * string the API sends, a legacy JSON number from an endpoint not yet
 * migrated, or nothing at all.
 */
export type MoneyAmount = MoneyString | number | null | undefined;

export interface MoneyFormatOptions {
	/**
	 * ISO 4217 code — the one the figure's own payload names. Absent, empty or
	 * malformed means NOBODY established it, and the figure renders bare
	 * (grouped digits, no symbol) rather than under a borrowed default.
	 */
	currency?: string | null;
	/**
	 * BCP-47 locale. `undefined` (the default) uses the runtime/browser
	 * locale, which is what we want in the static SPA.
	 */
	locale?: string | undefined;
	/** Drop the fractional part (whole-currency display, e.g. dashboard KPIs). */
	whole?: boolean;
	/** Render as an accounting-style negative `($1,234.50)` rather than `-$1,234.50`. */
	accounting?: boolean;
}

/**
 * Normalise a currency code to an ISO 4217 string, or `null` when there is none
 * to normalise. Deliberately returns nothing rather than a default: the caller
 * decides what an unprovable currency looks like, and `formatMoney`'s answer is
 * "no symbol".
 */
function resolveCurrency(currency?: string | null): string | null {
	const code = (currency ?? '').trim().toUpperCase();
	return /^[A-Z]{3}$/.test(code) ? code : null;
}

/**
 * A figure with NO currency symbol — the honest rendering when no currency can
 * be proven. Grouping and decimal separators still follow `locale`, so the
 * figure sits consistently beside labelled ones; two fraction digits (none for
 * `whole`) because no currency means no minor-unit count to read, and two is
 * what every money column holds. `accounting` keeps its parentheses, which
 * `Intl` only offers alongside a currency symbol.
 */
function formatBare(n: number, locale: string | undefined, options: MoneyFormatOptions): string {
	const places = options.whole ? 0 : 2;
	const fmt = new Intl.NumberFormat(locale, {
		minimumFractionDigits: places,
		maximumFractionDigits: places
	});
	return options.accounting && n < 0 ? `(${fmt.format(-n)})` : fmt.format(n);
}

/**
 * Coerce the many shapes amounts arrive in (number, string-Decimal from
 * the API, null) to a finite number. Returns `null` when there is no
 * usable value so the caller can render a placeholder.
 */
function toNumber(amount: MoneyAmount): number | null {
	if (amount === null || amount === undefined || amount === '') return null;
	const n = typeof amount === 'number' ? amount : Number(amount);
	return Number.isFinite(n) ? n : null;
}

/**
 * Parse an exact-decimal money amount into a plain `number` for **layout and
 * ordering only** — a chart bar's width, the `Math.max()` that sets a chart's
 * scale, a sort key.
 *
 * This is the one sanctioned way a money string becomes a JS number, and it is
 * named for its purpose so the name refuses the wrong use at the call site.
 * The result is a *geometry* input, never a figure:
 *
 * - **Never render it.** Use `formatMoney` / `<Money>` on the original string.
 * - **Never add, subtract or scale two of them into a figure a user reads.**
 *   That is float money math; render both figures and let the backend own any
 *   delta (`sumMoney` exists for the narrow display-total case it documents).
 * - **Never compare two amounts for a business decision.** Use
 *   `isPositiveAmount` / `isNegativeAmount`, or ask the backend.
 *
 * Returns `0` for null / empty / unparseable input so a chart can't render a
 * `NaN%` width off a missing field.
 */
export function parseMoneyForLayout(amount: MoneyAmount): number {
	return toNumber(amount) ?? 0;
}

/**
 * Is this a strictly-positive amount?
 *
 * A *predicate*, not arithmetic — it never feeds a rendered figure, so
 * there is no rounding to get wrong. Use it to decide whether a
 * string-Decimal from the API is worth showing at all (e.g. the 1099
 * report's card-excluded total, which is only meaningful when non-zero);
 * never to add, subtract or compare two amounts against each other.
 */
export function isPositiveAmount(amount: MoneyAmount): boolean {
	const n = toNumber(amount);
	return n !== null && n > 0;
}

/**
 * Is this a strictly-negative amount?
 *
 * The mirror of {@link isPositiveAmount}, and a *predicate* for the same
 * reason: it decides whether to tint a figure as a loss (an unrealized FX
 * gain/loss column, a negative proration), never what that figure reads as.
 * An absent / unparseable amount is not a loss, so it returns `false`.
 */
export function isNegativeAmount(amount: MoneyAmount): boolean {
	const n = toNumber(amount);
	return n !== null && n < 0;
}

/**
 * Format an amount as locale-aware currency.
 *
 * The currency is the one the figure's own payload names. When there is none —
 * `null`, empty, malformed — the figure renders **bare**: grouped figures, no
 * symbol. That is the answer, not a degraded mode (`docs/decisions.md` §160,
 * §198). A `null` code is where the server refused to guess, so substituting
 * one would reinstate the fabrication the field exists to end; a missing symbol
 * is a visible gap a reader can ask about, a wrong one is a wrong number that
 * looks right. It is not the `placeholder` either: that means *no figure*, and
 * here there is a figure whose label is merely unknown.
 *
 * @returns the formatted string, or `placeholder` (default `—`) when the
 *          amount is null/empty/non-finite.
 */
export function formatMoney(
	amount: MoneyAmount,
	options: MoneyFormatOptions = {},
	placeholder = '—'
): string {
	const n = toNumber(amount);
	if (n === null) return placeholder;

	// Default to the active in-app locale (i18n picker) when the caller
	// passes none; falls back to `undefined` (browser locale) pre-selection.
	const locale = options.locale ?? getActiveFormatLocale();
	const currency = resolveCurrency(options.currency);
	if (currency === null) return formatBare(n, locale, options);
	const fmtOptions: Intl.NumberFormatOptions = {
		style: 'currency',
		currency,
		currencyDisplay: 'symbol'
	};
	if (options.whole) {
		fmtOptions.minimumFractionDigits = 0;
		fmtOptions.maximumFractionDigits = 0;
	}
	if (options.accounting) {
		// `currencySign: 'accounting'` wraps negatives in parentheses
		// using the locale's accounting convention.
		fmtOptions.currencySign = 'accounting';
	}

	try {
		return new Intl.NumberFormat(locale, fmtOptions).format(n);
	} catch {
		// A code `Intl` rejects is one nobody can render a symbol for. Bare,
		// never a borrowed code — and never a thrown table render.
		return formatBare(n, locale, options);
	}
}

/**
 * Sum exact Decimal-string money amounts without introducing float
 * rounding artifacts (the classic `0.1 + 0.2` class of bug).
 *
 * Money arrives from the API as Decimal strings (e.g. "10.10"). Reducing
 * with `sum + Number(amount)` coerces each one to a binary float before
 * adding, so a running total can drift off the exact cent value even
 * though every individual amount is exact. This instead parses each
 * amount's sign/integer/fraction textually, scales every value to the
 * widest fraction length seen (via `BigInt`, so scaling never touches a
 * float), sums as exact integers, and only converts back to `number`
 * once at the very end — a single, lossless-for-display conversion
 * rather than one per addend.
 *
 * Null/undefined/empty/non-numeric entries are skipped (treated as 0),
 * mirroring the `Number(x ?? 0)` call sites this replaces. Returns `0`
 * for an empty or all-skipped input.
 */
export function sumMoney(amounts: Iterable<MoneyAmount>): number {
	let maxScale = 0;
	const parsed: { negative: boolean; digits: string; scale: number }[] = [];

	for (const raw of amounts) {
		if (raw === null || raw === undefined || raw === '') continue;
		const str = typeof raw === 'number' ? String(raw) : raw.trim();
		if (str === '') continue;
		const match = /^(-)?(\d+)(?:\.(\d+))?$/.exec(str);
		if (!match) continue; // not a plain decimal string — skip rather than throw on a display path
		const [, sign, intPart, fracPart = ''] = match;
		if (fracPart.length > maxScale) maxScale = fracPart.length;
		parsed.push({ negative: sign === '-', digits: intPart + fracPart, scale: fracPart.length });
	}

	if (parsed.length === 0) return 0;

	let totalScaled = 0n;
	for (const { negative, digits, scale } of parsed) {
		const scaledDigits = digits + '0'.repeat(maxScale - scale);
		const value = BigInt(scaledDigits);
		totalScaled += negative ? -value : value;
	}

	const divisor = 10 ** maxScale;
	return Number(totalScaled) / divisor;
}

/**
 * Multiply a money amount by a non-money factor, exactly.
 *
 * `sumMoney` handles addition losslessly, but several previews scale money by
 * a quantity or a percentage — a requisition line's `quantity * unit_price`, a
 * discount tier's `base_amount * percent / 100`. Doing that in `number` is the
 * float arithmetic the `MoneyAmount` type exists to prevent, and
 * `parseMoneyForLayout` is explicitly not for figures that get rendered.
 *
 * Both operands are parsed as plain decimals, multiplied as `BigInt`s at their
 * combined scale, then rounded HALF_UP to `scale` (default 2, matching the
 * backend's `ROUND_HALF_UP` money quantisation). `divideBy` folds a constant
 * divisor into the same exact step so a percentage does not need a lossy
 * `percent / 100` first.
 *
 * Returns `null` when either operand is missing or is not a plain decimal —
 * the caller shows a dash rather than a wrong number. This is deliberate: a
 * preview that silently repairs unreadable input is how a bad figure reaches a
 * form field the user then trusts.
 */
export function scaleMoney(
	amount: MoneyAmount,
	factor: MoneyAmount,
	options: { scale?: number; divideBy?: number } = {}
): MoneyString | null {
	const { scale = 2, divideBy = 1 } = options;
	if (!Number.isInteger(scale) || scale < 0) return null;
	if (!Number.isInteger(divideBy) || divideBy <= 0) return null;

	const a = parseDecimal(amount);
	const b = parseDecimal(factor);
	if (a === null || b === null) return null;

	const negative = a.negative !== b.negative;
	// Exact product at the combined scale, then re-scaled to `scale` in one
	// rounding step — never two, which is where a half-cent goes missing.
	const product = a.digits * b.digits;
	const numerator = product * 10n ** BigInt(scale);
	const denominator = 10n ** BigInt(a.scale + b.scale) * BigInt(divideBy);

	let quotient = numerator / denominator;
	const remainder = numerator % denominator;
	if (remainder * 2n >= denominator) quotient += 1n; // HALF_UP on the magnitude

	const digits = quotient.toString().padStart(scale + 1, '0');
	const whole = digits.slice(0, digits.length - scale);
	const frac = scale > 0 ? `.${digits.slice(digits.length - scale)}` : '';
	// -0.00 is not a money figure anyone wants to read.
	const sign = negative && quotient !== 0n ? '-' : '';
	return `${sign}${whole}${frac}`;
}

/** Shared plain-decimal parse. `null` for anything that is not one. */
function parseDecimal(
	raw: MoneyAmount
): { negative: boolean; digits: bigint; scale: number } | null {
	if (raw === null || raw === undefined || raw === '') return null;
	const str = typeof raw === 'number' ? String(raw) : raw.trim();
	const match = /^(-)?(\d+)(?:\.(\d+))?$/.exec(str);
	if (!match) return null;
	const [, sign, intPart, fracPart = ''] = match;
	return {
		negative: sign === '-',
		digits: BigInt(intPart + fracPart),
		scale: fracPart.length
	};
}
