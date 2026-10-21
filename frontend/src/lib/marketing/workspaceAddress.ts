/**
 * "Find your workspace" on the platform apex: turn what a returning customer
 * types into the sign-in page of their own workspace (`/find-workspace`,
 * issue #635 finding 4).
 *
 * **The visitor names the workspace; nothing is looked up.** There is no
 * email → workspace search, and there must not be one: an endpoint that answers
 * "which workspace is this address in" tells anyone who asks whether a given
 * person is a customer, and at which company — an account-enumeration oracle on
 * an unauthenticated page. The address is in the welcome email and in every
 * notification the workspace sends, so the visitor already holds it; this only
 * spares them typing the whole URL. A slug that names no workspace lands on
 * that host's sign-in page, which says so the same way it does for anyone.
 *
 * Pure — no `$app` import — so vitest reaches it and the page stays thin.
 */

/**
 * The format half of `backend/app/utils/slug.py::validate_slug_format`
 * (`^[a-z][a-z0-9-]{2,29}$`, no trailing or doubled hyphen). The reserved-word
 * list is deliberately not copied: no workspace can hold a reserved slug, so
 * typing one simply finds nothing, which is what any unknown slug does.
 */
export type WorkspaceAddressProblem =
	| 'empty'
	| 'startLetter'
	| 'characters'
	| 'tooShort'
	| 'tooLong'
	| 'endHyphen'
	| 'doubleHyphen';

const SLUG_MIN = 3;
const SLUG_MAX = 30;

/**
 * The host part a template puts after the slug — `https://{slug}.feohledger.com`
 * gives `.feohledger.com`, the dev default `http://{slug}.localhost:7777` gives
 * `.localhost:7777`. `null` for a template that is not exactly one `{slug}`
 * followed by a host, which nothing here can build an address from.
 */
export function tenantHostSuffix(template: string | null | undefined): string | null {
	const raw = (template ?? '').trim();
	const noScheme = raw.replace(/^https?:\/\//i, '');
	if (noScheme === raw) return null;
	const parts = noScheme.split('{slug}');
	if (parts.length !== 2 || parts[0] !== '') return null;
	const suffix = parts[1].replace(/\/+$/, '');
	// A path after the host would make the address something other than a host.
	if (!suffix || suffix.includes('/')) return null;
	return suffix;
}

/**
 * What the visitor typed, reduced to the slug they meant: trimmed and
 * lowercased, with a pasted scheme, path, and the platform's own host suffix
 * taken off — so `acme`, `Acme`, `acme.feohledger.com` and
 * `https://acme.feohledger.com/invoices` all give `acme`. Anything else is
 * left as typed for {@link workspaceAddressProblem} to refuse, rather than
 * guessed at: `ap.acmecorp.com` is a customer's own domain, and its first
 * label is not a slug.
 */
export function normalizeWorkspaceInput(raw: string, suffix: string | null): string {
	let value = raw.trim().toLowerCase();
	value = value.replace(/^[a-z][a-z0-9+.-]*:\/\//, '');
	value = value.split(/[/?#]/, 1)[0];
	const host = suffix?.toLowerCase();
	if (host && value.endsWith(host) && value.length > host.length) {
		value = value.slice(0, -host.length);
	}
	return value;
}

/**
 * The host the visitor typed when it is a whole domain that is not the
 * platform's — `ap.acmecorp.com`, a workspace on its own custom domain
 * (`docs/white-label.md` § Custom domains), whose emails carry that address.
 * `null` for anything else. The page offers it as a link rather than refusing
 * it as a malformed slug; it is never followed on its own, and nothing but
 * what the visitor typed goes into it.
 */
export function ownDomainHost(normalized: string, suffix: string | null): string | null {
	if (!/^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::\d{1,5})?$/.test(normalized)) {
		return null;
	}
	const platform = suffix?.toLowerCase().replace(/^\./, '');
	if (platform && (normalized === platform || normalized.endsWith(`.${platform}`))) return null;
	return normalized;
}

/** Why `slug` cannot be a workspace address, or `null` when it can. */
export function workspaceAddressProblem(slug: string): WorkspaceAddressProblem | null {
	if (!slug) return 'empty';
	if (!/^[a-z]/.test(slug)) return 'startLetter';
	if (!/^[a-z0-9-]+$/.test(slug)) return 'characters';
	if (slug.length < SLUG_MIN) return 'tooShort';
	if (slug.length > SLUG_MAX) return 'tooLong';
	if (slug.endsWith('-')) return 'endHyphen';
	if (slug.includes('--')) return 'doubleHyphen';
	return null;
}

/**
 * The sign-in page of workspace `slug` under the deployment's
 * `tenant_url_template` (`GET /api/public-config`), or `null` when the slug is
 * not a valid address or the template cannot produce an http(s) URL. Never a
 * guess from the current hostname: the template is the backend's statement of
 * where workspaces live, and a wrong guess would send a customer's sign-in to
 * a host that is not ours.
 */
export function workspaceLoginUrl(slug: string, template: string | null | undefined): string | null {
	if (workspaceAddressProblem(slug) !== null) return null;
	if (tenantHostSuffix(template) === null) return null;
	const base = (template ?? '').trim().replace('{slug}', slug).replace(/\/+$/, '');
	try {
		const url = new URL(`${base}/login`);
		if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
		return url.toString();
	} catch {
		return null;
	}
}
