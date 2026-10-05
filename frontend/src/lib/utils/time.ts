import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

/**
 * `Intl.RelativeTimeFormat` is not free to construct and `timeAgo` runs once
 * per row of a notifications table, so memoize per (locale, numeric) pair.
 * The active locale is part of the key, so a locale switch never serves a
 * stale formatter.
 */
const relativeFormatters = new Map<string, Intl.RelativeTimeFormat>();

function relativeFormatter(numeric: 'auto' | 'always'): Intl.RelativeTimeFormat {
	const locale = getActiveFormatLocale();
	const key = `${locale ?? ''}|${numeric}`;
	let fmt = relativeFormatters.get(key);
	if (!fmt) {
		// `short` over `narrow`: narrow reproduces the old English exactly
		// ("5m ago") but degrades badly elsewhere — French renders a bare
		// "-5 min" and German "vor 5 m". `short` stays compact in every shipped
		// locale ("5 min. ago" / "vor 5 Min." / "il y a 5 min" / "5 分前").
		fmt = new Intl.RelativeTimeFormat(locale, { numeric, style: 'short' });
		relativeFormatters.set(key, fmt);
	}
	return fmt;
}

/**
 * Friendly relative-time formatting (e.g. "now", "5 min. ago", "2 days ago").
 * Shared by the notifications page + the sidebar notification popover so the
 * two can't drift. Coarse buckets only — exact timestamps go in `title`.
 *
 * Localized via `Intl.RelativeTimeFormat` keyed on the active in-app locale,
 * the same holder {@link formatDate} reads — so the picker moves relative
 * times alongside dates and money instead of leaving hardcoded English labels
 * inside an otherwise-German page. `Intl` was chosen over ICU plural message
 * keys because this module must stay importable under vitest's node
 * environment: `m()` lives in `i18n/store.svelte.ts`, a rune module that
 * imports `$app/env`, and pulling it in here would break both the
 * pure-module contract and the unit tests. `Intl` also gets every locale's
 * plural rules for free and needs no catalogue keys at all.
 *
 * The buckets are unchanged: under a minute, minutes, hours, then days. Only
 * the sub-minute bucket uses `numeric: 'auto'` (so it reads "now" / "jetzt"
 * rather than "in 0 seconds"); every other bucket stays explicitly numeric so
 * a day-old row can't render as "yesterday".
 *
 * A future timestamp still falls into the sub-minute bucket ("now"), matching
 * the previous behaviour. An unparseable value returns `placeholder` — the
 * old code fed `NaN` into a template literal and rendered "NaNd ago", and
 * `Intl.RelativeTimeFormat#format` throws a `RangeError` on a non-finite
 * value, which would take the whole table render down.
 */
export function timeAgo(iso: string, placeholder = '—'): string {
	const then = new Date(iso).getTime();
	if (Number.isNaN(then)) return placeholder;
	const mins = Math.floor((Date.now() - then) / 60000);
	if (mins < 1) return relativeFormatter('auto').format(0, 'second');
	if (mins < 60) return relativeFormatter('always').format(-mins, 'minute');
	const hours = Math.floor(mins / 60);
	if (hours < 24) return relativeFormatter('always').format(-hours, 'hour');
	return relativeFormatter('always').format(-Math.floor(hours / 24), 'day');
}

/**
 * Parse a bare calendar date (`YYYY-MM-DD` or `YYYY-MM`) into a `Date` at
 * *local* midnight. `new Date('2026-06-20')` parses the string as UTC, which
 * in a negative-offset timezone (e.g. America/New_York) rolls the displayed
 * calendar day back to the 19th — so a date-only key must be split and fed to
 * the local-time `Date(y, mIndex, d)` constructor instead. Returns `null` when
 * the components aren't a real date. A value carrying a time component (an ISO
 * timestamp) is left to the normal `Date` parser, since its instant is exact.
 */
function parseLocalDate(value: string): Date | null {
	const dateOnly = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(value);
	const d = dateOnly
		? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, dateOnly[3] ? Number(dateOnly[3]) : 1)
		: new Date(value);
	return Number.isNaN(d.getTime()) ? null : d;
}

/** Default short-date parts (e.g. "Jun 20, 2026" / "20 juin 2026"). */
const DEFAULT_DATE_OPTS: Intl.DateTimeFormatOptions = {
	month: 'short',
	day: 'numeric',
	year: 'numeric'
};

/**
 * Locale-aware short calendar date (e.g. "Jun 20, 2026" / "20 juin 2026").
 *
 * The locale follows the active in-app i18n locale (the picker), read from
 * `formatLocale.ts::getActiveFormatLocale()` — the same holder `money.ts`
 * already drives `Intl.NumberFormat` off, so dates and money switch together.
 * Until a locale is actively selected the holder is `undefined`, so the
 * browser/runtime locale is used (the pre-i18n behaviour — nothing regresses).
 *
 * Pass `opts` to vary the parts a caller needs — a list row that wants no year
 * (`{month:'short', day:'numeric'}`) or a date+time (`…, hour:'numeric',
 * minute:'2-digit'}`) — while still localizing off the active locale. Omit it
 * for the standard short date. When `opts.hour`/`minute` is present a date-only
 * key (`YYYY-MM-DD`) still renders at local midnight; an ISO timestamp keeps its
 * exact instant.
 *
 * Returns the `placeholder` (default `—`) for a null/empty/unparseable value.
 * Read it inside a `$derived` / template so a locale switch re-renders.
 */
export function formatDate(
	value: string | null | undefined,
	placeholder = '—',
	opts: Intl.DateTimeFormatOptions = DEFAULT_DATE_OPTS
): string {
	if (!value) return placeholder;
	const d = parseLocalDate(value);
	if (!d) return placeholder;
	// `toLocaleDateString` ignores time parts; switch to `toLocaleString` when a
	// caller asks for hour/minute/second (e.g. a "Jun 20, 3:45 PM" datetime cell).
	const locale = getActiveFormatLocale();
	return opts.hour || opts.minute || opts.second
		? d.toLocaleString(locale, opts)
		: d.toLocaleDateString(locale, opts);
}

/**
 * Locale-aware short label for a reporting-period key — `YYYY-MM` (month) or
 * `YYYY-MM-DD` (day/week). Month keys render month + 2-digit year; day/week
 * keys add the day-of-month. Drives its locale off the active i18n locale,
 * the same way {@link formatDate} does. Returns the raw key unchanged if it
 * isn't a parseable date.
 */
export function formatPeriod(p: string): string {
	// month keys are YYYY-MM; day/week keys are YYYY-MM-DD.
	const isMonthKey = p.length === 7;
	const d = parseLocalDate(p);
	if (!d) return p;
	return d.toLocaleDateString(getActiveFormatLocale(), {
		month: 'short',
		day: isMonthKey ? undefined : 'numeric',
		year: '2-digit'
	});
}
