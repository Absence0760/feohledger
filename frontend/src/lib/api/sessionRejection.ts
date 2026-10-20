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

/** `backend/app/api/refusals.py::TOKEN_TENANT_MISMATCH`. */
export const TOKEN_TENANT_MISMATCH = 'token_tenant_mismatch';

export type SessionRejection = 'expired' | 'other_workspace';

/** The query parameter `/login` reads to say why the user is there. */
export const LOGIN_REASON_PARAM = 'reason';

const REASON_OTHER_WORKSPACE = 'other_workspace';

/** Whether a refused response ends the session, and why; `null` when it
 *  does not (any other 403, a 404, a 409…). */
export function sessionRejection(status: number, code: string | null): SessionRejection | null {
	if (status === 401) return 'expired';
	if (status === 403 && code === TOKEN_TENANT_MISMATCH) return 'other_workspace';
	return null;
}

/** Where to send the user once the session has been cleared. An expired
 *  session is the plain `/login` it always was. */
export function loginPathFor(rejection: SessionRejection): string {
	return rejection === 'other_workspace'
		? `/login?${LOGIN_REASON_PARAM}=${REASON_OTHER_WORKSPACE}`
		: '/login';
}

/** The notice `/login` shows for the reason in its URL, or `null`. Only a
 *  reason this module set is honoured — the parameter is not free text. */
export function loginNoticeKey(params: Pick<URLSearchParams, 'get'>): MessageKey | null {
	return params.get(LOGIN_REASON_PARAM) === REASON_OTHER_WORKSPACE
		? 'auth.login.signedOutOtherWorkspace'
		: null;
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
