import { describe, expect, it } from 'vitest';

/**
 * Counts and dates render through the locale-aware helpers, never the
 * browser's locale or a pinned one.
 *
 * - A bare `toLocaleString()` (no locale argument) formats in the BROWSER's
 *   locale, not the in-app one: a German UI on an English browser printed
 *   "12,345" inside a German sentence. Use `formatCount` (`utils/count.ts`)
 *   for a count, `formatMoney` / `formatPercent` / `formatDate` for the rest.
 * - A `toLocale*String('en-US', …)` pins every reader to US formatting.
 * - `new Intl.NumberFormat(locale).format(n)` with no options is a hand-rolled
 *   `formatCount`; six components each carried one before the shared helper.
 * - In a `.svelte` file, `.toFixed(` is a figure in English notation ("12.5d"
 *   in a German page) — use `formatDays` / `formatHours` (`utils/duration.ts`),
 *   `formatPercent` or `formatCount`. And a component-local
 *   `new Intl.RelativeTimeFormat` duplicates `utils/time.ts` (`timeAgo`,
 *   `formatDueIn`), which reads the active locale and memoizes the formatter.
 *
 * The `formatters` themselves (`utils/money.ts`, `percent.ts`, `time.ts`,
 * `count.ts`, `quantity.ts`, `duration.ts`) are where `Intl` belongs and are
 * not scanned.
 */
const RAW = import.meta.glob('/src/**/*.{svelte,ts}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const FORMATTERS = new Set([
	'/src/lib/utils/count.ts',
	'/src/lib/utils/duration.ts',
	'/src/lib/utils/money.ts',
	'/src/lib/utils/percent.ts',
	'/src/lib/utils/quantity.ts',
	'/src/lib/utils/time.ts'
]);

/**
 * Files another lane of the same round is editing, left for it to convert so
 * two branches do not conflict on them. Each entry names the sites still open.
 * Remove an entry once its file is converted; the test fails on a stale one.
 */
const PENDING_OTHER_LANE: Record<string, string> = {};

const RULES: { name: string; pattern: RegExp; svelteOnly?: boolean }[] = [
	{ name: 'bare toLocaleString()', pattern: /\.toLocale(?:Date|Time)?String\(\s*\)/ },
	{
		name: "pinned locale in toLocale*String('xx-YY')",
		pattern: /\.toLocale(?:Date|Time)?String\(\s*['"`][a-z]{2}/
	},
	{
		name: 'options-less Intl.NumberFormat (use formatCount)',
		pattern: /new Intl\.NumberFormat\(\s*[^,()]*(?:\(\s*\))?\s*\)\s*\.format\(/
	},
	{
		name: '.toFixed( in a component (use formatDays / formatHours / formatPercent / formatCount)',
		pattern: /\.toFixed\(/,
		svelteOnly: true
	},
	{
		name: 'component-local Intl.RelativeTimeFormat (use utils/time.ts)',
		pattern: /new Intl\.RelativeTimeFormat\b/,
		svelteOnly: true
	}
];

/** Strip comments so a doc sentence quoting the banned call is not a hit. */
function code(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
}

function offences(): string[] {
	const found: string[] = [];
	for (const [path, source] of Object.entries(RAW)) {
		if (FORMATTERS.has(path) || /\.test\.ts$/.test(path)) continue;
		const isSvelte = path.endsWith('.svelte');
		const lines = code(source).split('\n');
		lines.forEach((line, i) => {
			for (const { name, pattern, svelteOnly } of RULES) {
				if (svelteOnly && !isSvelte) continue;
				if (pattern.test(line)) found.push(`${path}:${i + 1} ${name}`);
			}
		});
	}
	return found;
}

describe('counts and dates use the locale-aware helpers', () => {
	it('no bare or locale-pinned toLocaleString, no hand-rolled count formatter', () => {
		const found = offences().filter((o) => !(o.split(':')[0] in PENDING_OTHER_LANE));
		expect(found).toEqual([]);
	});

	it('every allowlisted file still has a site (prune converted entries)', () => {
		const files = new Set(offences().map((o) => o.split(':')[0]));
		const stale = Object.keys(PENDING_OTHER_LANE).filter((p) => !files.has(p));
		expect(stale).toEqual([]);
	});
});
