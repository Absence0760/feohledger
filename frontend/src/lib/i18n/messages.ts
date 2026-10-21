// Type-only imports: the settings and portal slices are lazy in every locale,
// English included, so nothing here may pull them into the entry chunk.
import type { en } from './locales/en';
import type { enHelp } from './locales/help/en';
import type { enPortal } from './locales/portal/en';
import type { enSettings } from './locales/settings/en';

// The catalogue shape: every locale module is `... satisfies Messages`,
// so a missing or extra key is a compile error. Values are plain strings
// (en's value type widens to `string`, not literals) — translations are
// free to differ.
export type Messages = typeof en;

// The lazily loaded catalogue slices: keys only a bounded set of routes (or,
// for help, the ⓘ HelpTip) renders, shipped as one more chunk per locale and
// loaded by `ensureSlices()` (decisions §261, §300). Every slice locale module
// is `... satisfies <Name>Messages`.
export type HelpMessages = typeof enHelp;
export type SettingsMessages = typeof enSettings;
export type PortalMessages = typeof enPortal;

/** Each slice's shape, by name. Adding a slice starts here. */
export type SliceMessages = {
	help: HelpMessages;
	settings: SettingsMessages;
	portal: PortalMessages;
};

export type SliceName = keyof SliceMessages;

/** A key of the main catalogue, which every page has. */
export type MainMessageKey = keyof Messages;

/** A key of one of the lazy slices. */
export type SliceMessageKey = { [S in SliceName]: keyof SliceMessages[S] }[SliceName];

/** Any key `m()` can resolve: the main catalogue or a slice. */
export type MessageKey = MainMessageKey | SliceMessageKey;
