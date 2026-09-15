/**
 * Pure host → tenant routing rules.
 *
 * This module deliberately imports NOTHING — no `$env`, no `$app/*`, no
 * `window` — so it can be unit-tested under the node vitest environment and so
 * there is exactly ONE place that decides what a hostname means. The
 * env-bound wrappers (`getTenantSlug` / `getApiBase` / …) live in
 * `$lib/tenant.ts`, which is the only module that reads the configuration.
 *
 * ## Why a hostname needs classifying at all
 *
 * The platform serves a tenant three ways:
 *
 *   - a **platform subdomain** — `acme.feohledger.com`, `acme.localhost:7777`.
 *     The slug is in the hostname, so the SPA sends `X-Tenant-Slug: acme` and
 *     the backend resolves the tenant DB from the header.
 *   - a **platform apex** — `feohledger.com`, bare `localhost`. The marketing /
 *     signup host: no tenant at all.
 *   - a **customer vanity host** — `ap.acmecorp.com`, a white-label custom
 *     domain the tenant owns (`settings.brand.custom_domains`, see
 *     `docs/white-label.md`). There is no slug to read off the hostname: the
 *     backend maps the inbound `Host` header to the tenant itself
 *     (`backend/app/tenant.py::get_tenant_slug`), but **only when
 *     `X-Tenant-Slug` is absent**.
 *
 * The old rule ("first label of any 3+-label hostname") could not tell the
 * third case from the first: on `ap.acmecorp.com` it sent
 * `X-Tenant-Slug: ap`, and every call 404'd `Unknown tenant: ap` before the
 * backend's `Host` fallback was ever reached. Classifying against an
 * operator-declared list of platform domains is what separates them.
 *
 * ## The unconfigured default is the OLD behaviour, on purpose
 *
 * `PUBLIC_PLATFORM_DOMAINS` is baked into a static build, and every existing
 * deployment (CI, `deploy/deploy.sh`, the S3 + CloudFront release deploy)
 * builds without it. If an empty list meant "every host is a vanity host",
 * every one of those builds would stop sending `X-Tenant-Slug` overnight and
 * fall back to a `Host` map that has no entries — i.e. total breakage on
 * upgrade. So an empty list replays {@link legacyClassify}, byte-for-byte
 * the pre-change rule, and vanity-domain support switches on only when an
 * operator declares which domains are theirs.
 */

/** What a hostname is, from the SPA's point of view. */
export type HostKind =
	/** No hostname available — SSR / prerender, where `window` is undefined. */
	| 'unknown'
	/** A platform domain itself (marketing / signup host). No tenant. */
	| 'platform-apex'
	/** A subdomain of a platform domain — the slug is in the hostname. */
	| 'platform-tenant'
	/** Anything else: a customer's own vanity hostname. */
	| 'vanity';

export interface HostClassification {
	kind: HostKind;
	/** The `X-Tenant-Slug` to send, or `null` to send no header at all. */
	slug: string | null;
}

/** Lower-case, strip a trailing root dot and any `:port`, drop surrounding
 *  whitespace. `window.location.hostname` never carries a port, but a caller
 *  passing `location.host` shouldn't silently get a different answer. */
function normalizeHost(hostname: string | null | undefined): string {
	if (!hostname) return '';
	let host = hostname.trim().toLowerCase();
	const colon = host.lastIndexOf(':');
	// Skip IPv6 literals (`[::1]`), where colons are part of the address.
	if (colon !== -1 && !host.includes(']')) host = host.slice(0, colon);
	while (host.endsWith('.')) host = host.slice(0, -1);
	return host;
}

/**
 * Parse the comma-separated `PUBLIC_PLATFORM_DOMAINS` value into registrable
 * domains. Empty / unset yields `[]`, which selects the legacy rule — see the
 * module docstring.
 */
export function parsePlatformDomains(raw: string | null | undefined): string[] {
	if (!raw) return [];
	const seen = new Set<string>();
	for (const entry of raw.split(',')) {
		let domain = entry.trim().toLowerCase();
		while (domain.startsWith('.')) domain = domain.slice(1);
		while (domain.endsWith('.')) domain = domain.slice(0, -1);
		if (domain) seen.add(domain);
	}
	return [...seen];
}

/**
 * The pre-`PUBLIC_PLATFORM_DOMAINS` rule, preserved exactly: the first label of
 * any 3+-label hostname, plus the `*.localhost` two-label dev convention (the
 * local TLD has no third label to spare). A bare apex is the marketing host.
 */
function legacyClassify(host: string): HostClassification {
	const parts = host.split('.');
	const first = parts[0];
	// `first` can be empty for a malformed leading-dot host (`.example.com`);
	// an empty slug is no slug, not a header with an empty value.
	if (first && parts.length === 2 && parts[1] === 'localhost') {
		return { kind: 'platform-tenant', slug: first };
	}
	if (first && parts.length >= 3) return { kind: 'platform-tenant', slug: first };
	return { kind: 'platform-apex', slug: null };
}

/**
 * Classify a hostname against the operator-declared platform domains.
 *
 * Domains are matched longest-first, so an operator who lists both
 * `app.example.com` and `example.com` gets `acme.app.example.com` → slug
 * `acme` (under the more specific domain) rather than slug `acme` under
 * `example.com` with `app` swallowed into the prefix.
 *
 * A multi-label prefix keeps the pre-change reading — `staging.acme.example.com`
 * is slug `staging` — because that is what the old rule returned and nothing in
 * the product assigns meaning to a second label.
 *
 * An IP literal is never a platform host unless it is listed verbatim; an
 * operator serving the SPA on a bare IP should put that IP in
 * `PUBLIC_PLATFORM_DOMAINS`.
 */
export function classifyHost(
	hostname: string | null | undefined,
	platformDomains: string[]
): HostClassification {
	const host = normalizeHost(hostname);
	if (!host) return { kind: 'unknown', slug: null };
	if (platformDomains.length === 0) return legacyClassify(host);

	for (const domain of [...platformDomains].sort((a, b) => b.length - a.length)) {
		if (host === domain) return { kind: 'platform-apex', slug: null };
		if (host.endsWith(`.${domain}`)) {
			const prefix = host.slice(0, -(domain.length + 1));
			const slug = prefix.split('.')[0];
			return slug
				? { kind: 'platform-tenant', slug }
				: { kind: 'platform-apex', slug: null };
		}
	}
	return { kind: 'vanity', slug: null };
}

/**
 * The `X-Tenant-Slug` header value for a hostname, or `null` to send no header.
 *
 * `null` on a vanity host is the whole point: the header is what SUPPRESSES the
 * backend's `Host`-based tenant lookup, so sending a guess is strictly worse
 * than sending nothing.
 */
export function tenantSlugForHost(
	hostname: string | null | undefined,
	platformDomains: string[]
): string | null {
	return classifyHost(hostname, platformDomains).slug;
}

/**
 * The API origin to prefix request paths with.
 *
 * On a platform host that is the build-time `PUBLIC_API_URL` (the API lives on
 * its own hostname — `api.feohledger.com`, `localhost:8000`). On a vanity host
 * it is the empty string, i.e. **same origin**: request paths already start
 * with `/api`, and only a same-origin request carries the vanity hostname in
 * its `Host` header — which is the one thing the backend needs to resolve the
 * tenant. Pointing a vanity host at the build-time API origin would send the
 * platform's own `Host` and defeat the lookup.
 *
 * The corollary is an operator requirement, not an implementation detail: a
 * vanity host must terminate `/api` on the same origin (reverse-proxy it to
 * the backend). See `docs/white-label.md` § Custom domains.
 */
export function resolveApiBase(
	hostname: string | null | undefined,
	platformDomains: string[],
	buildTimeApiUrl: string
): string {
	const base = (buildTimeApiUrl ?? '').replace(/\/+$/, '');
	return classifyHost(hostname, platformDomains).kind === 'vanity' ? '' : base;
}

/**
 * A stable per-tenant key for browser storage, or `null` when there is no
 * tenant context.
 *
 * The slug on a platform host; the hostname itself on a vanity host (a vanity
 * hostname maps 1:1 to a tenant, so it partitions storage just as well). Keeps
 * the invariant `$lib/entity.ts` depends on: switching hosts must never carry
 * one tenant's selected entity id into another.
 */
export function tenantStorageKeyForHost(
	hostname: string | null | undefined,
	platformDomains: string[]
): string | null {
	const { kind, slug } = classifyHost(hostname, platformDomains);
	if (kind === 'platform-tenant') return slug;
	if (kind === 'vanity') return normalizeHost(hostname);
	return null;
}

/**
 * The current browser hostname, or `null` when there is no `window` (SSR /
 * prerender — the build prerenders every route, so this path is live).
 *
 * The one browser read in this module. It lives here rather than in
 * `$lib/tenant.ts` so the `typeof window === 'undefined'` guard is covered by
 * the same unit tests as the rules it feeds; `$lib/tenant.ts` is then nothing
 * but the configuration read.
 */
export function currentHostname(): string | null {
	if (typeof window === 'undefined') return null;
	return window.location.hostname;
}
