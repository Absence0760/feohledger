import { en } from './locales/en';
import { enHelp } from './locales/help/en';
import { enPortal } from './locales/portal/en';
import { enSettings } from './locales/settings/en';
import { CATALOGUE_LOADERS, SLICE_LOADERS, SLICE_NAMES } from './catalogues';
import type { Locale } from './locale';
import type { MessageKey, SliceMessages, SliceName } from './messages';

// Every catalogue at once — the main one and every lazy slice (catalogues.ts),
// for code that needs the text of a key whichever chunk it ships in: tests,
// mostly. Never for rendering, which goes through `m()`. The app never imports
// this module; that is what keeps the English settings and portal slices out
// of the entry chunk, so it must stay that way.

/** The English slices, statically. */
export const EN_SLICES: { [S in SliceName]: SliceMessages[S] } = {
	help: enHelp,
	settings: enSettings,
	portal: enPortal,
};

/** Every English string, by any key `m()` accepts. */
export const EN_ALL = Object.assign({}, en, ...Object.values(EN_SLICES)) as Record<MessageKey, string>;

/** Every string of one locale, main catalogue and all slices loaded together. */
export async function loadAllMessages(locale: Locale): Promise<Record<MessageKey, string>> {
	const parts = await Promise.all([CATALOGUE_LOADERS[locale](), ...SLICE_NAMES.map((s) => SLICE_LOADERS[s][locale]())]);
	return Object.assign({}, ...parts) as Record<MessageKey, string>;
}
