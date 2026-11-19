import { ensureSlices } from '#lib/i18n/store.svelte.ts';

// /audit labels each AuditLog action from the audit catalogue slice
// (decisions §300). SvelteKit awaits this before rendering, so the labels are
// there on the first frame rather than the raw tokens flashing first;
// `slices.test.ts` fails if a slice key is rendered from a route that does not
// load its slice.
export const load = () => ensureSlices('audit');
