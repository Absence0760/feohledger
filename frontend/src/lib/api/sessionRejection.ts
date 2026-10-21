/**
 * Which refused responses end the signed-in session on this page.
 *
 * Two do: a 401 (the token expired or was revoked), and a 403 coded
 * `token_tenant_mismatch` — the backend's `tenant.get_tenant` refusing a token
 * minted for a DIFFERENT workspace than the one this page belongs to. The
 * second is not a permission problem the user can do anything about while
 * signed in: every tenant-scoped call on the page answers the same way, so
 * before this the shell sat on "Loading…" behind a "Failed to load
 * organization" toast (production, 2026-10-20; `docs/decisions.md` §288).
 * Treated like an expired session instead — token cleared, back to this
 * host's `/login`, which says why.
 *
 * Keyed on the refusal's CODE, never its English message. Pure (imports no
 * `$app/*`), so the node vitest config reaches it; `api.ts` owns the side
 * effects.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { withNext } from '#lib/utils/postLoginRedirect.ts';

/** `backend/app/api/refusals.py::TOKEN_TENANT_MISMATCH`. */
export const TOKEN_TENANT_MISMATCH = 'token_tenant_mismatch';

export type SessionRejection = 'expired' | 'other_workspace';

/** The query parameter `/login` reads to say why the user is there. */
export const LOGIN_REASON_PARAM = 'reason';

const REASON_OTHER_WORKSPACE = 'other_workspace';
const REASON_WORKSPACE_CLOSED = 'workspace_closed';

/** Where an administrator lands after closing their workspace
 *  (`components/organization/CloseWorkspacePanel.svelte`): this host's sign-in
 *  page, which says the workspace is gone and that its addresses are free. */
export const WORKSPACE_CLOSED_LOGIN_PATH = `/login?${LOGIN_REASON_PARAM}=${REASON_WORKSPACE_CLOSED}`;

/** Whether a refused response ends the session, and why; `null` when it
 *  does not (any other 403, a 404, a 409…). */
export function sessionRejection(status: number, code: string | null): SessionRejection | null {
	if (status === 401) return 'expired';
	if (status === 403 && code === TOKEN_TENANT_MISMATCH) return 'other_workspace';
	return null;
}

/** The part of `fetch` {@link revokeRejectedSession} uses — injected so the
 *  node vitest config can drive it without a network. */
export type LogoutFetch = (url: string, init: RequestInit) => Promise<unknown>;

/**
 * Best-effort revocation, on the server, of a session this page is ending.
 *
 * Only for `other_workspace`. That token was refused HERE because it belongs to
 * another workspace, but it is still a live session there until it expires,
 * and still listed under that account's sessions — on a shared machine,
 * clearing it from this origin's storage alone left a usable credential behind.
 * `POST /api/auth/logout` needs no workspace: it reads the token's own `jti`,
 * `sub` and `org`, blocklists it and audits `auth.logout` in the token's own
 * workspace, so it is sent with the token and deliberately WITHOUT this page's
 * `X-Tenant-Slug` — the header that caused the refusal. An expired session
 * (`401`) is not revoked: the server already refuses it, and logout would 401.
 *
 * `keepalive`, because the caller navigates away on the next line; and every
 * failure is swallowed, because the user is being signed out of this page
 * either way and nothing they could do would change the outcome.
 */
export function revokeRejectedSession(
	rejection: SessionRejection,
	token: string | null,
	apiBase: string,
	fetchImpl: LogoutFetch
): void {
	if (rejection !== 'other_workspace' || !token) return;
	try {
		void Promise.resolve(
			fetchImpl(`${apiBase}/api/auth/logout`, {
				method: 'POST',
				headers: { Authorization: `Bearer ${token}` },
				keepalive: true
			})
		).catch(() => undefined);
	} catch {
		// A fetch that throws synchronously (no network stack) — same as a failure.
	}
}

/** Where to send the user once the session has been cleared: `/login`, with
 *  the reason for `other_workspace`, and `next` — the page they were on, so
 *  signing in again returns them there (`#lib/utils/postLoginRedirect.ts`,
 *  which drops anything that is not a safe same-origin path). */
export function loginPathFor(rejection: SessionRejection, next?: string | null): string {
	const base =
		rejection === 'other_workspace'
			? `/login?${LOGIN_REASON_PARAM}=${REASON_OTHER_WORKSPACE}`
			: '/login';
	return withNext(base, next);
}

/** The notice `/login` shows for the reason in its URL, or `null`. Only a
 *  reason this module set is honoured — the parameter is not free text. */
export function loginNoticeKey(params: Pick<URLSearchParams, 'get'>): MessageKey | null {
	switch (params.get(LOGIN_REASON_PARAM)) {
		case REASON_OTHER_WORKSPACE:
			return 'auth.login.signedOutOtherWorkspace';
		case REASON_WORKSPACE_CLOSED:
			return 'auth.login.workspaceClosed';
		default:
			return null;
	}
}

/** The account's own sign-in page from a `wrong_workspace` refusal's params,
 *  when it is an http(s) URL — it becomes a link's `href`, so nothing else
 *  (a `javascript:` URL from a compromised response) is rendered. */
export function ownWorkspaceSignInUrl(params: Record<string, unknown>): string | null {
	const raw = params.sign_in_url;
	if (typeof raw !== 'string' || !raw) return null;
	try {
		const url = new URL(raw);
		return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
	} catch {
		return null;
	}
}
