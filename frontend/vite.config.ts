import adapter from "@sveltejs/adapter-static";
import { sveltekit } from "@sveltejs/kit/vite";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";
import { mdsvex } from "mdsvex";
import Icons from "unplugin-icons/vite";
import { defineConfig, loadEnv } from "vite";

/**
 * `/api` on the SAME origin, proxied to the backend.
 *
 * This is not a convenience: it is the operator requirement a white-label
 * vanity domain carries (`docs/white-label.md` § Custom domains). On a platform
 * host the SPA calls the build-time `PUBLIC_API_URL` cross-origin and never
 * touches this proxy; on a vanity host `getApiBase()` collapses to `''`,
 * because only a same-origin request carries the vanity hostname in `Host` —
 * which is the one thing the backend resolves the tenant from when there is no
 * `X-Tenant-Slug`. Without this, a custom domain could not be exercised on a
 * dev laptop at all, and `tests-e2e/tenant/vanity-host.spec.ts` would be
 * asserting against a 404 from the static server rather than the real path.
 *
 * `changeOrigin` is left at its default (false) deliberately — rewriting `Host`
 * to the target would defeat the entire lookup this exists to make reachable.
 *
 * The target comes from `loadEnv` rather than a bare `process.env` read: this
 * file is inside `tsconfig`'s include set and the project carries no
 * `@types/node`, so the global is untyped here. `loadEnv` merges the real
 * environment over the `.env*` files for the given prefix, which is what makes
 * `E2E_WEB_ORIGIN`-style per-worktree overrides work.
 */
export default defineConfig(({ mode }) => {
	const env = loadEnv(mode, ".", "PUBLIC_");
	// SvelteKit 3 reads its config from the plugin, not `svelte.config.js`.
	// `BASE_PATH` (unprefixed, so `loadEnv` needs the empty prefix to see it) is
	// the sub-path escape hatch — unset everywhere today.
	const { BASE_PATH } = loadEnv(mode, ".", "BASE_PATH");
	const apiProxy = {
		"/api": {
			target: env.PUBLIC_API_URL || "http://localhost:8000",
			changeOrigin: false,
		},
	};

	return {
		plugins: [
			sveltekit({
				extensions: [".svelte", ".md"],
				compilerOptions: {
					modernAst: true,
					warningFilter,
				},
				preprocess: [vitePreprocess(), mdsvex({ extensions: [".md"] })],
				// adapter-static with an SPA fallback: there is no server to render
				// on, so every unprerendered path is served this one document.
				adapter: adapter({
					fallback: "index.html",
				}),
				prerender: {
					entries: ["*"],
				},
				paths: {
					base: (BASE_PATH || "") as "" | `/${string}`,
				},
				inlineStyleThreshold: 0,
			}),
			Icons({
				autoInstall: true,
				compiler: "svelte",
			}),
		],
		server: { proxy: apiProxy },
		preview: { proxy: apiProxy },
	};
});

/**
 * Silence the Svelte compiler warnings this codebase has decided not to act on.
 */
function warningFilter(warning: { code: string }): boolean {
	return ![
		"svelte_component_deprecated",
		"slot_element_deprecated",
		"a11y_no_noninteractive_tabindex",
		"css_unused_selector",
	].includes(warning.code);
}
