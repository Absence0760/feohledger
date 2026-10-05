import { afterEach, describe, expect, it } from 'vitest';
import { formatDate, formatPeriod, timeAgo } from './time';
import { setActiveFormatLocale } from '#lib/i18n/formatLocale.ts';

// The date helpers drive their locale off the active in-app i18n locale
// (the same holder money.ts reads). These tests assert the locale actually
// flows through and that null/invalid inputs fall back gracefully.

afterEach(() => {
	// Reset the shared holder so locale state never leaks across tests.
	setActiveFormatLocale(undefined);
});

describe('formatDate', () => {
	it('returns the placeholder for null/empty/invalid', () => {
		expect(formatDate(null)).toBe('—');
		expect(formatDate('')).toBe('—');
		expect(formatDate(undefined)).toBe('—');
		expect(formatDate('not-a-date')).toBe('—');
		expect(formatDate(null, 'n/a')).toBe('n/a');
	});

	it('formats a date under the explicit en-US locale', () => {
		setActiveFormatLocale('en-US');
		// Use a midday UTC timestamp so the calendar day is locale/timezone-stable.
		expect(formatDate('2026-06-20T12:00:00Z')).toBe('Jun 20, 2026');
	});

	it('follows the active locale (German month abbreviation differs)', () => {
		setActiveFormatLocale('en-US');
		const en = formatDate('2026-03-20T12:00:00Z');
		setActiveFormatLocale('de-DE');
		const de = formatDate('2026-03-20T12:00:00Z');
		// March is "Mar" in en-US and "März" in de-DE — the locale must change
		// the output, proving the helper is driven off the active locale.
		expect(en).not.toBe(de);
		expect(en).toContain('Mar');
		expect(de).toContain('März');
	});

	it('honours an opts override that drops the year (dashboard due-date cell)', () => {
		setActiveFormatLocale('en-US');
		const out = formatDate('2026-06-20T12:00:00Z', '—', { month: 'short', day: 'numeric' });
		expect(out).toBe('Jun 20');
		expect(out).not.toContain('2026');
	});

	it('renders a time component when opts asks for hour/minute (switches to toLocaleString)', () => {
		setActiveFormatLocale('en-US');
		// 15:45 UTC is past noon, so the calendar day is stable across timezones
		// behind UTC; assert the date parts + that a time is present.
		const out = formatDate('2026-06-20T15:45:00Z', '—', {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		});
		expect(out).toContain('Jun');
		// A ":" only appears once the time component is rendered — proving the
		// date-only `toLocaleDateString` path was NOT taken.
		expect(out).toContain(':');
	});

	it('opts override still localizes off the active locale', () => {
		const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
		setActiveFormatLocale('en-US');
		const en = formatDate('2026-03-20T12:00:00Z', '—', opts);
		setActiveFormatLocale('de-DE');
		const de = formatDate('2026-03-20T12:00:00Z', '—', opts);
		expect(en).not.toBe(de);
		expect(en).toContain('Mar');
		expect(de).toContain('März');
	});

	it('still returns the placeholder with opts for null/invalid', () => {
		const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
		expect(formatDate(null, '—', opts)).toBe('—');
		expect(formatDate('not-a-date', 'n/a', opts)).toBe('n/a');
	});
});

describe('formatPeriod', () => {
	it('returns the raw key for an unparseable value', () => {
		expect(formatPeriod('garbage')).toBe('garbage');
	});

	it('renders a month key (YYYY-MM) with no day-of-month', () => {
		setActiveFormatLocale('en-US');
		// Month key → "<Mon> <YY>" (year 2-digit, no day).
		expect(formatPeriod('2026-06')).toBe('Jun 26');
	});

	it('renders a day key (YYYY-MM-DD) with the day-of-month', () => {
		setActiveFormatLocale('en-US');
		expect(formatPeriod('2026-06-20')).toBe('Jun 20, 26');
	});

	it('follows the active locale', () => {
		setActiveFormatLocale('en-US');
		const en = formatPeriod('2026-03');
		setActiveFormatLocale('de-DE');
		const de = formatPeriod('2026-03');
		expect(en).not.toBe(de);
		expect(en).toContain('Mar');
		expect(de).toContain('März');
	});
});

// `timeAgo` was the one helper in this file the locale picker did not move —
// it returned hardcoded English ("Just now", "5m ago", "2d ago") while its
// `formatDate` / `formatPeriod` siblings localized, so a German user read
// English relative times inside a German page. It is now
// `Intl.RelativeTimeFormat` keyed on the SAME active-locale holder. These
// tests pin the coarse buckets (they are the behaviour, not an artefact) and
// lock the localized output for more than one locale.

/** An ISO timestamp `ms` in the past, offset a little to clear a bucket edge. */
function agoIso(ms: number): string {
	return new Date(Date.now() - ms).toISOString();
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('timeAgo', () => {
	it('renders the sub-minute bucket as the locale-aware "now"', () => {
		setActiveFormatLocale('en-US');
		expect(timeAgo(agoIso(30_000))).toBe('now');
		setActiveFormatLocale('de-DE');
		expect(timeAgo(agoIso(30_000))).toBe('jetzt');
	});

	it('treats a future timestamp as the sub-minute bucket (unchanged behaviour)', () => {
		setActiveFormatLocale('en-US');
		expect(timeAgo(new Date(Date.now() + 5 * MINUTE).toISOString())).toBe('now');
	});

	it('renders the minute bucket under en-US', () => {
		setActiveFormatLocale('en-US');
		expect(timeAgo(agoIso(5 * MINUTE + 1_000))).toBe('5 min. ago');
		expect(timeAgo(agoIso(MINUTE + 1_000))).toBe('1 min. ago');
	});

	it('renders the hour bucket under en-US', () => {
		setActiveFormatLocale('en-US');
		expect(timeAgo(agoIso(2 * HOUR + MINUTE))).toBe('2 hr. ago');
	});

	it('renders the day bucket under en-US, and never as "yesterday"', () => {
		setActiveFormatLocale('en-US');
		// `numeric: 'always'` — a one-day-old row must stay numeric so the
		// bucket reads as an age, not a calendar word.
		expect(timeAgo(agoIso(DAY + HOUR))).toBe('1 day ago');
		expect(timeAgo(agoIso(3 * DAY + HOUR))).toBe('3 days ago');
	});

	it('localizes every bucket under German', () => {
		setActiveFormatLocale('de-DE');
		expect(timeAgo(agoIso(5 * MINUTE + 1_000))).toBe('vor 5 Min.');
		expect(timeAgo(agoIso(2 * HOUR + MINUTE))).toBe('vor 2 Std.');
		expect(timeAgo(agoIso(3 * DAY + HOUR))).toBe('vor 3 Tagen');
	});

	it('localizes every bucket under Japanese (no grammatical plural)', () => {
		// Japanese is the locale an ICU-plural implementation would have had to
		// special-case (`Intl.PluralRules('ja')` never selects `one`);
		// `Intl.RelativeTimeFormat` gets it right with no per-locale authoring.
		setActiveFormatLocale('ja');
		expect(timeAgo(agoIso(5 * MINUTE + 1_000))).toBe('5 分前');
		expect(timeAgo(agoIso(3 * DAY + HOUR))).toBe('3 日前');
	});

	it('changes output when the active locale changes, with no remount', () => {
		// The whole point: the SAME module-level function, called twice, must
		// follow the picker. (This is the value half of the reactivity fix; the
		// re-render half needs a browser — see formatLocale.test.ts.)
		const iso = agoIso(2 * HOUR + MINUTE);
		setActiveFormatLocale('en-US');
		const en = timeAgo(iso);
		setActiveFormatLocale('fr');
		const fr = timeAgo(iso);
		expect(en).toBe('2 hr. ago');
		// ICU separates the French number from its unit with a narrow no-break
		// space (U+202F), so match the prefix rather than pinning an invisible
		// character an editor will silently normalise.
		expect(fr).toMatch(/^il y a 2\s+h$/u);
		expect(fr).not.toBe(en);
	});

	it('returns the placeholder for an unparseable timestamp', () => {
		// The old implementation rendered "NaNd ago" here, and
		// `Intl.RelativeTimeFormat#format` throws a RangeError on a non-finite
		// value — which would have taken a whole table render down.
		setActiveFormatLocale('en-US');
		expect(timeAgo('not-a-date')).toBe('—');
		expect(timeAgo('')).toBe('—');
		expect(timeAgo('not-a-date', 'n/a')).toBe('n/a');
	});
});
