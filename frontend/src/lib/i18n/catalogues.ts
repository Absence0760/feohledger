import { en } from './locales/en';
import { enHelp } from './locales/help/en';
import type { HelpMessages, Messages } from './messages';
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

// The help-centre slice, one more chunk per locale, loaded only once a /help
// route or an ⓘ HelpTip asks for it (`ensureHelpCatalogue`). Kept apart
// because it was ~9 KB gzipped of every locale catalogue that most pages
// never read, and the largest catalogue had reached the 100 KB per-chunk
// budget (decisions §261).
export const HELP_CATALOGUE_LOADERS: Record<Locale, () => Promise<HelpMessages>> = {
	en: () => Promise.resolve(enHelp),
	de: () => import('./locales/help/de').then((m) => m.messages),
	fr: () => import('./locales/help/fr').then((m) => m.messages),
	es: () => import('./locales/help/es').then((m) => m.messages),
	'pt-BR': () => import('./locales/help/pt-BR').then((m) => m.messages),
	ja: () => import('./locales/help/ja').then((m) => m.messages),
};
