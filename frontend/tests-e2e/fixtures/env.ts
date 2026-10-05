/**
 * Where the e2e suite points — the frontend origin and the backend API base.
 *
 * Both were hardcoded (`:7777` in the per-worker `baseURL` fixture, `:8000` in
 * a handful of `process.env.PUBLIC_API_URL ?? …` repeats), and
 * `playwright.config.ts` reuses an existing server. A session working in a git
 * **worktree** therefore either tested the *primary* checkout's build without
 * noticing, or had to take the port from another session — worktrees isolate
 * files, not ports. Two round-24 agents hit exactly that; one stopped and
 * restarted a server it did not own.
 *
 * So the two origins come from the environment, defaulting to today's values —
 * an existing run is bit-for-bit unchanged, and a worktree can serve on its own
 * port:
 *
 *   E2E_WEB_ORIGIN=http://localhost:7801 PUBLIC_API_URL=http://localhost:8001 \
 *     pnpm test:e2e
 *
 * Everything else is DERIVED from `E2E_WEB_ORIGIN` — the per-tenant subdomain
 * origins, the post-login landing-URL pattern, the dev/preview server's port —
 * so there is one knob for the whole web half, not a port here and a hostname
 * there. Deliberately dependency-free (no `@playwright/test` import) so
 * `playwright.config.ts` can import it at config-load time alongside the
 * fixtures.
 */

/** Today's values. Changing these changes the default for every run. */
const DEFAULT_WEB_ORIGIN = 'http://localhost:7777';
const DEFAULT_API_BASE = 'http://localhost:8000';

function trimTrailingSlashes(value: string): string {
	return value.replace(/\/+$/, '');
}

/** Origin the SvelteKit app is served on, WITHOUT a tenant subdomain. */
export const WEB_ORIGIN = trimTrailingSlashes(process.env.E2E_WEB_ORIGIN ?? DEFAULT_WEB_ORIGIN);

/** Origin the FastAPI backend is served on. Named `PUBLIC_API_URL` because that
 *  is the variable Vite already reads at dev/build time — one value configures
 *  the app under test and the specs that call the API directly. */
export const API_BASE = trimTrailingSlashes(process.env.PUBLIC_API_URL ?? DEFAULT_API_BASE);

const _web = new URL(WEB_ORIGIN);

/** `http:` / `https:` — includes the colon, as `URL.protocol` does. */
export const WEB_PROTOCOL = _web.protocol;
/** `localhost` — the registrable host every tenant subdomain hangs off. */
export const WEB_HOSTNAME = _web.hostname;
/** `7777`, or `''` when the origin uses the scheme's default port. */
export const WEB_PORT = _web.port;
/** `:7777`, or `''` — appendable to a hostname without a conditional. */
export const WEB_PORT_SUFFIX = WEB_PORT ? `:${WEB_PORT}` : '';

/**
 * The origin for one tenant. The frontend reads tenant context from the
 * subdomain, and Chromium resolves `*.localhost` to 127.0.0.1 per RFC 6761, so
 * no /etc/hosts entry is needed for the default host.
 */
export function tenantOrigin(slug: string): string {
	return `${WEB_PROTOCOL}//${slug}.${WEB_HOSTNAME}${WEB_PORT_SUFFIX}`;
}

/**
 * The registrable domains the SPA under test treats as the PLATFORM's own —
 * the value `playwright.config.ts` passes to the dev server as
 * `PUBLIC_PLATFORM_DOMAINS`, and the one CI's `pnpm build` step must bake into
 * the preview bundle.
 *
 * It is derived here rather than written twice because the two run modes
 * disagreeing is the exact failure that kept `tenant/vanity-host.spec.ts` from
 * covering the vanity half at all: unset means "no host is a vanity host", so
 * the same navigation would assert opposite things locally and in CI.
 */
export const PLATFORM_DOMAINS: string = process.env.PUBLIC_PLATFORM_DOMAINS ?? WEB_HOSTNAME;

/**
 * An origin that is deliberately NOT under any platform domain — the harness's
 * stand-in for a customer's `ap.acmecorp.com`.
 *
 * It is the loopback **IP literal**, and that is the whole trick. Every
 * hostname the harness can reach is `*.localhost` (Chromium resolves those to
 * loopback per RFC 6761, which is why no /etc/hosts entry is needed) — and
 * `localhost` is precisely what `PUBLIC_PLATFORM_DOMAINS` declares, so no
 * `.localhost` name can ever classify as vanity. An IP literal is never a
 * platform host unless it is listed verbatim (`#lib/hostRouting.ts`), Vite
 * serves it with no `allowedHosts` entry (IP literals are exempt from the host
 * check), and it needs no DNS and no Chromium resolver rule at all. The code
 * path it exercises is identical to a real vanity hostname's:
 * `kind === 'vanity'` → no slug, same-origin `/api`.
 *
 * A literal connects only to the address the server actually BOUND, which is
 * why `playwright.config.ts` pins the dev/preview server to `--host 127.0.0.1`
 * rather than leaving it on Vite's `localhost` default (which resolves to `::1`
 * on at least one of the machines this runs on). See the comment there.
 */
export const VANITY_ORIGIN: string = `${WEB_PROTOCOL}//127.0.0.1${WEB_PORT_SUFFIX}`;

// Loud at config-load time rather than as a mystifying spec failure: if the
// loopback IP were ever declared a platform domain, `VANITY_ORIGIN` would be a
// platform host and the vanity spec would assert the opposite of its intent.
if (
	PLATFORM_DOMAINS.split(',')
		.map((d: string) => d.trim().toLowerCase().replace(/^\.+|\.+$/g, ''))
		.includes('127.0.0.1')
) {
	throw new Error(
		'PUBLIC_PLATFORM_DOMAINS lists 127.0.0.1, which is what tests-e2e uses as its ' +
			'VANITY_ORIGIN. Pick a different vanity origin or drop the IP from the list.'
	);
}

/**
 * Matches a tenant ROOT url — where the login handler's `goto('/')` lands.
 *
 * The trailing-slash anchor is load-bearing: without it the pattern also
 * matches descendant paths like `/login/mfa`, so a spec would stop waiting
 * before the redirect it is waiting for. The host is left open (`[^/]+`)
 * because the worker's tenant slug varies.
 */
export const TENANT_ROOT_URL = new RegExp(`^${WEB_PROTOCOL}//[^/]+${WEB_PORT_SUFFIX}/?$`);
