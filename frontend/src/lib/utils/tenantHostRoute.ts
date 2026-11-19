/**
 * Where the root layout sends a visitor on a workspace host — the one owner of
 * that decision.
 *
 * `/find-workspace` once made this decision a second time. On a workspace host
 * the page redirected itself to `/` from its own `onMount`, while the root
 * layout's auth wall — which runs as soon as the tenant probe resolves — sent a
 * signed-out visitor to `/login?next=/find-workspace`. Two `goto`s raced from
 * the same tick, and whichever SvelteKit settled last won: either `/` (then a
 * second hop to a bare `/login`), or a sign-in that, once completed, returned
 * the user to `/find-workspace` only for it to bounce them again. Now the page
 * makes no navigation of its own and this function answers for every path, so
 * there is exactly one navigation and it is decided once the tenant probe and
 * the stored token are both known (`hasTenant` is the readiness signal; the
 * token is read synchronously when the auth store is created).
 *
 * Pure — no `$app/*`, no store — so the node vitest config reaches it.
 */

import { withNext } from '#lib/utils/postLoginRedirect.ts';

/** "Find your workspace": a page for the apex only. */
export const FIND_WORKSPACE_PATH = '/find-workspace';

export interface TenantHostState {
	/** `page.url.pathname`. */
	path: string;
	/** `page.url.search`, carried through sign-in as `?next=`. */
	search: string;
	loggedIn: boolean;
	/** The account has been loaded (`auth.user` is set). */
	userLoaded: boolean;
	mustChangePassword: boolean;
}

export type TenantHostAction =
	| { kind: 'goto'; to: string }
	/** Signed in, account not loaded: start the load, navigate nowhere yet. */
	| { kind: 'loadUser' }
	| { kind: 'stay' };

/**
 * The action for a workspace host. Callers handle the portal and legal
 * subtrees before asking (they own their own routing).
 */
export function tenantHostRoute(s: TenantHostState): TenantHostAction {
	// A workspace host already IS a workspace: its sign-in is `/login`, and its
	// home is `/`. Never `?next=/find-workspace` — that would only send the user
	// back here after signing in.
	if (s.path === FIND_WORKSPACE_PATH) {
		return { kind: 'goto', to: s.loggedIn ? '/' : '/login' };
	}

	if (!s.loggedIn && !s.path.startsWith('/login')) {
		return { kind: 'goto', to: withNext('/login', s.path + s.search) };
	}

	if (s.loggedIn && !s.userLoaded) return { kind: 'loadUser' };

	if (s.loggedIn && s.mustChangePassword && s.path !== '/change-password') {
		return { kind: 'goto', to: withNext('/change-password', s.path + s.search) };
	}

	return { kind: 'stay' };
}
