/**
 * Tenant + API-origin resolution for the current host.
 *
 * This module is the ONLY place that reads the host-routing configuration; the
 * rules themselves are dependency-free in `$lib/hostRouting.ts` (and unit-tested
 * there, via `tenant.test.ts`). Everything below is the same one-line
 * composition, so the two can't drift and no call site re-derives a slug or an
 * API origin of its own.
 *
 * Configuration: `PUBLIC_PLATFORM_DOMAINS` — a comma-separated list of the
 * registrable domains the PLATFORM serves (e.g. `feohledger.com,localhost`).
 * Read through `$env/dynamic/public` rather than `$env/static/public` on
 * purpose: a `$env/static/public` import of an unset variable is a hard
 * build error, and every existing build (CI, `deploy/deploy.sh`, the S3 +
 * CloudFront release deploy) sets only `PUBLIC_API_URL`. Unset therefore has
 * to be legal, and it means "replay the pre-change rule" — see
 * `$lib/hostRouting.ts`.
 */
import { PUBLIC_API_URL } from '$env/static/public';
import { env as publicEnv } from '$env/dynamic/public';
import {
	classifyHost,
	currentHostname,
	parsePlatformDomains,
	resolveApiBase,
	tenantSlugForHost,
	tenantStorageKeyForHost,
} from '$lib/hostRouting';

const PLATFORM_DOMAINS = parsePlatformDomains(publicEnv.PUBLIC_PLATFORM_DOMAINS);

/**
 * The `X-Tenant-Slug` header value for the current host, or `null` to send no
 * header.
 *
 * `acme.localhost:7777` → `"acme"`; `acme.feohledger.com` → `"acme"`;
 * bare `localhost` / `feohledger.com` → `null` (marketing host);
 * `ap.acmecorp.com` (a tenant's own vanity host) → `null`, so the backend
 * resolves the tenant from the `Host` header instead.
 */
export function getTenantSlug(): string | null {
	return tenantSlugForHost(currentHostname(), PLATFORM_DOMAINS);
}

/**
 * The origin every API request path is prefixed with — resolved at RUNTIME
 * from the current host, not frozen at build time.
 *
 * Platform host → the build-time `PUBLIC_API_URL`. Vanity host → `''`
 * (same origin), which is the only way the backend sees the vanity hostname in
 * the `Host` header it resolves the tenant from.
 */
export function getApiBase(): string {
	return resolveApiBase(currentHostname(), PLATFORM_DOMAINS, PUBLIC_API_URL);
}

/** True when the current host is a customer's own vanity hostname (i.e. not a
 *  platform domain or one of its subdomains). A vanity host HAS a tenant — the
 *  backend resolves it from `Host` — it just has no slug in the URL. */
export function isVanityHost(): boolean {
	return classifyHost(currentHostname(), PLATFORM_DOMAINS).kind === 'vanity';
}

/** True when the current host carries a tenant at all — a platform subdomain
 *  or a vanity host, but not the platform apex (marketing / signup). */
export function hasTenantContext(): boolean {
	const { kind } = classifyHost(currentHostname(), PLATFORM_DOMAINS);
	return kind === 'platform-tenant' || kind === 'vanity';
}

/** A stable per-tenant key for browser storage (`$lib/entity.ts`), or `null`
 *  when there is no tenant context. The slug on a platform host, the hostname
 *  on a vanity host. */
export function getTenantStorageKey(): string | null {
	return tenantStorageKeyForHost(currentHostname(), PLATFORM_DOMAINS);
}
