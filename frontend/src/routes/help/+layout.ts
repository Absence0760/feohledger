import { ensureSlices } from '#lib/i18n/store.svelte.ts';

// Every /help route renders the help-centre catalogue slice (decisions §261),
// and the guides name the settings screens' controls with `{ui:org.…}` /
// `{ui:admin.…}`, so they render the settings slice too (§300). SvelteKit
// awaits this before rendering, so both are in the reader's language on the
// first frame; `slices.test.ts` fails if a slice key is rendered from a route
// that does not load its slice.
export const load = () => ensureSlices('help', 'settings');
