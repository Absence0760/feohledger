import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	classifyHost,
	currentHostname,
	parsePlatformDomains,
	resolveApiBase,
	tenantSlugForHost,
	tenantStorageKeyForHost,
} from './hostRouting';

// These cover `#lib/tenant.ts`'s behaviour through `#lib/hostRouting.ts`, which
// holds the whole rule set. `tenant.ts` itself is four one-line compositions
// over these functions plus the `PUBLIC_PLATFORM_DOMAINS` read, and it can't be
// imported here: it pulls `$app/env/public`, which
// the node-environment `vitest.config.ts` doesn't alias. Splitting the rules out
// is what makes them testable at all — so keep new rules in `hostRouting.ts`,
// never inline in `tenant.ts`.
//
// vitest.config.ts runs these under `environment: 'node'` (no jsdom), so
// `window` isn't defined by default — `currentHostname()`'s own
// `typeof window === 'undefined'` guard is exercised for free by that. To
// exercise the hostname-parsing branches we stub a minimal `window.location`
// per test and unstub it afterward so tests can't leak state into each other.
function stubHostname(hostname: string) {
	vi.stubGlobal('window', { location: { hostname } });
}

/** The dev / e2e configuration committed in `frontend/.env.development`. */
const DEV_DOMAINS = parsePlatformDomains('localhost');
/** A representative deployed configuration. */
const PROD_DOMAINS = parsePlatformDomains('feohledger.com,localhost');
/** No `PUBLIC_PLATFORM_DOMAINS` set — the legacy rule. */
const UNSET = parsePlatformDomains(undefined);

const API_URL = 'http://localhost:8000';

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('parsePlatformDomains', () => {
	it('returns an empty list for unset / blank input', () => {
		expect(parsePlatformDomains(undefined)).toEqual([]);
		expect(parsePlatformDomains(null)).toEqual([]);
		expect(parsePlatformDomains('')).toEqual([]);
		expect(parsePlatformDomains('  ,  ,')).toEqual([]);
	});

	it('splits, trims, lower-cases, strips stray dots and de-duplicates', () => {
		expect(parsePlatformDomains(' FeohLedger.com , .localhost. ,feohledger.com')).toEqual([
			'feohledger.com',
			'localhost',
		]);
	});
});

describe('currentHostname', () => {
	it('returns null when window is undefined (SSR / prerender)', () => {
		// No stub — `window` stays undefined under the node test environment.
		expect(currentHostname()).toBeNull();
	});

	it('returns the browser hostname when window exists', () => {
		stubHostname('acme.localhost');
		expect(currentHostname()).toBe('acme.localhost');
	});
});

describe('getTenantSlug (via tenantSlugForHost)', () => {
	it('returns null with no hostname (SSR / prerender)', () => {
		expect(tenantSlugForHost(null, DEV_DOMAINS)).toBeNull();
		expect(tenantSlugForHost(undefined, PROD_DOMAINS)).toBeNull();
	});

	it('reads the slug off a subdomain of a configured platform domain', () => {
		expect(tenantSlugForHost('acme.feohledger.com', PROD_DOMAINS)).toBe('acme');
		expect(tenantSlugForHost('techflow.feohledger.com', PROD_DOMAINS)).toBe('techflow');
	});

	it('keeps the *.localhost dev convention working (regression)', () => {
		expect(tenantSlugForHost('acme.localhost', DEV_DOMAINS)).toBe('acme');
		expect(tenantSlugForHost('techflow.localhost', DEV_DOMAINS)).toBe('techflow');
		// And through the real window read, the way the app calls it.
		stubHostname('acme.localhost');
		expect(tenantSlugForHost(currentHostname(), DEV_DOMAINS)).toBe('acme');
	});

	it('returns null on a platform apex (the marketing / signup host)', () => {
		expect(tenantSlugForHost('feohledger.com', PROD_DOMAINS)).toBeNull();
		expect(tenantSlugForHost('localhost', DEV_DOMAINS)).toBeNull();
	});

	it('returns null on a customer vanity host, so the backend can use Host', () => {
		// The bug this whole change exists for: the old rule sent
		// `X-Tenant-Slug: ap` here and every call 404'd `Unknown tenant: ap`,
		// because a present header suppresses the backend's Host fallback.
		expect(tenantSlugForHost('ap.acmecorp.com', PROD_DOMAINS)).toBeNull();
		expect(tenantSlugForHost('invoices.acme.co.uk', PROD_DOMAINS)).toBeNull();
	});

	it('returns null on a vanity apex', () => {
		expect(tenantSlugForHost('acmecorp.com', PROD_DOMAINS)).toBeNull();
	});

	it('is case- and trailing-dot-insensitive', () => {
		expect(tenantSlugForHost('ACME.FeohLedger.com.', PROD_DOMAINS)).toBe('acme');
		expect(tenantSlugForHost('AP.AcmeCorp.com', PROD_DOMAINS)).toBeNull();
	});

	it('matches the most specific platform domain first', () => {
		const domains = parsePlatformDomains('app.example.com,example.com');
		expect(tenantSlugForHost('acme.app.example.com', domains)).toBe('acme');
		expect(tenantSlugForHost('app.example.com', domains)).toBeNull();
		expect(tenantSlugForHost('acme.example.com', domains)).toBe('acme');
	});

	it('never yields an empty-string slug for a malformed leading-dot host', () => {
		expect(tenantSlugForHost('.example.com', UNSET)).toBeNull();
	});

	it('replays the pre-change rule when no platform domains are configured', () => {
		// Existing builds set only PUBLIC_API_URL; they must not change.
		expect(tenantSlugForHost('acme.localhost', UNSET)).toBe('acme');
		expect(tenantSlugForHost('acme.example.com', UNSET)).toBe('acme');
		expect(tenantSlugForHost('acme.example.co.uk', UNSET)).toBe('acme');
		expect(tenantSlugForHost('localhost', UNSET)).toBeNull();
		expect(tenantSlugForHost('example.com', UNSET)).toBeNull();
		// …including the bug: an unconfigured build still mis-reads a vanity host.
		expect(tenantSlugForHost('ap.acmecorp.com', UNSET)).toBe('ap');
	});
});

describe('classifyHost', () => {
	it('names each of the four host kinds', () => {
		expect(classifyHost(null, PROD_DOMAINS).kind).toBe('unknown');
		expect(classifyHost('feohledger.com', PROD_DOMAINS).kind).toBe('platform-apex');
		expect(classifyHost('acme.feohledger.com', PROD_DOMAINS).kind).toBe('platform-tenant');
		expect(classifyHost('ap.acmecorp.com', PROD_DOMAINS).kind).toBe('vanity');
	});

	it('never reports a vanity host when nothing is configured', () => {
		expect(classifyHost('ap.acmecorp.com', UNSET).kind).toBe('platform-tenant');
	});

	it('treats the loopback IP literal as vanity — the e2e harness depends on it', () => {
		// `tests-e2e/fixtures/env.ts::VANITY_ORIGIN` is `http://127.0.0.1:<port>`,
		// because no `*.localhost` name can be a vanity host while `localhost` is
		// the declared platform domain, and an IP literal is never a platform host
		// unless listed verbatim. Pinned here so the e2e spec's premise is a unit
		// assertion rather than an assumption about the classifier.
		expect(classifyHost('127.0.0.1', DEV_DOMAINS).kind).toBe('vanity');
		expect(tenantSlugForHost('127.0.0.1', DEV_DOMAINS)).toBeNull();
		expect(resolveApiBase('127.0.0.1', DEV_DOMAINS, API_URL)).toBe('');
		// …and it stops being one the moment an operator declares it.
		expect(classifyHost('127.0.0.1', parsePlatformDomains('127.0.0.1')).kind).toBe(
			'platform-apex'
		);
	});
});

describe('getApiBase (via resolveApiBase)', () => {
	it('uses the build-time PUBLIC_API_URL on a platform host', () => {
		expect(resolveApiBase('acme.feohledger.com', PROD_DOMAINS, API_URL)).toBe(API_URL);
		expect(resolveApiBase('acme.localhost', DEV_DOMAINS, API_URL)).toBe(API_URL);
		expect(resolveApiBase('feohledger.com', PROD_DOMAINS, API_URL)).toBe(API_URL);
	});

	it('uses the same origin on a vanity host', () => {
		// Empty base + a path that already starts with `/api` = a same-origin
		// request, which is the only kind that carries the vanity hostname in
		// `Host` for the backend to resolve the tenant from.
		expect(resolveApiBase('ap.acmecorp.com', PROD_DOMAINS, API_URL)).toBe('');
		expect(resolveApiBase('acmecorp.com', PROD_DOMAINS, API_URL)).toBe('');
	});

	it('falls back to the build-time base with no hostname (prerender)', () => {
		expect(resolveApiBase(null, PROD_DOMAINS, API_URL)).toBe(API_URL);
	});

	it('keeps the build-time base everywhere when nothing is configured', () => {
		expect(resolveApiBase('ap.acmecorp.com', UNSET, API_URL)).toBe(API_URL);
	});

	it('strips trailing slashes off the configured base', () => {
		expect(resolveApiBase('acme.localhost', DEV_DOMAINS, 'http://localhost:8000//')).toBe(
			API_URL
		);
	});

	it('is composed the same way the app composes it', () => {
		stubHostname('ap.acmecorp.com');
		expect(resolveApiBase(currentHostname(), PROD_DOMAINS, API_URL)).toBe('');
	});
});

describe('getTenantStorageKey (via tenantStorageKeyForHost)', () => {
	it('keys by slug on a platform host', () => {
		expect(tenantStorageKeyForHost('acme.feohledger.com', PROD_DOMAINS)).toBe('acme');
	});

	it('keys by hostname on a vanity host, so entity scoping still partitions', () => {
		expect(tenantStorageKeyForHost('ap.acmecorp.com', PROD_DOMAINS)).toBe('ap.acmecorp.com');
		expect(tenantStorageKeyForHost('AP.AcmeCorp.com.', PROD_DOMAINS)).toBe('ap.acmecorp.com');
	});

	it('has no key on the platform apex or with no hostname', () => {
		expect(tenantStorageKeyForHost('feohledger.com', PROD_DOMAINS)).toBeNull();
		expect(tenantStorageKeyForHost(null, PROD_DOMAINS)).toBeNull();
	});
});
