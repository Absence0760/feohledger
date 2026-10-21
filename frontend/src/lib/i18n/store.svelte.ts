import { browser } from '$app/env';
import { en } from './locales/en';
import { CATALOGUE_LOADERS, EN_FALLBACK, SLICE_LOADERS, SLICE_NAMES, sliceOf } from './catalogues';
import { interpolate } from './interpolate';
import { setActiveFormatLocale } from './formatLocale';
import type { MessageKey, Messages, SliceName } from './messages';
import { DEFAULT_LOCALE, dirForLocale, isSupportedLocale, negotiateLocale, type Locale } from './locale';

// localStorage key for the persisted picker choice. Kept distinct from the
// (account-level) email-language pref that a later server-side track adds —
// this one is "what language to show in-app on THIS device".
const STORAGE_KEY = 'feoh_locale';

// Reactive locale runes. `dict` is what `m()` reads, so swapping it on
// setLocale re-renders every call site (template / $derived).
let locale = $state<Locale>(DEFAULT_LOCALE);
let dict = $state<Messages>(en);

// The lazy catalogue slices (catalogues.ts, decisions §261 + §300). A slice is
// absent until `ensureSlices()` loads it, except help, which starts on its
// static English copy. `$state.raw`: the record is replaced whole on every
// change rather than deep-proxied, since a slice is a thousand-key object that
// is only ever swapped, never mutated.
type SliceDict = Record<string, string>;
let sliceDicts = $state.raw<Partial<Record<SliceName, SliceDict>>>({ ...EN_FALLBACK });
// The locale each loaded slice is in (absent: not loaded), which slices
// something has asked for (so a locale switch reloads them), and the load in
// flight per slice.
const sliceLocale: Partial<Record<SliceName, Locale>> = Object.fromEntries(
	Object.keys(EN_FALLBACK).map((s) => [s, 'en'])
);
const wanted = new Set<SliceName>();
const sliceLoading = new Map<SliceName, Promise<void>>();
// Dev-only: keys already reported as rendered before their slice was asked for.
const reported = new Set<string>();

export function currentLocale(): Locale {
	return locale;
}

/**
 * Reactive message lookup. Reading `dict` here makes every call site (template
 * / `$derived`) re-render when the active locale changes; a slice key also
 * reads `sliceDicts`, so it re-renders when its slice lands. Falls back to the
 * English string, then the raw key, so a not-yet-translated key degrades
 * gracefully rather than rendering blank.
 */
export function m(key: MessageKey, params?: Record<string, string | number>): string {
	let value: string | undefined = (dict as Record<string, string>)[key] ?? (en as Record<string, string>)[key];
	if (value === undefined) {
		const slice = sliceOf(key);
		if (slice) {
			if (import.meta.env.DEV && !wanted.has(slice) && !reported.has(key)) {
				// A slice key rendered where nothing loaded its slice: it shows as
				// its raw name. `slices.test.ts` catches this statically; this
				// names the call site in the browser console.
				reported.add(key);
				console.error(
					`i18n: '${key}' is in the '${slice}' catalogue slice, which nothing on this page loaded. ` +
						`Call ensureSlices('${slice}') from the route's +layout.ts (decisions §300).`
				);
			}
			value = sliceDicts[slice]?.[key] ?? (EN_FALLBACK[slice] as SliceDict | undefined)?.[key];
		}
	}
	return interpolate(value ?? key, params, locale);
}

/** A slice in `target`, else in English; null only if both fail to load. */
async function fetchSlice(name: SliceName, target: Locale): Promise<{ dict: SliceDict; loc: Locale } | null> {
	try {
		return { dict: await SLICE_LOADERS[name][target](), loc: target };
	} catch {
		if (target === 'en') return null;
		try {
			return { dict: await SLICE_LOADERS[name].en(), loc: 'en' };
		} catch {
			return null;
		}
	}
}

function setSlice(name: SliceName, loaded: SliceDict, loc: Locale): void {
	sliceDicts = { ...sliceDicts, [name]: loaded };
	sliceLocale[name] = loc;
}

function loadSlice(name: SliceName): Promise<void> {
	if (sliceLocale[name] === locale) return Promise.resolve();
	const inflight = sliceLoading.get(name);
	if (inflight) return inflight;
	const target = locale;
	const p = fetchSlice(name, target)
		.then((got) => {
			// A locale switch while this was in flight makes it stale. A slice
			// that could not load at all keeps whatever it had.
			if (got && locale === target) setSlice(name, got.dict, got.loc);
		})
		.finally(() => {
			sliceLoading.delete(name);
			// Settled for a locale that is no longer active: fetch the current
			// one. Only then — retrying a chunk that failed for THIS locale would
			// loop for as long as the network stays down.
			if (locale !== target) void loadSlice(name);
		});
	sliceLoading.set(name, p);
	return p;
}

/**
 * Load catalogue slices for the active locale, once each. A route calls it from
 * its `+layout.ts` `load` (`export const load = () => ensureSlices('settings')`),
 * which SvelteKit awaits before rendering the route, so a slice key is never
 * read before its slice is there; a component that owns a slice (the ⓘ
 * HelpTip) calls it as it initialises. Idempotent and cheap after the first
 * call. Remembers the slices were wanted, so a later locale switch loads them
 * in the new locale together with the main catalogue. A slice that fails to
 * load in the active locale falls back to English.
 */
export function ensureSlices(...names: SliceName[]): Promise<void> {
	for (const name of names) wanted.add(name);
	return Promise.all(names.map(loadSlice)).then(() => undefined);
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
 * Switch the active locale. Loads the locale's main catalogue first and keeps
 * the current one on failure (layered resilience — a failed locale fetch must
 * not blank the UI). The slices something has asked for load in parallel and
 * swap in with the main catalogue, so the page never shows one language's
 * chrome around another's settings form; a slice that cannot load in `next`
 * falls back to English. Persists to localStorage and sets `<html lang/dir>`.
 */
export async function setLocale(next: Locale): Promise<void> {
	const slices = [...wanted];
	let main: Messages;
	let got: Awaited<ReturnType<typeof fetchSlice>>[];
	try {
		[main, ...got] = await Promise.all([CATALOGUE_LOADERS[next](), ...slices.map((s) => fetchSlice(s, next))]);
	} catch {
		return; /* keep the current locale + dict */
	}
	dict = main;
	locale = next;
	slices.forEach((s, i) => {
		const loaded = got[i];
		if (loaded) setSlice(s, loaded.dict, loaded.loc);
	});
	for (const s of SLICE_NAMES) {
		if (slices.includes(s)) continue;
		// Asked for while the catalogue was loading: fetch it in the new locale.
		if (wanted.has(s)) void loadSlice(s);
		// Never asked for: only help's static English is loaded, and stays.
	}
	applyDocumentLocale(next);
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
