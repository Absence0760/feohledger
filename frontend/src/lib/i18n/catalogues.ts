import { en } from './locales/en';
import { enHelp } from './locales/help/en';
import type { Messages, SliceMessages, SliceName } from './messages';
import type { Locale } from './locale';

// One loader per supported locale, typed `Record<Locale, …>` so adding a
// locale to SUPPORTED_LOCALES without a catalogue here is a compile error.
// English resolves synchronously (it is the static fallback dict + the
// prerender default); every other locale is a dynamic import() so it
// splits into its own chunk and only downloads when actually selected —
// the i18n layer adds ~nothing to the initial payload.
//
// Used by the runtime (store.svelte.ts) to switch locale and by
// messages_parity.test.ts to validate every shipped catalogue without
// hard-coding the locale list.
export const CATALOGUE_LOADERS: Record<Locale, () => Promise<Messages>> = {
	en: () => Promise.resolve(en),
	de: () => import('./locales/de').then((m) => m.messages),
	fr: () => import('./locales/fr').then((m) => m.messages),
	es: () => import('./locales/es').then((m) => m.messages),
	'pt-BR': () => import('./locales/pt-BR').then((m) => m.messages),
	ja: () => import('./locales/ja').then((m) => m.messages),
};

// The lazy catalogue slices: keys only a bounded part of the app renders, one
// more chunk per locale each, loaded by `ensureSlices()`. They exist because
// the largest catalogue kept reaching the 100 KB per-chunk budget in
// web-bundle-budget.yml.
//
//   help     — glossary, guide chrome, diagram labels. Loaded by /help and by
//              every ⓘ HelpTip (decisions §261).
//   settings — org / SSO / admin / billing / sweep-health screens. Loaded by
//              /organization, /admin, /billing and /help (§300).
//   portal   — the supplier portal. Loaded by /portal (§300).
//
// A route-scoped slice (settings, portal) is lazy in EVERY locale, English
// included: the route's `+layout.ts` awaits it before rendering, so nothing
// ever reads it unloaded, and keeping English out of the entry chunk is half
// of what the split buys. The help slice keeps a static English copy
// (EN_FALLBACK) because a HelpTip renders on any page with no route load to
// wait on, so it needs something to show while its chunk is in flight.
//
// `slices.test.ts` proves every key of a slice is rendered only where that
// slice is loaded; messages_parity.test.ts holds each slice to the same parity
// rules as the main catalogue.
export const SLICE_LOADERS: { [S in SliceName]: Record<Locale, () => Promise<SliceMessages[S]>> } = {
	help: {
		en: () => Promise.resolve(enHelp),
		de: () => import('./locales/help/de').then((m) => m.messages),
		fr: () => import('./locales/help/fr').then((m) => m.messages),
		es: () => import('./locales/help/es').then((m) => m.messages),
		'pt-BR': () => import('./locales/help/pt-BR').then((m) => m.messages),
		ja: () => import('./locales/help/ja').then((m) => m.messages),
	},
	settings: {
		en: () => import('./locales/settings/en').then((m) => m.enSettings),
		de: () => import('./locales/settings/de').then((m) => m.messages),
		fr: () => import('./locales/settings/fr').then((m) => m.messages),
		es: () => import('./locales/settings/es').then((m) => m.messages),
		'pt-BR': () => import('./locales/settings/pt-BR').then((m) => m.messages),
		ja: () => import('./locales/settings/ja').then((m) => m.messages),
	},
	portal: {
		en: () => import('./locales/portal/en').then((m) => m.enPortal),
		de: () => import('./locales/portal/de').then((m) => m.messages),
		fr: () => import('./locales/portal/fr').then((m) => m.messages),
		es: () => import('./locales/portal/es').then((m) => m.messages),
		'pt-BR': () => import('./locales/portal/pt-BR').then((m) => m.messages),
		ja: () => import('./locales/portal/ja').then((m) => m.messages),
	},
};

/**
 * Each slice's key prefixes. A key the main catalogue lacks belongs to the
 * slice whose prefix it starts with; a key under one of these prefixes that
 * shared code renders stays in the main catalogue, which `m()` reads first.
 * Lets the runtime place a key without bundling the slice's key list.
 * `slices.test.ts` proves every slice key carries one of its slice's prefixes
 * and that no prefix is claimed twice.
 */
export const SLICE_PREFIXES: Record<SliceName, readonly string[]> = {
	help: ['help.'],
	settings: ['org.', 'orgSso.', 'admin.', 'billing.', 'sweepHealth.'],
	portal: ['portal.'],
};

/** The static English copy a slice shows until its chunk lands — help only (see above). */
export const EN_FALLBACK: { [S in SliceName]?: SliceMessages[S] } = { help: enHelp };

export const SLICE_NAMES = Object.keys(SLICE_LOADERS) as SliceName[];

/** The slice a key not in the main catalogue lives in, by prefix. */
export function sliceOf(key: string): SliceName | undefined {
	for (const name of SLICE_NAMES) if (SLICE_PREFIXES[name].some((p) => key.startsWith(p))) return name;
	return undefined;
}
