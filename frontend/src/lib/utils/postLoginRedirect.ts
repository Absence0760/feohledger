/**
 * Where a sign-in returns the user to: the page they were trying to open.
 *
 * Every route that sends someone to `/login` because they are not (or no
 * longer) signed in puts the path they were on in `?next=` — the root layout's
 * auth wall, and `api.ts`'s session-ending bounce. The value is then carried
 * through each step a sign-in can take: `/login` → `/login/mfa` → the forced
 * `/change-password` step, each passing it on in its own `?next=`. The IdP
 * round trip of OIDC / SAML cannot carry a parameter of ours, so `/login`
 * stashes it in `sessionStorage` before leaving and the callback page takes it
 * back ({@link rememberNextForSso} / {@link takeNextForSso}).
 *
 * The value comes from the URL, so anyone can write a link that sets it. It is
 * honoured ONLY as a same-origin relative path — {@link safeNextPath} is the one
 * gate, and every reader goes through it — so a crafted link can never turn the
 * sign-in page into an open redirect. Pure apart from the injected storage, so
 * the node vitest config reaches all of it.
 */

/** The query parameter that carries the destination. */
export const NEXT_PARAM = 'next';

/** `sessionStorage` key holding the destination across an SSO / SAML round trip. */
export const SSO_NEXT_KEY = 'auth_next';

/** A URL is never this long legitimately; a longer one is refused outright. */
const MAX_NEXT_LENGTH = 2048;

/** Any backslash (browsers read `\` as `/` in a path, so `/\evil.example` is a
 *  protocol-relative URL) or C0 control / DEL (the URL parser strips tab and
 *  newline, so `/\t/evil.example` would be one too). */
const UNSAFE_CHARS = /[\\\u0000-\u001f\u007f]/;

const PROBE_ORIGIN = 'https://next.invalid';

/**
 * `raw` when it is a same-origin path that is safe to navigate to after
 * sign-in, otherwise `null`.
 *
 * Accepted: a string starting with exactly one `/`, with no scheme, no host, no
 * backslash and no control character, that resolves to the same origin it was
 * resolved against. Refused besides: anything under `/login` (it would send a
 * freshly signed-in user straight back to a sign-in form) and `/` itself
 * (the default destination; carrying it is noise).
 */
export function safeNextPath(raw: unknown): string | null {
	if (typeof raw !== 'string') return null;
	if (raw.length < 2 || raw.length > MAX_NEXT_LENGTH) return null;
	if (raw[0] !== '/' || raw[1] === '/') return null;
	if (UNSAFE_CHARS.test(raw)) return null;
	let url: URL;
	try {
		url = new URL(raw, PROBE_ORIGIN);
	} catch {
		return null;
	}
	if (url.origin !== PROBE_ORIGIN) return null;
	// Dot segments can collapse to a leading `//` (`/.//evil.example`); the
	// resolved path must not start like a protocol-relative URL either.
	if (url.pathname.startsWith('//')) return null;
	let decoded: string;
	try {
		decoded = decodeURIComponent(url.pathname).toLowerCase();
	} catch {
		return null;
	}
	if (decoded === '/login' || decoded.startsWith('/login/')) return null;
	if (url.pathname === '/' && !url.search && !url.hash) return null;
	return raw;
}

/** The validated destination a page's own URL carries, or `null`. */
export function nextFromParams(params: Pick<URLSearchParams, 'get'>): string | null {
	return safeNextPath(params.get(NEXT_PARAM));
}

/** Where to go once signed in: the validated destination, else the dashboard. */
export function postLoginTarget(next: string | null | undefined): string {
	return safeNextPath(next) ?? '/';
}

/**
 * `path` with `next` appended as `?next=` (or `&next=` when `path` already has
 * a query). An unsafe or absent `next` adds nothing, so a caller can pass
 * whatever it holds without checking it first.
 */
export function withNext(path: string, next: string | null | undefined): string {
	const safe = safeNextPath(next);
	if (!safe) return path;
	const sep = path.includes('?') ? '&' : '?';
	return `${path}${sep}${new URLSearchParams({ [NEXT_PARAM]: safe }).toString()}`;
}

/** The part of `Storage` the SSO stash uses — injected for the node tests. */
export type NextStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

function defaultStorage(): NextStorage | null {
	try {
		return typeof sessionStorage === 'undefined' ? null : sessionStorage;
	} catch {
		// Storage blocked (privacy mode, sandboxed frame): the sign-in still
		// works, it just lands on the dashboard.
		return null;
	}
}

/**
 * Stash the destination before leaving for the IdP. Always writes or clears,
 * so an abandoned earlier attempt in this tab can never steer a later one.
 * Best-effort: blocked storage only costs the redirect.
 */
export function rememberNextForSso(
	next: string | null | undefined,
	storage: NextStorage | null = defaultStorage()
): void {
	if (!storage) return;
	const safe = safeNextPath(next);
	try {
		if (safe) storage.setItem(SSO_NEXT_KEY, safe);
		else storage.removeItem(SSO_NEXT_KEY);
	} catch {
		// Quota / blocked storage — fall back to the dashboard.
	}
}

/** Read and clear the stashed destination (validated again on the way out —
 *  storage is writable by any script on the origin). */
export function takeNextForSso(storage: NextStorage | null = defaultStorage()): string | null {
	if (!storage) return null;
	try {
		const raw = storage.getItem(SSO_NEXT_KEY);
		storage.removeItem(SSO_NEXT_KEY);
		return safeNextPath(raw);
	} catch {
		return null;
	}
}
