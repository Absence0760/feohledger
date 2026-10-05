/**
 * Locale-aware list joining.
 *
 * The third `Intl`-backed formatter in this directory, alongside
 * `money.ts::formatMoney` and `time.ts`'s date / relative-time helpers, and it
 * reads the same active-locale holder (`i18n/formatLocale`) they do — so the
 * locale picker moves the separators inside an enumeration at the same moment
 * it moves decimal separators and month names.
 *
 * ## Why a helper rather than `.join(', ')`
 *
 * `', '` is an English punctuation rule wearing a string literal's clothes.
 * Japanese separates list items with the ideographic comma `、` and no space;
 * German and Spanish join the final pair with a word (`und` / `y`) rather than
 * a comma. A translated sentence with an ASCII-comma list spliced into it
 * therefore reads as half-localized in three of the six shipped locales — and
 * the mismatch is loudest exactly where it matters most: inside a
 * screen-reader announcement, where the separator is what produces the pause
 * between items.
 *
 * ## What it does NOT cover
 *
 * Only lists a HUMAN READS AS PROSE. A `.join(', ')` whose output is parsed
 * back (a text input re-split on `,`), sent over the wire (a query parameter,
 * a CSV cell), or which is a bare list of machine identifiers (role slugs, API
 * scopes, webhook event types) must keep its literal separator: swapping in a
 * locale-dependent one there is a correctness bug, not a translation fix.
 *
 * Pure — no `$state`, no `fetch`, no Svelte compiler — so it unit-tests under
 * the plain-Node vitest config exactly like `money.ts` and `time.ts`.
 */

import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

/**
 * How a list reads once joined.
 *
 * - `plain` — an enumeration with no connecting word in English. This is the
 *   drop-in replacement for `.join(', ')`: `conjunction` at `narrow` width
 *   renders `"A, B, C"` in en / fr / pt-BR (character-identical to the literal
 *   it replaces), `"A、B、C"` in ja, and `"A, B und C"` / `"A, B y C"` in
 *   de / es — which is what those languages actually do with a list. `unit` was
 *   rejected as the plain shape: its Japanese form is SPACE-separated
 *   (`"A B C"`) and drops the comma entirely, the opposite of the fix.
 * - `and` — an explicit conjunction (`"A, B, and C"`).
 * - `or` — an explicit disjunction (`"A, B, or C"`).
 */
export type ListJoinStyle = 'plain' | 'and' | 'or';

const STYLE_OPTIONS: Record<
	ListJoinStyle,
	{ type: 'conjunction' | 'disjunction'; style: 'long' | 'narrow' }
> = {
	plain: { type: 'conjunction', style: 'narrow' },
	and: { type: 'conjunction', style: 'long' },
	or: { type: 'disjunction', style: 'long' }
};

/**
 * The separator every call site used before this helper, and the fallback
 * whenever `Intl.ListFormat` cannot produce an answer. Degrading to the
 * previous behaviour is the honest failure mode: an English-punctuated list is
 * what shipped yesterday, and it beats a thrown `RangeError` taking a table
 * render down.
 */
const LITERAL_SEPARATOR = ', ';

/**
 * `Intl.ListFormat` is not free to construct and `formatList` runs once per row
 * in a couple of tables, so memoize per (locale, style) pair — the same
 * treatment `time.ts` gives `Intl.RelativeTimeFormat`. The active locale is
 * part of the key, so a locale switch never serves a stale formatter. A `null`
 * entry is a cached negative: the environment or the tag can't build one.
 */
const formatters = new Map<string, Intl.ListFormat | null>();

function listFormatter(locale: string | undefined, style: ListJoinStyle): Intl.ListFormat | null {
	const key = `${locale ?? ''}|${style}`;
	const cached = formatters.get(key);
	if (cached !== undefined) return cached;
	let fmt: Intl.ListFormat | null = null;
	try {
		// Guarded rather than assumed: `Intl.ListFormat` is missing from a few
		// older embedded WebViews, and a malformed locale tag (the holder carries
		// whatever `navigator.language` reports) throws `RangeError` from the
		// constructor. The result is cached either way, so a failing environment
		// pays the throw once rather than once per row.
		if (typeof Intl.ListFormat === 'function') {
			fmt = new Intl.ListFormat(locale, STYLE_OPTIONS[style]);
		}
	} catch {
		fmt = null;
	}
	formatters.set(key, fmt);
	return fmt;
}

/**
 * Join translated / human-readable fragments with the active locale's own list
 * punctuation.
 *
 * Reading the locale here is reactive (see `i18n/formatLocale`), so a
 * `$derived` or a template calling this re-renders on a locale switch.
 *
 * A nullish list, and one with no entries, both render as the empty string —
 * matching `[].join(', ')`. Entries are coerced with `String()` because
 * `Intl.ListFormat#format` throws a `TypeError` on a non-string element, and a
 * number is a perfectly reasonable thing to enumerate.
 */
export function formatList(
	items: readonly (string | number)[] | null | undefined,
	style: ListJoinStyle = 'plain'
): string {
	if (!Array.isArray(items) || items.length === 0) return '';
	const parts = items.map((item) => String(item));
	const fmt = listFormatter(getActiveFormatLocale(), style);
	if (!fmt) return parts.join(LITERAL_SEPARATOR);
	try {
		return fmt.format(parts);
	} catch {
		return parts.join(LITERAL_SEPARATOR);
	}
}

/**
 * Test seam: drop the memoized formatters so a suite can exercise the
 * construction guard against a cold cache instead of a warm one. Not called by
 * application code.
 */
export function __resetListFormatterCache(): void {
	formatters.clear();
}
