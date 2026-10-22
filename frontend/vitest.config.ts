import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

// Unit-test config, kept separate from vite.config.ts (which carries the
// SvelteKit plugin). The i18n unit tests cover the *pure* runtime modules
// (locale negotiation, interpolation, message-catalogue parity) — none of
// which import `$app/*` or the Svelte compiler — so plain Node ESM + the
// dynamic-import() catalogue loaders are all that's needed.
//
// The one plugin is Svelte's own, so a rune store (`*.svelte.ts` — `$state`,
// `$derived`) compiles and can be unit-tested directly: `stores/sidebar` and
// `stores/entity` are. It compiles nothing a test does not import; `?raw`
// imports of `.svelte` files (the source-scan guards) stay plain text. A store
// whose module graph reaches `$app/*` (e.g. through `#lib/api.ts`) mocks that
// import with `vi.mock` — see `stores/entity.test.ts`.
//
// No alias is needed for `#lib/...`: it is a package.json `imports` entry, which
// Vite (and so vitest) resolves natively.
export default defineConfig({
	plugins: [svelte()],
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
