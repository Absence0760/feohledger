import { execFileSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	expect,
	test as base,
	type Browser,
	type Locator,
	type Page
} from '@playwright/test';

import { API_BASE as _API_BASE, TENANT_ROOT_URL, WEB_ORIGIN, tenantOrigin } from './env';

// `frontend/package.json` is `"type": "module"`, so the CommonJS
// `__dirname` global isn't defined here. Recover it from
// `import.meta.url` so the AUTH_DIR resolves relative to this file's
// location regardless of the test runner's cwd.
const _thisFile = fileURLToPath(import.meta.url);
const _thisDir = path.dirname(_thisFile);

/**
 * Per-worker tenant isolation for parallel Playwright execution.
 *
 * `backend/scripts/seed.py` provisions `FEOH_E2E_TENANT_COUNT` (default 4)
 * `e2e<N>` tenants. Each Playwright worker maps to one tenant via
 * `parallelIndex`, so a worker that creates / deletes data in
 * `e2e1` can't collide with another worker working in `e2e2`. Spec
 * files import `test` from this module (not `@playwright/test`); the
 * fixture below overrides `baseURL` and injects role-specific creds
 * so most specs need no further changes.
 *
 * Auth storage state: a worker-scoped `storageState` fixture lazy-
 * creates `.auth/<tenantSlug>-admin.json` on first use by signing the
 * worker's admin into a temporary context. Every subsequent test in
 * that worker boots the page with the JWT already in localStorage —
 * no `signInAndWait` needed in `beforeEach`. Specs that need a fresh
 * unauthenticated browser (login UI, auth-wall, signup) opt out via
 * `test.use({ storageState: { cookies: [], origins: [] } })` at the
 * top of the file or describe block.
 */

const AUTH_DIR = path.resolve(_thisDir, '../.auth');

// Exported so `fixtures/globalSetup.ts` (the workflow-definition shape guard,
// see docs/known-issues.md § "Workflow-mutating e2e specs can strand a tenant
// on a disabled workflow definition") enumerates the same tenant set as the
// per-worker fixture below, instead of re-deriving the env-var precedence.
export const E2E_TENANT_COUNT = parseInt(
	process.env.E2E_TENANT_COUNT ?? process.env.FEOH_E2E_TENANT_COUNT ?? '4',
	10
);

// Optional fixed offset added to every worker's tenant index. Defaults to
// 0 (no change to normal single- or multi-worker runs). It exists so that
// several *independent* Playwright processes (e.g. parallel authoring
// sessions, each running with PLAYWRIGHT_WORKERS=1) can each be pinned to a
// distinct `e2e<N>` tenant instead of all colliding on `e2e1`: process k
// sets `E2E_TENANT_OFFSET=k` and its lone worker resolves to `e2e<k+1>`.
const E2E_TENANT_OFFSET = parseInt(process.env.E2E_TENANT_OFFSET ?? '0', 10);

type TenantCreds = { email: string; password: string };

type WorkerFixtures = {
	tenantSlug: string;
	tenantAdmin: TenantCreds;
	tenantManager: TenantCreds;
	tenantClerk: TenantCreds;
	tenantCfo: TenantCreds;
};

// Keyed on `parallelIndex`, never `workerIndex`. Playwright replaces a worker
// after a failed test and the replacement gets a NEW `workerIndex` (4, 5, …), so
// `workerIndex % E2E_TENANT_COUNT` wrapped it onto a tenant a still-running
// worker owned: after the first failure in a local 4-worker run, two workers
// wrote to `e2e1` at once and counting specs failed on the other one's rows.
// `parallelIndex` is what Playwright guarantees is distinct among live workers
// (0 … workers-1) and is inherited by a replacement. CI provisions one tenant,
// so there the two keys resolve identically.
function _tenantSlugFor(parallelIndex: number): string {
	return `e2e${((parallelIndex + E2E_TENANT_OFFSET) % Math.max(E2E_TENANT_COUNT, 1)) + 1}`;
}

function _credsFor(slug: string, role: 'admin' | 'manager' | 'clerk' | 'cfo'): TenantCreds {
	return { email: `demo+${role}@${slug}.localhost`, password: 'demo' };
}

export const test = base.extend<object, WorkerFixtures>({
	tenantSlug: [
		async ({}, use, workerInfo) => {
			await use(_tenantSlugFor(workerInfo.parallelIndex));
		},
		{ scope: 'worker' }
	],
	tenantAdmin: [
		async ({ tenantSlug }, use) => {
			await use(_credsFor(tenantSlug, 'admin'));
		},
		{ scope: 'worker' }
	],
	tenantManager: [
		async ({ tenantSlug }, use) => {
			await use(_credsFor(tenantSlug, 'manager'));
		},
		{ scope: 'worker' }
	],
	tenantClerk: [
		async ({ tenantSlug }, use) => {
			await use(_credsFor(tenantSlug, 'clerk'));
		},
		{ scope: 'worker' }
	],
	tenantCfo: [
		async ({ tenantSlug }, use) => {
			await use(_credsFor(tenantSlug, 'cfo'));
		},
		{ scope: 'worker' }
	],
	baseURL: async ({ tenantSlug }, use) => {
		await use(tenantOrigin(tenantSlug));
	},
	// Default storage state for every test: the worker's tenant admin
	// is already signed in. First test per worker pays the ~1–2 s login
	// cost once and persists the resulting localStorage to disk; every
	// subsequent test in the worker loads the file in <100 ms.
	//
	// Specs that need to test the login UI itself, the auth wall, or
	// signup must opt out:
	//
	//   test.use({ storageState: { cookies: [], origins: [] } });
	//
	// Specs that need a different role keep their explicit
	// `signInAndWait(page, tenantClerk)` — the storage-state preload is
	// only the *default*, not a hard contract.
	storageState: async ({ browser, tenantSlug, tenantAdmin }, use) => {
		await use(await _ensureAdminStorageState(browser, tenantSlug, tenantAdmin));
	},
	// Pre-navigate the page to the worker's tenant root before yielding
	// to the test. Why this exists:
	//
	// `storageState` populates the browser context's localStorage for
	// the tenant origin, but a freshly-created `page` starts at
	// `about:blank`. Reading localStorage on `about:blank` throws
	// `SecurityError: Failed to read the 'localStorage' property` —
	// browsers refuse storage access on the empty-document scheme.
	//
	// Specs that already start with a `page.goto(...)` in `beforeEach`
	// pay one redundant navigation, which is cheap. Specs that go
	// straight from arg destructuring to an API call (like
	// `admin/delete-safety.spec.ts`'s `await createUser(page, …)`)
	// would otherwise fail at the first `authToken(page)` because the
	// page is still on `about:blank`. The pre-nav lifts the SecurityError
	// while keeping the storage-state speed-up.
	//
	// Opt-out specs (`storageState: { cookies: [], origins: [] }`)
	// still get the pre-nav; for them the worker's tenant root
	// redirects to `/login` (no auth) which is the same place those
	// specs were going to navigate next anyway.
	//
	// It also watches for a stub answering a dev-server MODULE request — see
	// `watchForStubbedModules` below.
	page: async ({ page, baseURL }, use) => {
		const stubbedModules = watchForStubbedModules(page);
		if (baseURL) {
			await page.goto(baseURL);
		}
		await use(page);
		expect(stubbedModules, STUBBED_MODULE_HINT).toEqual([]);
	}
});

const STUBBED_MODULE_HINT =
	'A page.route() stub answered a dev-server MODULE request with JSON, so the ' +
	"route's code never loaded (SvelteKit renders its 500 page). Under `vite dev` " +
	'the source is served over HTTP — `$lib/api/vendors.ts` is `/src/lib/api/vendors.ts` — ' +
	'so a pattern like `**/api/vendors*` or `/\\/api\\/vendors/` matches the module as well ' +
	'as the API call. Match the API request by its exact pathname instead. See ' +
	'tests-e2e/README.md § Stubbing an API route.';

/**
 * Collect every module script the page received a JSON body for.
 *
 * Only a `page.route()` stub produces that: Vite serves every module —
 * including a JSON import — as JavaScript, and the preview build CI runs
 * serves hashed `/_app/immutable/` chunks no API pattern can match. So under
 * `vite dev` an over-broad stub turns into a route that silently fails to
 * load, and the spec then times out on whatever it looks for first — four
 * `/credit-memos` specs sat red locally and green in CI for exactly this
 * reason, with nothing in their output naming the stub (issue #443). Checked
 * in the `page` fixture's teardown, so the failure names the module and the
 * fix instead of a missing row.
 */
function watchForStubbedModules(page: Page): string[] {
	const offenders: string[] = [];
	page.on('response', (response) => {
		if (response.request().resourceType() !== 'script') return;
		const type = response.headers()['content-type'] ?? '';
		if (type.includes('json')) offenders.push(new URL(response.url()).pathname);
	});
	return offenders;
}

/**
 * Read the auth_token value out of a persisted storageState JSON
 * (format: `{origins:[{localStorage:[{name,value}]}]}`).
 * Returns null when the file is missing, malformed, or holds no token.
 */
function _readStoredToken(file: string): string | null {
	try {
		const raw = fs.readFileSync(file, 'utf8');
		const parsed = JSON.parse(raw) as {
			origins?: Array<{ localStorage?: Array<{ name: string; value: string }> }>;
		};
		for (const origin of parsed.origins ?? []) {
			for (const entry of origin.localStorage ?? []) {
				if (entry.name === 'auth_token') return entry.value;
			}
		}
	} catch {
		/* missing or corrupt file — treat as invalid */
	}
	return null;
}

/**
 * Probe the API with a stored JWT to decide if the storageState file is
 * still usable. Returns true only when `/api/auth/me` responds 2xx.
 * A 401 (expired, blocklisted, or otherwise revoked) returns false.
 * Network errors also return false so the caller re-logs in.
 */
async function _isStoredTokenValid(token: string, tenantSlug: string): Promise<boolean> {
	try {
		const res = await fetch(`${_API_BASE}/api/auth/me`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': tenantSlug
			}
		});
		return res.ok;
	} catch {
		return false;
	}
}

/** Worker-scoped lazy creator for the per-tenant admin storage-state
 *  file. The first test in a worker signs the admin into a throwaway
 *  context, persists the localStorage to disk, and closes the
 *  context. Subsequent tests just read the file path.
 *
 *  The file is validated on every use: the stored JWT is probed against
 *  `/api/auth/me`. If it returns 401 (expired or blocklisted by the
 *  session-management eviction system) the stale file is deleted and
 *  a fresh login regenerates it. This prevents the "not signed in" flake
 *  that occurs when another test's `signInAndWait` call pushes the cached
 *  JTI out of the active-sessions set and into the Redis blocklist. */
async function _ensureAdminStorageState(
	browser: Browser,
	tenantSlug: string,
	creds: TenantCreds
): Promise<string> {
	const file = path.join(AUTH_DIR, `${tenantSlug}-admin.json`);
	if (fs.existsSync(file)) {
		const token = _readStoredToken(file);
		if (token && (await _isStoredTokenValid(token, tenantSlug))) {
			return file;
		}
		// Token missing, expired, or blocklisted — remove the stale file so
		// we fall through to re-login below.
		fs.unlinkSync(file);
	}

	fs.mkdirSync(AUTH_DIR, { recursive: true });
	const context = await browser.newContext({
		baseURL: tenantOrigin(tenantSlug)
	});
	try {
		const page = await context.newPage();
		await page.goto('/login');
		// No `waitForLoadState('networkidle')` here — see `signIn` below for why
		// `.fill()`'s own auto-wait IS the hydration gate this once needed. The
		// two sign-in paths must stay identical on this point; a wait restored to
		// one and not the other is worse than either choice.
		await page.locator('input[type="email"]').fill(creds.email);
		await page.locator('input[type="password"]').fill(creds.password);
		await page.locator('form button[type="submit"]').click();
		// Mirror signInAndWait's success contract — land on the tenant
		// dashboard URL before snapshotting storage. If the redirect
		// hasn't happened, the localStorage hasn't been written yet.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		// Bake a recorded cookie-consent choice into the persisted state so the
		// GDPR consent banner (position:fixed, bottom-centre, z-index 10000) is
		// hidden for every authenticated spec. The banner otherwise overlaps the
		// app's bottom-anchored controls (BulkBar, modal footers, Load-more) and
		// intercepts their clicks — the systemic cause of the e2e shard failures.
		// This is the post-consent steady state every real session is in after
		// the first visit; it is NOT an init script, so consent-banner.spec.ts
		// (which removes the key and reloads to assert the banner) still works.
		await page.evaluate(() => localStorage.setItem('feoh_consent_choice', 'accepted'));
		await context.storageState({ path: file });
	} finally {
		await context.close();
	}
	return file;
}

export { expect };

/**
 * Resolve a per-worker admin from `test.info()` for callers (signIn helpers)
 * that don't take the fixture as an argument. Falls back to ACME_ADMIN when
 * called outside a Playwright test context (e.g. globalSetup, unit tests).
 */
function _currentWorkerAdmin(): TenantCreds {
	try {
		const pi = base.info().parallelIndex;
		return _credsFor(_tenantSlugFor(pi), 'admin');
	} catch {
		return ACME_ADMIN;
	}
}

/**
 * Seeded credentials for the two non-e2e demo tenants (acme + techflow).
 * These stay seeded so the cross-tenant isolation specs have a stable
 * pair of distinct tenants to exercise. New parallel specs should prefer
 * the `tenantAdmin` / `tenantClerk` worker fixtures above instead.
 */
export const ACME_ADMIN = {
	email: 'demo@acme.com',
	password: 'demo'
} as const;

export const ACME_CLERK = {
	email: 'demo+apclerk@acme.com',
	password: 'demo'
} as const;

export const ACME_MANAGER = {
	email: 'demo+apmanager@acme.com',
	password: 'demo'
} as const;

export const ACME_CFO = {
	email: 'demo+cfo@acme.com',
	password: 'demo'
} as const;

export const TECHFLOW_ADMIN = {
	email: 'admin@techflow.com',
	password: 'demo'
} as const;

/** Tenant origins, derived from `E2E_WEB_ORIGIN` (default
 *  `http://localhost:7777`). `*.localhost` resolves to 127.0.0.1 in Chromium. */
export const ACME_BASE = tenantOrigin('acme');
export const TECHFLOW_BASE = tenantOrigin('techflow');
export const NO_TENANT_BASE = WEB_ORIGIN;

/** Build a tenant origin from a slug — used by specs that want to address
 *  the current worker's tenant explicitly (e.g. when overriding baseURL
 *  on a specific page.goto). */
export function tenantBase(slug: string): string {
	return tenantOrigin(slug);
}

/** The post-login landing URL (the tenant root). Re-exported so a spec can wait
 *  on it instead of hardcoding a `:7777` pattern of its own. */
export { TENANT_ROOT_URL };

/** Escape every regex metacharacter so a literal URL can be embedded in a RegExp. */
export function escapeRegExp(input: string): string {
	return input.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
}

/**
 * Record a cookie-consent choice before the first paint.
 *
 * The GDPR consent banner is `position: fixed`, bottom-centre, `z-index:
 * 10000`, so it sits directly over the app's bottom-anchored controls —
 * BulkBar, modal footers, Load-more, and any table row near the fold — and
 * intercepts their clicks. That was the systemic cause of a family of e2e
 * shard failures.
 *
 * Authenticated AP specs get this for free two ways: baked into the persisted
 * admin storage state by `_ensureAdminStorageState`, and as an init script in
 * `signIn` for specs that sign in fresh. **The supplier portal has its own
 * sign-in path and its own token key, so it gets neither** — every portal spec
 * that opts out of the storage state has to call this itself. Four of them had
 * hand-copied the same block; this is that block, hoisted, so the next portal
 * spec cannot forget it and then fail intermittently depending on where the
 * banner happens to land relative to a click.
 *
 * It is an init script rather than a `page.evaluate`, because the banner
 * renders on first paint — writing the key after `goto` is already too late.
 * `consent-banner.spec.ts` deliberately uses neither path, so the banner still
 * shows there and its assertions still hold.
 */
export async function acceptConsent(page: Page) {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			/* about:blank — ignore */
		}
	});
}

/**
 * Drive the email-password sign-in form on the current worker's tenant.
 * The frontend's tenant resolution requires hitting an `<slug>.localhost`
 * URL, so the playwright.config.ts baseURL is a tenant origin.
 *
 * Returns once the submit click has fired. Callers assert the
 * destination URL (or use `signInAndWait`, which does).
 *
 * **Why there is no `waitForLoadState('networkidle')` after the `goto`.**
 * This JSDoc used to carry one, and to justify it: Svelte 5 binds the form's
 * `onsubmit` only after hydration, so a click before that would fire the
 * native GET submit and navigate to /login?email=…&password=… — visually
 * identical to "still on /login" but with no auth POST attempted. That hazard
 * needs a form to exist in the pre-hydration document, and in this app one
 * never does. `routes/+layout.svelte` holds `hasTenant` as a tri-state that
 * starts `undefined` and is only assigned inside a `browser`-guarded
 * `$effect`; effects do not run during SSR or prerender, so the layout renders
 * its empty branch and `<slot/>` — the whole login page — is never emitted.
 * `pnpm build` shows it directly: the entire `build/` tree is one
 * `index.html` whose body is an empty `<div style="display: contents">`, with
 * no `<form>` and no `<input>` anywhere in the file, and `vite preview` (what
 * CI serves) hands that same fallback to every path including /login.
 *
 * So the email input cannot exist until hydration has run the layout effect
 * and rendered the login component — which creates the `<form onsubmit=…>` and
 * its inputs in one pass, with the handler attached at element creation
 * (`submit` is not in Svelte 5's delegated-event set, so there is no window
 * where the element exists unbound). `.fill()` auto-waits for that element,
 * which makes it a strictly stronger hydration gate than a quiet network ever
 * was: `networkidle` only ever claimed no request had been made for 500ms,
 * never that anything had rendered.
 *
 * Do not restore the wait here without restoring it in
 * `_ensureAdminStorageState` too — the two sign-in paths must agree.
 */
export async function signIn(
	page: Page,
	creds?: { email: string; password: string }
) {
	// Default to the current worker's tenant admin so a spec running in
	// worker N signs into e2eN rather than acme. Specs that need a
	// specific tenant (cross-tenant isolation tests, etc.) pass `creds`
	// explicitly.
	const resolved = creds ?? _currentWorkerAdmin();
	// Record a cookie-consent choice before the first paint so the GDPR consent
	// banner (position:fixed, bottom-centre, z-index 10000) stays hidden for
	// specs that sign in fresh via this path (non-admin roles, cross-tenant
	// flows) — it otherwise overlaps the app's bottom-anchored controls and
	// intercepts their clicks. Mirrors the persisted-state injection in
	// _ensureAdminStorageState. consent-banner.spec.ts uses neither path, so the
	// banner still shows there.
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			/* about:blank — ignore */
		}
	});
	await page.goto('/login');

	await page.locator('input[type="email"]').fill(resolved.email);
	await page.locator('input[type="password"]').fill(resolved.password);
	await page.locator('form button[type="submit"]').click();
}

/**
 * Sign in and wait for the post-login redirect to land on the tenant
 * root (`goto('/')` is what the login handler runs on success). Use
 * this when subsequent assertions need the authed app shell.
 */
export async function signInAndWait(
	page: Page,
	creds?: { email: string; password: string }
) {
	await signIn(page, creds);
	// The tenant root is the dashboard. URL must end in just '/' — using
	// a trailing-slash regex anchors the match against descendant paths
	// like '/login/mfa'.
	await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
}

/**
 * Click the sidebar profile button → Log Out and assert the redirect
 * to /login.
 */
export async function signOut(page: Page) {
	await page.locator('.profile-btn').click();
	await page.locator('.profile-logout').click();
	await expect(page).toHaveURL(/\/login/);
}

/** Read the current worker's tenant slug. Falls back to `acme` outside a
 *  Playwright context (e.g. unit-test imports). Use in API-request blocks
 *  that need `X-Tenant-Slug` so a spec running in worker N targets e2eN
 *  rather than always acme. */
export function currentTenantSlug(): string {
	try {
		return _tenantSlugFor(base.info().parallelIndex);
	} catch {
		return 'acme';
	}
}

/** Read the JWT the frontend stored after login. Throws when called
 *  before a successful sign-in. */
export async function authToken(page: Page): Promise<string> {
	const t = await page.evaluate(() => localStorage.getItem('auth_token'));
	if (!t) throw new Error('not signed in');
	return t;
}

/** Build the `Authorization` + `X-Tenant-Slug` headers for an
 *  authenticated, tenant-scoped API request. Defaults to the current
 *  worker's tenant; pass `slug` to target a specific one (cross-tenant
 *  isolation tests). */
export function tenantHeaders(token: string, slug?: string): Record<string, string> {
	return {
		Authorization: `Bearer ${token}`,
		'X-Tenant-Slug': slug ?? currentTenantSlug()
	};
}

/** Resolve an authenticated API request's headers in one shot. Reads
 *  the token from `localStorage`, then composes the tenant headers. */
export async function authedTenantHeaders(
	page: Page,
	slug?: string
): Promise<Record<string, string>> {
	return tenantHeaders(await authToken(page), slug);
}

/** Run a synchronous `psql -c <query>` against a tenant DB. Defaults to
 *  the current worker's tenant. Used by specs that need to set up /
 *  inspect state the API doesn't expose (e.g. clobbering
 *  `assigned_to_id` to provoke a blocked-delete branch). */
export function tenantPsql(query: string, slug?: string): string {
	const db = `feoh_${slug ?? currentTenantSlug()}`;
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', db, '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out.toString();
}

/**
 * Run a synchronous `psql -c <query>` against the CONTROL-plane database.
 *
 * The sibling of `tenantPsql` for the rows that do not live in a tenant DB —
 * organizations, users, roles, API keys, plans, subscriptions, webhook
 * subscriptions. Same connection defaults, same synchronous shape.
 *
 * **Scope every statement to the ids the test itself created.** A tenant DB is
 * per-worker, so a `LIKE 'e2e-%'` sweep there can only ever hit that worker's
 * own rows; the control plane is shared by every worker AND every tenant, so
 * the same sweep would delete a concurrent worker's in-flight row. Delete by
 * primary key.
 */
export function controlPsql(query: string): string {
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', 'feohledger', '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out.toString();
}

/**
 * Delete the virtual cards matching `predicate`, and everything referencing them.
 *
 * Shared by the invoice and vendor teardowns, because a card hangs off BOTH an
 * invoice (and its payment) and a vendor, and carries three references of its
 * own that neither caller should have to know about. Not exported: a spec
 * deleting a card in isolation should be deleting its invoice or its vendor.
 *
 * `corporate_card_transactions` is an imported feed row that merely *points* at
 * the card, so its link is cleared rather than the row deleted — the same
 * distinction `deleteInvoicesWhere` draws for `vendor_statement_recon_lines`.
 */
function deleteVirtualCardsWhere(predicate: string, slug?: string): void {
	const cards = `SELECT id FROM virtual_cards WHERE ${predicate}`;
	tenantPsql(`DELETE FROM card_rebates WHERE virtual_card_id IN (${cards})`, slug);
	tenantPsql(`DELETE FROM card_reveal_tokens WHERE card_id IN (${cards})`, slug);
	tenantPsql(
		`UPDATE corporate_card_transactions SET virtual_card_id = NULL WHERE virtual_card_id IN (${cards})`,
		slug
	);
	tenantPsql(`DELETE FROM virtual_cards WHERE ${predicate}`, slug);
}

/**
 * Delete the invoices matching `predicate`, and everything that references them.
 *
 * `invoices` is referenced by 16 foreign keys and none of them cascade, so a
 * bare `DELETE FROM invoices WHERE ...` only works while the invoice happens to
 * have no children. Specs that hand-maintained a subset of that list were
 * quietly depending on which children the app had created for them — and one of
 * them broke the moment invoice extraction started succeeding (it had always
 * been rolling back before writing its line items, so none existed to block the
 * delete).
 *
 * Deletes second-level children first, then every direct child, then the
 * invoices themselves. `predicate` is the WHERE clause body, so callers can
 * scope by id, `invoice_number LIKE`, `vendor_id`, or anything else.
 */
export function deleteInvoicesWhere(predicate: string, slug?: string): void {
	const ids = `SELECT id FROM invoices WHERE ${predicate}`;

	// Second level — these reference a child, not the invoice.
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id IN (${ids}))`,
		slug
	);
	tenantPsql(
		`DELETE FROM supplier_chat_messages WHERE thread_id IN (SELECT id FROM supplier_chat_threads WHERE invoice_id IN (${ids}))`,
		slug
	);
	tenantPsql(
		`DELETE FROM bank_transactions WHERE matched_payment_id IN (SELECT id FROM payments WHERE invoice_id IN (${ids}))`,
		slug
	);
	// A card can hang off the payment or off the invoice, and carries children
	// of its own — `deleteVirtualCardsWhere` owns that sub-graph for both this
	// helper and `deleteVendorsWhere`.
	deleteVirtualCardsWhere(
		`payment_id IN (SELECT id FROM payments WHERE invoice_id IN (${ids})) OR invoice_id IN (${ids})`,
		slug
	);

	// Direct children. `payments` is self-referential via `retry_of_payment_id`,
	// so clear that link before deleting the rows.
	tenantPsql(
		`UPDATE payments SET retry_of_payment_id = NULL WHERE invoice_id IN (${ids})`,
		slug
	);
	for (const table of [
		'agent_decisions',
		'credit_memos',
		'discount_offers',
		'exceptions',
		'invoice_embeddings',
		'invoice_extraction_results',
		'invoice_line_items',
		'payment_schedules',
		'payments',
		'peppol_transmissions',
		'supplier_chat_threads',
		'workflow_instances'
	]) {
		tenantPsql(`DELETE FROM ${table} WHERE invoice_id IN (${ids})`, slug);
	}
	tenantPsql(
		`UPDATE vendor_statement_recon_lines SET matched_invoice_id = NULL WHERE matched_invoice_id IN (${ids})`,
		slug
	);
	// An inter-company mirror points at its origin invoice.
	tenantPsql(
		`UPDATE invoices SET intercompany_mirror_id = NULL WHERE intercompany_mirror_id IN (${ids})`,
		slug
	);

	tenantPsql(`DELETE FROM invoices WHERE ${predicate}`, slug);
}

/**
 * Delete the vendors matching `predicate`, and everything that references them.
 *
 * `vendors` is referenced by 17 foreign keys and only two of them cascade
 * (`vendor_change_requests`, `vendor_users`), so a bare
 * `DELETE FROM vendors WHERE ...` only works while the vendor happens to have
 * none of the other fifteen kinds of child. Seventeen specs hand-rolled that
 * delete, each maintaining its own partial child list — `vendors/import-csv`
 * knew about `sanctions_checks`, `invoices/coding-suggestions` about
 * `vendor_extraction_priors`, most about nothing — which is the same trap
 * `deleteInvoicesWhere` was written for, one table over: the list is only
 * correct until the app writes a child the spec never anticipated.
 *
 * The graph below was derived from `pg_constraint` against a live tenant
 * database rather than from any spec's list, and is walked to its leaves:
 * invoices (delegated), the procurement chain (catalogs → items, contracts →
 * line items, requisitions → line items, POs → receipts → GR line items and
 * inspections), virtual cards (delegated), statement reconciliations, and the
 * eight flat children.
 *
 * Rows that exist only because of the vendor are deleted; independent records
 * that merely point at one of its rows (an invoice's `contract_id`, an intake
 * request's `converted_po_id`, another requisition's catalog line) have the
 * link cleared instead. `predicate` is the WHERE clause body, so callers can
 * scope by id, `name LIKE`, `entity_id`, or anything else.
 */
export function deleteVendorsWhere(predicate: string, slug?: string): void {
	const ids = `SELECT id FROM vendors WHERE ${predicate}`;
	const pos = `SELECT id FROM purchase_orders WHERE vendor_id IN (${ids})`;
	const grs = `SELECT id FROM goods_receipts WHERE po_id IN (${pos})`;
	const reqs = `SELECT id FROM purchase_requisitions WHERE vendor_id IN (${ids})`;
	const contracts = `SELECT id FROM contracts WHERE vendor_id IN (${ids})`;
	const catalogs = `SELECT id FROM catalogs WHERE vendor_id IN (${ids})`;
	const templates = `SELECT id FROM recurring_invoice_templates WHERE vendor_id IN (${ids})`;

	// Invoices own a 16-table sub-graph of their own; delegate rather than
	// restate it. This also clears the vendor's payments, exceptions, chat
	// threads and workflow rows.
	deleteInvoicesWhere(`vendor_id IN (${ids})`, slug);

	// Procurement, leaves first. An inspection can hang off either the PO or
	// the goods receipt, so both parents are covered before either is deleted.
	tenantPsql(`DELETE FROM gr_line_items WHERE gr_id IN (${grs})`, slug);
	tenantPsql(`DELETE FROM quality_inspections WHERE po_id IN (${pos}) OR gr_id IN (${grs})`, slug);
	tenantPsql(`DELETE FROM goods_receipts WHERE po_id IN (${pos})`, slug);
	tenantPsql(`DELETE FROM po_line_items WHERE po_id IN (${pos})`, slug);

	tenantPsql(`DELETE FROM requisition_line_items WHERE requisition_id IN (${reqs})`, slug);
	tenantPsql(
		`UPDATE requisition_line_items SET catalog_item_id = NULL WHERE catalog_item_id IN (SELECT id FROM catalog_items WHERE catalog_id IN (${catalogs}) OR vendor_id IN (${ids}))`,
		slug
	);
	tenantPsql(
		`UPDATE punchout_sessions SET converted_requisition_id = NULL WHERE converted_requisition_id IN (${reqs})`,
		slug
	);
	tenantPsql(`DELETE FROM punchout_sessions WHERE catalog_id IN (${catalogs})`, slug);

	// The intake → requisition → PO conversion chain points forward, so the
	// vendor's own intakes go first and any surviving one is unlinked.
	tenantPsql(`DELETE FROM intake_requests WHERE vendor_id IN (${ids})`, slug);
	tenantPsql(
		`UPDATE intake_requests SET converted_po_id = NULL WHERE converted_po_id IN (${pos})`,
		slug
	);
	tenantPsql(
		`UPDATE intake_requests SET converted_requisition_id = NULL WHERE converted_requisition_id IN (${reqs})`,
		slug
	);
	tenantPsql(
		`UPDATE purchase_requisitions SET converted_po_id = NULL WHERE converted_po_id IN (${pos})`,
		slug
	);
	tenantPsql(
		`UPDATE purchase_requisitions SET contract_id = NULL WHERE contract_id IN (${contracts})`,
		slug
	);
	tenantPsql(`DELETE FROM purchase_requisitions WHERE vendor_id IN (${ids})`, slug);
	tenantPsql(`DELETE FROM purchase_orders WHERE vendor_id IN (${ids})`, slug);

	// A catalog item can belong to the vendor directly or through its catalog.
	tenantPsql(
		`DELETE FROM catalog_items WHERE catalog_id IN (${catalogs}) OR vendor_id IN (${ids})`,
		slug
	);
	tenantPsql(`DELETE FROM catalogs WHERE vendor_id IN (${ids})`, slug);

	tenantPsql(`DELETE FROM contract_line_items WHERE contract_id IN (${contracts})`, slug);
	tenantPsql(`UPDATE invoices SET contract_id = NULL WHERE contract_id IN (${contracts})`, slug);
	tenantPsql(`DELETE FROM contracts WHERE vendor_id IN (${ids})`, slug);

	deleteVirtualCardsWhere(`vendor_id IN (${ids})`, slug);

	// `vendor_statement_recon_lines` cascades from its reconciliation.
	tenantPsql(`DELETE FROM vendor_statement_reconciliations WHERE vendor_id IN (${ids})`, slug);

	// A generated invoice outlives its template's vendor only if it belongs to
	// another one, but the link still has to go before the template does.
	tenantPsql(
		`UPDATE invoices SET recurring_template_id = NULL WHERE recurring_template_id IN (${templates})`,
		slug
	);

	// Flat children. `vendor_change_requests` and `vendor_users` cascade today;
	// they are listed anyway so the graph does not depend on that staying true.
	for (const table of [
		'credit_memos',
		'discount_offers',
		'invoice_embeddings',
		'recurring_invoice_templates',
		'sanctions_checks',
		'vendor_change_requests',
		'vendor_extraction_priors',
		'vendor_users'
	]) {
		tenantPsql(`DELETE FROM ${table} WHERE vendor_id IN (${ids})`, slug);
	}

	tenantPsql(`DELETE FROM vendors WHERE ${predicate}`, slug);
}

/**
 * Delete the workflow definitions whose name starts with `namePrefix`, and
 * everything that references them.
 *
 * `workflow_definitions` is referenced by three foreign keys and none of them
 * cascade — `workflow_versions.definition_id`,
 * `workflow_experiments.workflow_definition_id` and
 * `workflow_instances.definition_id`, the last itself referenced by
 * `workflow_steps.instance_id` — so a bare `DELETE FROM workflow_definitions`
 * only works while the definition happens to have no children. Ten workflow
 * specs each carried a byte-identical hand-rolled copy of that walk, differing
 * only in the marker they swept by: the same trap `deleteInvoicesWhere` and
 * `deleteVendorsWhere` were written for, plus the copies. The graph below was
 * read from `pg_constraint` in a live tenant database, not from any spec.
 *
 * Unlike its two siblings this takes a NAME PREFIX rather than a WHERE clause
 * body, because every caller sweeps by the marker it names its rows with, and
 * the predicate carries a seatbelt no caller should be able to drop:
 * `is_default = false`. That keeps a marker typo away from the seeded default
 * `fixtures/globalSetup.ts` asserts the whole suite against — and away from
 * the `Invoice Processing` stub `services/workflow_engine.py` mints as a
 * last-resort fallback for an org with no active definition, which is neither
 * seeded nor spec-created and whose deletion could leave a tenant with none.
 *
 * Sweeping by name rather than by id is what makes this teardown survive the
 * failures the callers' own `finally` blocks cannot: a create whose POST landed
 * before the nav or canvas render threw, and an interrupted run. It also
 * reaches a row the API refuses to delete — `DELETE /api/workflows/{id}` 409s
 * on an active definition and on one that is the snapshot source for an
 * in-flight invoice.
 */
export function deleteWorkflowsWhere(namePrefix: string, slug?: string): void {
	const doomed =
		`SELECT id FROM workflow_definitions ` +
		`WHERE name LIKE '${namePrefix}%' AND is_default = false`;

	// Second level — `workflow_steps` references the instance, not the definition.
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN ` +
			`(SELECT id FROM workflow_instances WHERE definition_id IN (${doomed}))`,
		slug
	);

	// The three direct children.
	tenantPsql(`DELETE FROM workflow_instances WHERE definition_id IN (${doomed})`, slug);
	tenantPsql(`DELETE FROM workflow_versions WHERE definition_id IN (${doomed})`, slug);
	tenantPsql(`DELETE FROM workflow_experiments WHERE workflow_definition_id IN (${doomed})`, slug);

	tenantPsql(`DELETE FROM workflow_definitions WHERE id IN (${doomed})`, slug);
}

/**
 * Page a Load-more list until `row` is present, then leave it to the caller's
 * own assertion.
 *
 * A spec that creates a row and asserts `expect(row).toBeVisible()` straight
 * after the list loads is asserting something it never meant to: that the row
 * landed in the FIRST page. That holds only while the tenant is nearly empty.
 * The payments queue orders by `due_date ASC NULLS LAST, id` and pages at 20,
 * and the full local seed (`pnpm seed`, no `--lean`) already leaves ~23
 * payable invoices — so an API-created invoice with no due date sorts last and
 * lands on page 2. CI runs `seed.py --lean` (10 invoices/tenant), which is why
 * this only ever bit locally.
 *
 * The fix is to navigate to the row rather than assume its position: drive the
 * list's own "Load more" control until the row appears or the list is
 * exhausted. This is NOT a retry or a wait-longer — every step waits on a real
 * signal (the row count growing), and a genuinely absent row still fails the
 * caller's assertion, with the whole list loaded.
 *
 * **The row count must exclude the placeholder.** `ui/DataTable.svelte` renders
 * loading, errored and genuinely-empty as the SAME one-cell
 * `<tr><td class="empty">` row, distinguished only by its message text — so a
 * bare `tbody tr` count is satisfied while no real row exists. That is a
 * false-negative generator, not a slow test: the opening poll passed against
 * the loading placeholder, the loop then read a Load-more control that had not
 * rendered yet (the footer comes from the same response as the rows), and the
 * helper returned having paged nothing. A page-2 row then failed as "absent",
 * which reads as an app bug. `:not(:has(td.empty))` is the discriminator —
 * the same idiom the discounts spec already uses — and it is deliberately the
 * class rather than `DataTable`'s `data-testid`, so the hand-rolled tables
 * that use the global `.empty` cell are covered too.
 *
 * **Precondition: the row exists somewhere in the list.** Every caller creates
 * it first, so that holds by construction. On a genuinely empty list there is
 * no non-text signal to distinguish "still loading" from "nothing here" (see
 * `frontend/CLAUDE.md` § Data tables), so the opening poll fails on timeout
 * with the message below rather than silently returning — the honest outcome,
 * since the caller's own assertion was going to fail anyway.
 */
export async function loadMoreUntilRow(page: Page, row: Locator): Promise<void> {
	// Real data rows only — never DataTable's loading / errored / empty
	// placeholder, which is a `<tr>` too. See the note above.
	const rows = page.locator('table tbody tr:not(:has(td.empty))');
	const loadMore = page.locator('.btn-load-more');

	// The first page has to be on screen before "is there more?" means anything.
	await expect
		.poll(() => rows.count(), {
			message:
				'loadMoreUntilRow: the table never rendered a real row (only DataTable\'s loading / errored / empty placeholder). The list is empty, still loading past the timeout, or its fetch failed.'
		})
		.toBeGreaterThan(0);

	// Terminates on any finite list: each click consumes one page and the
	// control disappears once every row is loaded.
	while ((await row.count()) === 0 && (await loadMore.count()) > 0) {
		const before = await rows.count();
		await loadMore.click();
		await expect.poll(() => rows.count()).toBeGreaterThan(before);
	}
}

/**
 * Choose a vendor in the shared `ui/VendorPicker` combobox.
 *
 * Every vendor-choosing surface (`/catalogs`, `ContractModal`,
 * `RecurringModal`, `VendorStatementReconModal`, `BulkNegotiationModal`) used
 * to render a native `<select>`, so specs drove it with `selectOption`. The
 * picker is a WAI-ARIA combobox over a SERVER-searched list, which is what
 * makes a vendor past the first page reachable at all — so the spec has to
 * type, exactly as a user does. `selectOption` on it fails as "element is not
 * a <select>", which is the honest failure for a spec that was never updated.
 *
 * `input` is the combobox itself. Reach it with `vendorPicker(scope)` rather
 * than `getByLabel('Vendor')`: the picker renders a clear button named "Clear
 * the selected vendor", so a substring label match resolves to two elements
 * and fails strict mode. Filtering by the `combobox` role is what makes it
 * unambiguous, and it is honest about the control being a combobox now.
 * `name` is matched as a SUBSTRING of the option's accessible name, because an
 * option renders `<name> <code>` when the vendor carries a code.
 */
/**
 * Matches the vendor LIST response, and only it.
 *
 * `/api/vendors/counts` and `/api/vendors/ids` sit under the same prefix and
 * carry the same `search=` / `status=` query, and either can answer first — so
 * a substring match on `/api/vendors` resolves while the table still holds the
 * PREVIOUS result, and whatever the spec reads next is stale. Comparing the
 * pathname exactly is what makes the wait mean "the list has been re-fetched".
 *
 * `carrying` is an optional literal the URL must also contain, e.g.
 * `'search=Office'` or `'page=2'`.
 */
export function isVendorListResponse(url: string, carrying?: string): boolean {
	if (!new URL(url).pathname.endsWith('/api/vendors')) return false;
	return carrying ? url.includes(carrying) : true;
}

/**
 * The shared `ui/VendorPicker`'s combobox input, within `scope` (a dialog, a
 * page, a form). Filtering by role excludes the picker's own clear button,
 * whose accessible name also contains the field label — the strict-mode
 * collision that `getByLabel('Vendor')` walks into.
 */
export function vendorPicker(scope: Page | Locator, name = 'Vendor'): Locator {
	return scope.getByRole('combobox', { name });
}

export async function selectVendorInPicker(input: Locator, name: string): Promise<void> {
	await input.click();
	// Typing IS the reach mechanism: the popup holds one server-filtered page,
	// so a fixture vendor sorted past it is only ever found by searching.
	await input.fill(name);
	// Auto-waits through the search debounce and the request behind it.
	await input.page().getByRole('option', { name }).first().click();
	// The combobox commits by writing the chosen vendor's label into the box;
	// asserting it here means a caller's next step can't run against a field
	// that is still holding the raw search term.
	await expect(input).toHaveValue(new RegExp(escapeRegExp(name)));
}

/**
 * The shared `ui/InvoicePicker`'s combobox input within `scope`, by its label.
 * Same role filter as {@link vendorPicker}, for the same reason: the clear
 * button's name ("Clear the selected invoice") also names the field.
 */
export function invoicePicker(scope: Page | Locator, name: string): Locator {
	return scope.getByRole('combobox', { name });
}

/**
 * Choose an invoice in the shared `ui/InvoicePicker` combobox, by its number.
 *
 * Types the number (the picker searches server-side, so that is the reach
 * mechanism, exactly as in {@link selectVendorInPicker}) and clicks the option
 * whose accessible name STARTS with it. An option reads `<number> <amount>`, so
 * a bare substring would let `INV-1` pick `INV-10`; anchoring on the number
 * followed by whitespace is what makes the choice exact.
 */
export async function selectInvoiceInPicker(input: Locator, invoiceNumber: string): Promise<void> {
	await input.click();
	await input.fill(invoiceNumber);
	// Wait for the SEARCH to answer before choosing. The popup keeps showing the
	// previous page until the debounced request lands, and when the wanted
	// invoice is already on it, clicking there commits, closes the popup and
	// cancels the search — a pick that never exercised the reach mechanism. Once
	// no listed option lacks the number, the list on screen is the answer.
	const listbox = input.page().getByRole('listbox');
	await expect(listbox.getByRole('option').filter({ hasNotText: invoiceNumber })).toHaveCount(0);
	await listbox
		.getByRole('option', { name: new RegExp(`^${escapeRegExp(invoiceNumber)}(\\s|$)`) })
		.click();
	await expect(input).toHaveValue(invoiceNumber);
}

/** The backend origin. Specs that hit `${API_BASE}/api/...` directly import
 *  this instead of redeclaring `process.env.PUBLIC_API_URL ?? …`; it is defined
 *  in `fixtures/env.ts` alongside the web origin, so a worktree configures both
 *  halves from one place. */
export { _API_BASE as API_BASE };

/**
 * Wait until `SectionTabs` has measured itself and the row/menu split is final.
 *
 * Both helpers below read with a bare `.count()`, which has no auto-wait: on a
 * fresh navigation that reads 0 while the component is still mounting, and the
 * caller then looks for the tab in the wrong place. `data-tabs-ready` is the
 * component's own signal that the split is real rather than its pre-measure
 * "render everything inline" pass — waiting on a real state flag, never a sleep.
 */
async function settledSectionTabs(page: Page) {
	await expect(page.locator('.section-tabs[data-tabs-ready="true"]')).toBeVisible();
}

/**
 * Every section sub-tab's href for the current route — the visible row PLUS
 * whatever `SectionTabs` moved into its **More** menu.
 *
 * The bar shows as many tabs as fit and folds the rest behind a disclosure
 * (`docs/decisions.md` §174), so `.section-tabs a.section-tab` alone answers
 * "which tabs fit in 1280px", not "which tabs does this role have" — and a
 * group like Billing (9 children, ~1380px with the sidebar) overflows at the
 * default `Desktop Chrome` viewport. An RBAC or navigation assertion wants the
 * whole set, so this opens the menu when there is one and closes it again.
 */
export async function sectionTabHrefs(page: Page): Promise<string[]> {
	const hrefs = (loc: ReturnType<Page['locator']>) =>
		loc.evaluateAll((els) => els.map((e) => (e as HTMLAnchorElement).getAttribute('href') ?? ''));

	await settledSectionTabs(page);
	const inRow = await hrefs(page.locator('.section-tabs a.section-tab'));

	const more = page.getByRole('button', { name: /more sections/i });
	if ((await more.count()) === 0) return inRow;

	await more.click();
	const inMenu = await hrefs(page.locator('#section-more-menu a'));
	await page.keyboard.press('Escape');
	return [...inRow, ...inMenu];
}

/**
 * Click a section sub-tab by its visible label, opening the More menu first if
 * the tab overflowed out of the row. Use instead of clicking `.section-tab`
 * directly whenever the group can be wide (Billing, Settings).
 */
export async function clickSectionTab(page: Page, label: string) {
	await settledSectionTabs(page);
	const inRow = page.locator('.section-tabs a.section-tab', { hasText: label });
	if (await inRow.count()) {
		await inRow.first().click();
		return;
	}
	await page.getByRole('button', { name: /more sections/i }).click();
	await page.locator('#section-more-menu a', { hasText: label }).first().click();
}
