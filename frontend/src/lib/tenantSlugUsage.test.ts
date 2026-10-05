import { describe, expect, it } from 'vitest';
import invoicesListSource from '../routes/invoices/+page.svelte?raw';
import invoiceModalSource from './components/modals/InvoiceModal.svelte?raw';

// Regression coverage for issue #170: two call sites hand-rolled
// `document.location.hostname.split('.')[0]` for the X-Tenant-Slug header on
// a raw-fetch binary-download path (bypassing #lib/api.ts by necessity — a
// blob download needs the raw Response). That duplicated getTenantSlug()'s
// logic without its bare-apex-domain guard, so a deployed apex hostname with
// no tenant subdomain would send the apex's first label as a bogus tenant
// slug instead of no header at all. These are pure source-text assertions
// (not component tests — the frontend's vitest setup targets pure modules
// only, per frontend/CLAUDE.md) proving neither site re-derives the subdomain.
//
// The invariant is "never re-derive the slug", not "call getTenantSlug here":
// a site that has since moved onto the shared client (`api.downloadBlob`,
// which sets Authorization + X-Tenant-Slug + X-Entity-ID centrally) satisfies
// it without touching the header at all, and is strictly better than the
// hand-built version this test was originally written against. So the
// getTenantSlug requirement applies only to a file that still builds the
// header itself.
//
// It matters more now than it did for #170. A hand-rolled `hostname.split()`
// no longer just skips the apex guard — it can't tell a platform subdomain
// from a customer's white-label VANITY host, and on a vanity host the correct
// header is *no header at all* (a present one suppresses the backend's
// `Host`-based tenant lookup). See `#lib/hostRouting.ts`.

const SITES: [string, string][] = [
	['routes/invoices/+page.svelte', invoicesListSource],
	['components/modals/InvoiceModal.svelte', invoiceModalSource],
];

describe('tenant-slug header derivation (issue #170 follow-up)', () => {
	it.each(SITES)('%s never re-derives the tenant slug from the hostname', (_name, src) => {
		expect(src).not.toContain("hostname.split('.')[0]");
		expect(src).not.toContain('hostname.split(".")[0]');
	});

	it.each(SITES)('%s builds X-Tenant-Slug via getTenantSlug, or not at all', (_name, src) => {
		// Match the header as an object KEY, not any mention of it — a file that
		// routes through #lib/api names it only in a comment.
		if (!src.includes("'X-Tenant-Slug':")) return;
		expect(src).toContain("import { getTenantSlug } from '#lib/tenant.ts'");
		expect(src).toContain("'X-Tenant-Slug': getTenantSlug() ?? ''");
	});
});

// ---------------------------------------------------------------------------
// Whole-tree scans. The two named sites above are the historical offenders;
// these keep a third from appearing anywhere else.
// ---------------------------------------------------------------------------

const RAW = import.meta.glob('/src/**/*.{svelte,ts}', {
	query: '?raw',
	import: 'default',
	eager: true,
}) as Record<string, string>;

const FILES: [string, string][] = Object.entries(RAW)
	.map(([path, source]) => [path.replace(/^\/src\//, ''), source] as [string, string])
	.filter(([path]) => !path.endsWith('.test.ts'))
	.sort(([a], [b]) => a.localeCompare(b));

/** The two modules that ARE allowed to know how a hostname maps to a tenant. */
const HOST_RULE_OWNERS = new Set(['lib/tenant.ts', 'lib/hostRouting.ts']);

describe('no file re-derives a tenant slug from the hostname', () => {
	it('only #lib/hostRouting.ts splits a hostname into labels', () => {
		const offenders = FILES.filter(
			([path, src]) =>
				!HOST_RULE_OWNERS.has(path) && /\bhostname\s*\.\s*split\s*\(/.test(src)
		).map(([path]) => path);
		expect(offenders).toEqual([]);
	});
});

/**
 * Files still reading the build-time `PUBLIC_API_URL` directly instead of
 * `#lib/tenant.ts::getApiBase()`.
 *
 * `getApiBase()` resolves the API origin from the CURRENT host — the build-time
 * URL on a platform host, same-origin on a customer's vanity domain (only a
 * same-origin request carries the vanity hostname in `Host`, which is what the
 * backend resolves the tenant from). A file that keeps its own
 * `PUBLIC_API_URL.replace(/\/+$/, '')` sends its requests to the platform's own
 * API origin on every host, so it silently opts out of custom-domain support.
 *
 * **Only ever remove an entry — never add one.** Each remaining file needs the
 * same one-line swap the shared clients got; they are listed here rather than
 * left invisible.
 *
 * The list is now EMPTY, and that is the point of the ratchet: the last entry
 * was `routes/login/+page.svelte`, whose full-page navigations to the SSO /
 * SAML authorize endpoints could not move off the build-time origin while
 * those routes took the tenant as a REQUIRED `?slug=` a vanity host does not
 * have. That is fixed backend-side — `?slug=` is optional and an absent one is
 * resolved from the request `Host` against the tenant's registered custom
 * domains — so the login page now uses `getApiBase()` and omits the param.
 */
const BUILD_TIME_API_URL_BASELINE: string[] = [];

describe('API origin is resolved at runtime, not baked at build time', () => {
	it('only the baselined files still read PUBLIC_API_URL directly', () => {
		// The IMPORT, not any mention — `lib/api.ts` and `lib/hostRouting.ts`
		// name the variable in prose explaining why they no longer read it.
		const importsIt = /import\s*\{[^}]*\bPUBLIC_API_URL\b[^}]*\}\s*from\s*'\$app\/env\/public'/;
		const offenders = FILES.filter(
			([path, src]) => path !== 'lib/tenant.ts' && importsIt.test(src)
		).map(([path]) => path);
		expect(offenders.sort()).toEqual([...BUILD_TIME_API_URL_BASELINE].sort());
	});

	it('the shared HTTP clients go through getApiBase()', () => {
		for (const path of ['lib/api.ts', 'lib/portalApi.ts']) {
			const src = RAW[`/src/${path}`];
			expect(src, `${path} should exist`).toBeTruthy();
			expect(src).toContain("from '#lib/tenant.ts'");
			expect(src).toContain('getApiBase()');
		}
	});
});
