import { ensureSlices } from '#lib/i18n/store.svelte.ts';

// "Find your workspace" renders the marketing catalogue slice (decisions §300).
// SvelteKit awaits this before rendering, so the page is in the reader's
// language on its first frame; `slices.test.ts` fails if a slice key is
// rendered from a route that does not load its slice.
export const load = () => ensureSlices('marketing');
