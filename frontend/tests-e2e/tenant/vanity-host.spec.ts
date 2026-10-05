import { expect, test } from '../fixtures/helpers';
import { VANITY_ORIGIN } from '../fixtures/env';

/**
 * Host-derived tenant + API-origin resolution (`#lib/tenant.ts` over
 * `#lib/hostRouting.ts`).
 *
 * ## What this spec covers, and what it deliberately doesn't
 *
 * The SPA is now platform-domain aware: a host under a domain listed in
 * `PUBLIC_PLATFORM_DOMAINS` carries the tenant slug as its first label and
 * calls the build-time `PUBLIC_API_URL`; ANY OTHER host is a customer's
 * white-label vanity domain, where the SPA must send **no** `X-Tenant-Slug`
 * (a present header suppresses the backend's `Host` lookup) and must call
 * `/api` **same-origin** (only a same-origin request carries the vanity
 * hostname in `Host` for the backend to resolve the tenant from).
 *
 * **Both halves are covered here now.** Two things had to become true first,
 * and both are worth knowing before touching this file:
 *
 *   1. `PUBLIC_PLATFORM_DOMAINS` reaches BOTH run modes. Locally the suite
 *      boots `pnpm dev`, which reads it from `playwright.config.ts`'s
 *      `webServer.env`; CI sets `FEOH_E2E_USE_PREVIEW=true` and serves a
 *      `vite build` bundle, where it is baked in by the build step's own env.
 *      Both take it from `fixtures/env.ts::PLATFORM_DOMAINS`, so they cannot
 *      disagree — which they must not, because unset means "replay the
 *      pre-change rule", under which no host is a vanity host at all.
 *      **That failure is loud, not silent:** with the variable missing,
 *      `127.0.0.1` classifies as a 4-label platform host with slug `127`, so
 *      the two assertions below fail rather than quietly asserting the
 *      opposite thing.
 *   2. A second origin that serves both the SPA and `/api`. It is the loopback
 *      **IP literal** (`fixtures/env.ts::VANITY_ORIGIN`): every hostname the
 *      harness can reach is `*.localhost`, and `localhost` is exactly what
 *      `PUBLIC_PLATFORM_DOMAINS` declares, so no `.localhost` name can ever
 *      classify as vanity — while an IP literal is never a platform host
 *      unless listed verbatim. `vite.config.ts` proxies `/api` on the same
 *      origin (with `changeOrigin: false`, so the vanity `Host` survives),
 *      which is the operator requirement a real vanity deployment carries
 *      (`docs/white-label.md` § Custom domains); and `playwright.config.ts`
 *      pins the server to `--host 127.0.0.1`, because an IP literal only
 *      connects to the address the server actually bound and Vite's `localhost`
 *      default resolves to `::1` on some machines.
 *
 * Nothing here is stubbed: both describes assert on the requests the SPA
 * actually issues. What the vanity half deliberately does NOT assert is that
 * the backend then resolves a tenant from that `Host` — that needs the origin
 * registered in a tenant's `settings.brand.custom_domains`, which is a
 * cross-org-unique write and belongs to the backend's own coverage
 * (`backend/tests/test_tenant_custom_domain.py`). The half that was
 * unreachable, and is now reachable, is the SPA's: what it sends and where.
 */

interface SeenRequest {
	url: string;
	tenantHeader: string | undefined;
}

test.describe('platform host resolution', () => {
	test('sends the subdomain slug and calls the build-time API origin', async ({
		page,
		tenantSlug,
		baseURL
	}) => {
		const seen: SeenRequest[] = [];
		page.on('request', (req) => {
			// Match on the PATH, and only for an XHR/fetch: the dev server
			// serves module source over HTTP too, and `src/lib/api/audit.ts`
			// contains `/api/` as a substring.
			if (req.resourceType() !== 'xhr' && req.resourceType() !== 'fetch') return;
			if (!new URL(req.url()).pathname.startsWith('/api/')) return;
			seen.push({ url: req.url(), tenantHeader: req.headers()['x-tenant-slug'] });
		});

		await page.goto('/invoices');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expect
			.poll(() => seen.length, { message: 'expected at least one /api request' })
			.toBeGreaterThan(0);

		const pageOrigin = new URL(baseURL!).origin;

		for (const req of seen) {
			// The slug comes off the hostname's first label — `e2e1.localhost`
			// → `e2e1`. Regression guard on `getTenantSlug()`: dropping the
			// header here would push every request onto the backend's
			// custom-domain `Host` lookup, which has no entry for a platform
			// subdomain, and 400 with "Missing X-Tenant-Slug header".
			expect(req.tenantHeader, `X-Tenant-Slug on ${req.url}`).toBe(tenantSlug);

			// `getApiBase()` must still resolve to the build-time
			// `PUBLIC_API_URL` (a separate origin from the SPA) on a platform
			// host. Collapsing to same-origin here is the failure mode of
			// mis-classifying a platform subdomain as a vanity host.
			expect(new URL(req.url).origin, `API origin of ${req.url}`).not.toBe(pageOrigin);
		}
	});
});

test.describe('vanity host resolution', () => {
	// A customer's own domain carries no tenant slug and no signed-in session:
	// `storageState` is keyed by origin, so the worker admin's token does not
	// travel here. Opt out of the default sign-in rather than paying a login
	// whose result this origin cannot see (the documented opt-out for specs
	// that exercise the auth wall).
	test.use({ baseURL: VANITY_ORIGIN, storageState: { cookies: [], origins: [] } });

	test('sends NO tenant slug and calls /api same-origin', async ({ page }) => {
		const seen: SeenRequest[] = [];
		page.on('request', (req) => {
			if (req.resourceType() !== 'xhr' && req.resourceType() !== 'fetch') return;
			if (!new URL(req.url()).pathname.startsWith('/api/')) return;
			seen.push({ url: req.url(), tenantHeader: req.headers()['x-tenant-slug'] });
		});

		// `/login` is the deterministic probe: its `onMount` issues exactly two
		// `/api` calls (`/auth/sso/config`, `/auth/saml/config`), both non-fatal,
		// so the assertions do not depend on a tenant resolving. The page renders
		// either way — `hasTenantContext()` is true for a vanity host — which is
		// why a missing `PUBLIC_PLATFORM_DOMAINS` fails on the assertions below
		// rather than on a timeout.
		await page.goto('/login');
		await expect(page.locator('input[type="password"]')).toBeVisible();
		await expect
			.poll(() => seen.length, { message: 'expected at least one /api request' })
			.toBeGreaterThan(0);

		const pageOrigin = new URL(VANITY_ORIGIN).origin;

		for (const req of seen) {
			// The header is what SUPPRESSES the backend's `Host` lookup, so
			// sending a guessed slug is strictly worse than sending nothing —
			// `ap.acmecorp.com` used to send `X-Tenant-Slug: ap` and 404
			// "Unknown tenant: ap" on every call.
			expect(req.tenantHeader, `X-Tenant-Slug on ${req.url}`).toBeUndefined();

			// …and suppressing the header is only half of it: a vanity host that
			// still called the build-time API origin would hand the backend the
			// PLATFORM's `Host`, so the lookup would have nothing to resolve
			// from. Only a same-origin request carries the vanity hostname.
			expect(new URL(req.url).origin, `API origin of ${req.url}`).toBe(pageOrigin);
		}
	});
});
