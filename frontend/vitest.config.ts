import { defineConfig } from 'vitest/config';

// Unit-test config, kept separate from vite.config.ts (which carries the
// SvelteKit plugin). The i18n unit tests cover the *pure* runtime modules
// (locale negotiation, interpolation, message-catalogue parity) — none of
// which import `$app/*` or the Svelte compiler — so plain Node ESM + the
// dynamic-import() catalogue loaders are all that's needed. The reactive
// rune runtime (store.svelte.ts) isn't unit-tested here; its behaviour is
// exercised through the components in the e2e suite.
//
// No alias is needed for `#lib/...`: it is a package.json `imports` entry, which
// Vite (and so vitest) resolves natively.
export default defineConfig({
	test: {
		environment: 'node',
		include: ['src/**/*.{test,spec}.ts'],
		// Vitest's default (`css: false`) short-circuits every CSS module to an
		// empty string — including one imported `?raw`. The token-pairing guard
		// (`lib/a11y/tokenPairing.test.ts`) reads `app.css` as text to extract
		// the palette, and a silently-empty read would make it pass by scanning
		// nothing. No test imports CSS for its styles, so turning processing on
		// costs nothing else.
		css: true,
	},
});
