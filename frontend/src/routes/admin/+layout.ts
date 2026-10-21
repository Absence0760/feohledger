import { ensureSlices } from '#lib/i18n/store.svelte.ts';

// The admin screens render the settings catalogue slice (decisions §300).
// SvelteKit awaits this before rendering, so the slice is in the reader's
// language on the first frame; `slices.test.ts` fails if a slice key is
// rendered from a route that does not load its slice.
export const load = () => ensureSlices('settings');
