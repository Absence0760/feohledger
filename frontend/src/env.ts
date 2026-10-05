/**
 * Every environment variable the SPA reads, declared once (SvelteKit 3 has no
 * implicit `$env/*` modules — app code imports these from `$app/env/public`,
 * and `app.html` reads them as `%sveltekit.env.NAME%`).
 *
 * All of them are `static`: this is an adapter-static build served as files
 * from S3, so there is no server to read a value "when the app starts" — the
 * value in the build environment is the value that ships. Which of them may be
 * unset is a deliberate, per-variable choice; see each entry and
 * `docs/environment.md`.
 */
import { defineEnvVars } from '@sveltejs/kit/env';

export const variables = defineEnvVars({
	/**
	 * The API origin on a platform host. REQUIRED: a build without it would
	 * call nothing, so an unset value fails the build rather than shipping.
	 */
	PUBLIC_API_URL: {
		public: true,
		static: true,
	},
	/**
	 * The registrable domains the platform itself serves. Unset is legal and
	 * means "replay the pre-vanity-domain rule" (`#lib/hostRouting.ts`) — every
	 * build that predates it sets only `PUBLIC_API_URL`.
	 */
	PUBLIC_PLATFORM_DOMAINS: {
		public: true,
		static: true,
		schema: (value) => value,
	},
	/**
	 * The origin prefixing the og:image / twitter:image URLs in `app.html`.
	 * Unset renders a root-relative URL; the deploy paths fail when it is empty
	 * (`docs/environment.md`), so the build itself stays permissive.
	 */
	PUBLIC_SITE_URL: {
		public: true,
		static: true,
		schema: (value) => value ?? '',
	},
});
