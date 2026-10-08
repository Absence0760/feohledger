import { browser } from '$app/env';
import { en } from './locales/en';
import { enHelp } from './locales/help/en';
import { CATALOGUE_LOADERS, HELP_CATALOGUE_LOADERS } from './catalogues';
import { interpolate } from './interpolate';
import { setActiveFormatLocale } from './formatLocale';
import type { AnyMessageKey, HelpMessages, Messages } from './messages';
import { DEFAULT_LOCALE, dirForLocale, isSupportedLocale, negotiateLocale, type Locale } from './locale';

// localStorage key for the persisted picker choice. Kept distinct from the
// (account-level) email-language pref that a later server-side track adds —
// this one is "what language to show in-app on THIS device".
const STORAGE_KEY = 'feoh_locale';

// Reactive locale runes. `dict` is what `m()` reads, so swapping it on
// setLocale re-renders every call site (template / $derived).
let locale = $state<Locale>(DEFAULT_LOCALE);
let dict = $state<Messages>(en);
// The help-centre slice for `locale` once loaded, English until then — the
// same English-first degradation the main dict gives a slow chunk. Loaded only
// after something renders help copy (`ensureHelpCatalogue`), so a page with no
// /help route and no ⓘ HelpTip never downloads it (decisions §261).
let helpDict = $state<HelpMessages>(enHelp);
let helpLocale: Locale = 'en';
let helpWanted = false;
let helpLoading: Promise<void> | null = null;

export function currentLocale(): Locale {
	return locale;
}

/**
 * Reactive message lookup. Reading `dict` here makes every call site
 * (template / `$derived`) re-render when the active locale changes. Falls
 * back to the English string, then the raw key, so a not-yet-translated key
 * degrades gracefully rather than rendering blank.
 */
export function m(key: AnyMessageKey, params?: Record<string, string | number>): string {
	const main = dict as Record<string, string>;
	const help = helpDict as Record<string, string>;
	const value: string =
		main[key] ??
		help[key] ??
		(en as Record<string, string>)[key] ??
		(enHelp as Record<string, string>)[key] ??
		key;
	return interpolate(value, params, locale);
}

/**
 * Load the help-centre slice for the active locale, once. Called by the /help
 * layout and by every ⓘ HelpTip on mount; idempotent and cheap after the first
 * call. Remembers that help was wanted, so a later locale switch loads the new
 * locale's slice too. Until it lands `m()` answers help keys in English; a
 * failed load keeps English rather than blanking anything.
 */
export function ensureHelpCatalogue(): Promise<void> {
	helpWanted = true;
	if (helpLocale === locale) return Promise.resolve();
	if (helpLoading) return helpLoading;
	const target = locale;
	helpLoading = HELP_CATALOGUE_LOADERS[target]()
		.then((loaded) => {
			// A locale switch while this was in flight makes it stale.
			if (locale !== target) return;
			helpDict = loaded;
			helpLocale = target;
		})
		.catch(() => {
			/* keep English help copy */
		})
		.finally(() => {
			helpLoading = null;
			// Settled for a locale that is no longer active: fetch the current one.
			if (helpWanted && helpLocale !== locale) void ensureHelpCatalogue();
		});
	return helpLoading;
}

/** The help slice follows the main dict: back to English the moment the
 *  locale changes (never one language's help under another's chrome), then
 *  the new locale's slice if help copy is on screen. */
function followLocaleWithHelp(): void {
	helpDict = enHelp;
	helpLocale = 'en';
	if (helpWanted && locale !== 'en') void ensureHelpCatalogue();
}

/**
 * The FORMAT locale for a catalogue locale: the full browser locale when it is
 * a regional variant of that catalogue locale (e.g. catalogue 'en' + browser
 * 'en-GB' → format with 'en-GB' so dates/numbers read in the regional style);
 * otherwise the catalogue locale itself (an explicit picker choice like 'de'
 * wins over an unrelated browser tag).
 *
 * Split out of `applyDocumentLocale` so `initLocale` can apply it
 * *synchronously* — see the note there.
 */
function resolveFormatLocale(next: Locale): string {
	if (browser && typeof navigator !== 'undefined' && navigator.language) {
		const nav = navigator.language;
		if (nav.toLowerCase().split('-')[0] === next) return nav;
	}
	return next;
}

function applyDocumentLocale(next: Locale): void {
	// Keep the `Intl`-based formatters (money.ts, the time.ts date + relative
	// helpers) in sync with the active locale.
	setActiveFormatLocale(resolveFormatLocale(next));
	if (!browser) return;
	try {
		localStorage.setItem(STORAGE_KEY, next);
	} catch {
		/* storage may be unavailable (private mode / quota) — non-fatal */
	}
	document.documentElement.lang = next;
	document.documentElement.dir = dirForLocale(next);
}

/**
 * Switch the active locale. English swaps synchronously; other locales load
 * their chunk first and keep the current dict on failure (layered resilience —
 * a failed locale fetch must not blank the UI). Persists to localStorage and
 * sets `<html lang/dir>`.
 */
export async function setLocale(next: Locale): Promise<void> {
	if (next === 'en') {
		dict = en;
		locale = 'en';
		followLocaleWithHelp();
		applyDocumentLocale('en');
		return;
	}
	try {
		dict = await CATALOGUE_LOADERS[next]();
		locale = next;
		followLocaleWithHelp();
		applyDocumentLocale(next);
	} catch {
		/* keep the current locale + dict */
	}
}

/**
 * Detect the visitor's locale on first client mount: a stored choice wins,
 * else `navigator.languages`, else English. Called once from `+layout.svelte`.
 */
export function initLocale(): void {
	if (!browser) return;
	let stored: string | null = null;
	try {
		stored = localStorage.getItem(STORAGE_KEY);
	} catch {
		/* ignore */
	}
	const navLangs =
		typeof navigator !== 'undefined'
			? (navigator.languages?.length ? [...navigator.languages] : (navigator.language ?? null))
			: null;
	const next = negotiateLocale(stored, navLangs);
	// Apply the FORMAT locale synchronously, BEFORE `setLocale` awaits the lazy
	// catalogue chunk. `setLocale` only reaches `applyDocumentLocale` after that
	// dynamic `import()` resolves, so a visitor with a stored `de` used to get a
	// paint where the labels were still English and the figures were in the
	// browser locale, then a second flip once the chunk landed. The formatters
	// need no catalogue — the locale tag is all they consume — so there is
	// nothing to wait for. (`setLocale`'s own failure path keeps English
	// messages; formatting the user's OWN explicit locale choice is still the
	// right read for them, so it is deliberately not rolled back.)
	setActiveFormatLocale(resolveFormatLocale(next));
	void setLocale(next);
}

export { isSupportedLocale };
export type { Locale };
