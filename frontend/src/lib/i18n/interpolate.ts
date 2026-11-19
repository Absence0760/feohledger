// Message-string substitution for the i18n runtime. Extracted from the
// reactive runtime (store.svelte.ts) so it unit-tests without the Svelte
// compiler — the runes file can't be plain-imported under vitest's node
// environment.
//
// Two layers:
//   1. ICU plural selection. A value may contain a `{var, plural, …}`
//      block whose branch is chosen by Intl.PluralRules for the ACTIVE
//      locale, so "{n, plural, one {# invoice} other {# invoices}}"
//      renders "1 invoice" / "2 invoices" without per-call-site ternaries.
//      `#` inside the chosen branch is replaced by the count, grouped for
//      the locale as ICU specifies ("1,234" / "1.234" / "1 234" via
//      `formatCount`), so a plural never prints raw "1234". Locales
//      declare only the CLDR categories they need; a missing selected
//      category falls back to `other`.
//   2. Plain `{placeholder}` substitution over whatever the plural layer
//      produced (and over non-plural templates). Replacement is by literal
//      string (not regex), so a value with regex-special characters is
//      substituted verbatim, and an unreferenced placeholder is left
//      intact rather than blanked.

import { formatCount } from '#lib/utils/count.ts';

const PLURAL_OPEN = /\{(\w+),\s*plural,\s*/g;

// Find the index of the `}` that closes the brace opened at `open`,
// counting nested `{ }` so a branch message may itself contain `{…}`.
function matchingBrace(s: string, open: number): number {
	let depth = 0;
	for (let i = open; i < s.length; i++) {
		if (s[i] === '{') depth++;
		else if (s[i] === '}') {
			depth--;
			if (depth === 0) return i;
		}
	}
	return -1;
}

// Parse the category → message map out of a plural block body such as
// `one {# invoice} other {# invoices}` (also `=0 {…}` exact-value matches
// and `many {…}`). Brace-balanced so a branch message may itself contain
// `{placeholder}` tokens.
function parseBranches(body: string): Record<string, string> {
	const branches: Record<string, string> = {};
	let i = 0;
	while (i < body.length) {
		while (i < body.length && /\s/.test(body[i])) i++;
		const labelStart = i;
		while (i < body.length && body[i] !== '{') i++;
		const label = body.slice(labelStart, i).trim();
		if (i >= body.length || body[i] !== '{') break;
		const close = matchingBrace(body, i);
		if (close < 0) break;
		if (label) branches[label] = body.slice(i + 1, close);
		i = close + 1;
	}
	return branches;
}

function resolvePlural(
	template: string,
	params: Record<string, string | number> | undefined,
	locale: string,
): string {
	let out = template;
	// Resolve every plural block in the string (most have one, but a value
	// could mix two counts). Each pass replaces the first remaining block.
	for (let guard = 0; guard < 8; guard++) {
		PLURAL_OPEN.lastIndex = 0;
		const match = PLURAL_OPEN.exec(out);
		if (!match) break;
		const blockStart = match.index;
		const varName = match[1];
		const bodyStart = match.index + match[0].length;
		const blockEnd = matchingBrace(out, blockStart);
		if (blockEnd < 0) break;
		const branches = parseBranches(out.slice(bodyStart, blockEnd));
		const raw = params?.[varName];
		const count = typeof raw === 'number' ? raw : Number(raw ?? 0);
		let chosen: string | undefined;
		if (Number.isFinite(count)) {
			const exact = branches[`=${count}`];
			chosen = exact ?? branches[pluralCategory(count, locale)] ?? branches.other;
		}
		chosen ??= branches.other ?? '';
		// A whole count is grouped for the locale; a fractional plural value
		// (none today) keeps its digits rather than being rounded to a count.
		const shown = Number.isInteger(count)
			? formatCount(count, { locale, fallback: String(count) })
			: String(count);
		const rendered = chosen.replaceAll('#', shown);
		out = out.slice(0, blockStart) + rendered + out.slice(blockEnd + 1);
	}
	return out;
}

// Intl.PluralRules is cached per locale — constructing one per call is
// measurably slower and the locale set is tiny.
const pluralRulesCache = new Map<string, Intl.PluralRules>();
function pluralCategory(count: number, locale: string): string {
	let rules = pluralRulesCache.get(locale);
	if (!rules) {
		try {
			rules = new Intl.PluralRules(locale);
		} catch {
			rules = new Intl.PluralRules('en');
		}
		pluralRulesCache.set(locale, rules);
	}
	return rules.select(count);
}

export function interpolate(
	template: string,
	params?: Record<string, string | number>,
	locale: string = 'en',
): string {
	let out = template;
	if (out.includes(', plural,')) out = resolvePlural(out, params, locale);
	if (!params) return out;
	for (const [key, value] of Object.entries(params)) {
		out = out.replaceAll(`{${key}}`, String(value));
	}
	return out;
}
