import { defineConfig, devices } from '@playwright/test';

import { API_BASE, PLATFORM_DOMAINS, WEB_ORIGIN, WEB_PORT, tenantOrigin } from './fixtures/env';

const PORT_ARG = WEB_PORT ? ` --port ${WEB_PORT}` : '';

// Pin the dev/preview server to the IPv4 loopback ADDRESS, not the `localhost`
// NAME (Vite's default). Two reasons, and the second is load-bearing:
//
//   * It is deterministic. Resolving `localhost` picks one family per glibc's
//     RFC 3484 address selection, which on this machine yields `::1` even
//     though `/etc/hosts` lists 127.0.0.1 first — so which address the server
//     actually listens on is not a property of the config.
//   * `fixtures/env.ts::VANITY_ORIGIN` is the loopback IP literal, and a
//     literal only connects to the address the server bound. Pinning it is what
//     makes `tenant/vanity-host.spec.ts` portable rather than dependent on the
//     box's resolver.
//
// Nothing else changes: `localhost` and every `<slug>.localhost` origin still
// reach it (both Chromium and Node fall back across families for a NAME), and
// this is NARROWER than the default — no LAN interface is exposed.
const HOST_ARG = ' --host 127.0.0.1';

/**
 * Playwright e2e config for the frontend.
 *
 * The frontend reads tenant context from the subdomain
 * (`<slug>.localhost:7777`). Chromium resolves `*.localhost` to
 * 127.0.0.1 by default per RFC 6761, so no /etc/hosts changes are
 * needed.
 *
 * Both origins are configurable — `E2E_WEB_ORIGIN` (default
 * `http://localhost:7777`) and `PUBLIC_API_URL` (default
 * `http://localhost:8000`), resolved in `fixtures/env.ts`. A session
 * working in a git worktree needs that: `reuseExistingServer` is on
 * locally, so without its own port it would silently test the primary
 * checkout's build. See `tests-e2e/README.md` § Running from a worktree.
 *
 * Parallelism + isolation model:
 *
 *   - CI (`.github/workflows/ci.yml`): 14 shards × workers=1. Each
 *     shard is its own GitHub runner with its own Postgres + Redis +
 *     seeded backend (one tenant, e2e1), runs `--shard=N/14` of the
 *     test list serially. Total parallelism = 14 across machines.
 *     Tests inside a shard share the shard's single tenant, but
 *     they run *sequentially* — no within-shard worker contention,
 *     which was the source of the spec-to-spec interference we hit
 *     under the previous 4×4=16 model.
 *   - Local (no `--shard` flag): workers default to 4 for a fast
 *     iteration loop. Each worker is pinned to its own `e2e<N>`
 *     tenant via the worker-scoped `tenantSlug` fixture in
 *     `fixtures/helpers.ts`. Specs within a worker share that
 *     worker's tenant. Override with `PLAYWRIGHT_WORKERS=1` if a
 *     flake suggests within-worker spec interference.
 *
 * Seed (`backend/scripts/seed.py`) honors `FEOH_E2E_TENANT_COUNT`
 * (default 4) — CI sets it to 1 so each shard only provisions
 * one e2e tenant.
 *
 * The seeded `acme` + `techflow` tenants stay around for the
 * cross-tenant-isolation specs that need fixed slugs
 * (`tenant-isolation.spec.ts`, `cross-tenant-writes.spec.ts`).
 *
 * Prereqs (CI handles all of these):
 *   - Postgres running on :5432
 *   - Redis running on :6379
 *   - `python backend/scripts/seed.py` has run (creates acme +
 *     techflow + the e2e tenants)
 *   - Backend running on :8000 (or set PUBLIC_API_URL accordingly)
 *
 * Locally, `cd frontend && pnpm test:e2e` boots the dev server via
 * the webServer block below. The backend has to be running
 * separately.
 */
export default defineConfig({
	testDir: '.',
	// `day-one/` is its own suite with its own config (`day-one/playwright.config.ts`):
	// it runs against an unseeded stack that `scripts/day-one/stack.sh` builds,
	// in CI's day-one job, never in these shards.
	testIgnore: ['**/node_modules/**', '**/.auth/**', '**/fixtures/**', '**/day-one/**'],

	// Runs once in the main process before any test/worker starts. Verifies
	// every tenant's seeded "Default Workflow" is still is_active with its
	// approval + erp_export steps enabled — the cheap guard against
	// docs/known-issues.md § "Workflow-mutating e2e specs can strand a tenant
	// on a disabled workflow definition". See fixtures/globalSetup.ts.
	globalSetup: './fixtures/globalSetup.ts',

	timeout: 30_000,
	expect: { timeout: 10_000 },

	// One retry on CI absorbs incidental flake (dev-server cold start,
	// HMR transient errors). No retries locally so flakes are visible
	// during development.
	retries: process.env.CI ? 1 : 0,

	// Fail fast in CI; locally, run the whole suite even if the seed is
	// stale so the developer can see the full damage.
	forbidOnly: !!process.env.CI,
	// CI sets `PLAYWRIGHT_WORKERS=1` so each shard runs serially
	// (shards do the parallelism, workers don't). Locally we default
	// to 4 workers for a fast inner loop — each worker is pinned to
	// its own `e2e<N>` tenant via the worker-scoped `tenantSlug`
	// fixture in `fixtures/helpers.ts`. The seed
	// (`backend/scripts/seed.py`) provisions `FEOH_E2E_TENANT_COUNT`
	// (default 4) such tenants. Setting workers higher than the
	// tenant count makes the modulo in `fixtures/helpers.ts` wrap,
	// losing isolation between the wrapping workers.
	//
	// `fullyParallel: false` keeps tests *within* a file serial —
	// each file's tests share the worker's tenant, so a file that
	// mutates state (creates a user, voids a payment) needs them
	// ordered. Files across workers still run in parallel.
	workers: parseInt(
		process.env.PLAYWRIGHT_WORKERS ?? process.env.FEOH_E2E_TENANT_COUNT ?? '4',
		10
	),
	fullyParallel: false,

	reporter: process.env.CI ? [['github'], ['list']] : 'list',

	// Auto-start the frontend dev server. PUBLIC_API_URL has to be
	// passed through because vite reads it from process.env at build /
	// dev time. Default to localhost:8000 so a developer's locally-run
	// backend works without extra config.
	//
	// CI sets `FEOH_E2E_USE_PREVIEW=true` and runs `pnpm build` before
	// Playwright starts. Preview serves the static bundle straight
	// from `frontend/build/` — sub-second boot, no on-demand HMR
	// transforms during page navigation. Locally we keep `pnpm dev`
	// so an interactive run picks up source edits.
	webServer: {
		// `--port` on BOTH branches: `pnpm dev` is `vite dev --port 7777` in
		// package.json, so invoking the script directly would ignore
		// `E2E_WEB_ORIGIN` and hand back the primary checkout's port. Empty when
		// the origin uses the scheme's default port — then vite picks its own.
		command:
			process.env.FEOH_E2E_USE_PREVIEW === 'true'
				? `pnpm exec vite preview${PORT_ARG}${HOST_ARG}`
				: `pnpm exec vite dev${PORT_ARG}${HOST_ARG}`,
		url: WEB_ORIGIN,
		reuseExistingServer: !process.env.CI,
		timeout: 60_000,
		stdout: 'ignore',
		stderr: 'pipe',
		env: {
			PUBLIC_API_URL: API_BASE,
			// The dev server's half of the platform-vs-vanity classification.
			// CI's counterpart is the `pnpm build` step's env — a
			// production-mode build does not read `.env.development`, and this
			// value is baked into the bundle there rather than read per request.
			// Both come from `fixtures/env.ts::PLATFORM_DOMAINS`, because the two
			// run modes disagreeing is what kept `tenant/vanity-host.spec.ts`
			// from covering the vanity half at all.
			PUBLIC_PLATFORM_DOMAINS: PLATFORM_DOMAINS
		}
	},

	use: {
		// Fallback baseURL. The per-worker `baseURL` fixture in
		// `fixtures/helpers.ts` routes each worker to its own
		// `http://e2e<N>.localhost:7777`, so this value mostly ends up
		// in cross-tenant specs that pin via `test.use({ baseURL: … })`
		// (e.g. `auth/tenant-isolation.spec.ts`) and in any direct
		// `@playwright/test` import path.
		baseURL: process.env.PLAYWRIGHT_BASE_URL ?? tenantOrigin('acme'),
		trace: 'on-first-retry',
		screenshot: 'only-on-failure',
		video: 'retain-on-failure',
		locale: 'en-US',
		timezoneId: 'UTC'
	},

	projects: [
		{
			name: 'chromium',
			use: { ...devices['Desktop Chrome'] }
		}
	]
});
