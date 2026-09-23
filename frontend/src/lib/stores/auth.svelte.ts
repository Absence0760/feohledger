import { api, setToken, clearToken, hasToken } from '$lib/api';
import { performAuthentication, performRegistration } from '$lib/webauthn';

export interface Passkey {
	id: string;
	name: string;
	transports: string | null;
	created_at: string | null;
	last_used_at: string | null;
}

/** The factor-management actions a step-up can authorize. The server binds an
 * assertion to exactly one of these, so the value here is load-bearing — it
 * must match the operation of the call the proof is then sent with. */
export type StepUpOperation =
	| 'totp_enroll'
	| 'totp_disable'
	| 'passkey_register'
	| 'passkey_delete';

/** Re-proof of account control, sent with any change to an existing second
 * factor. Any ONE of the three satisfies the server: the account password, a
 * code from the current authenticator, or a passkey assertion. */
export interface StepUpProof {
	password?: string;
	code?: string;
	assertion?: unknown;
}

/** One live sign-in on this account. `id` is the token's session handle — the
 * only thing the revoke endpoint accepts — and `current` marks the session
 * making the request, which the UI must never offer up for casual removal.
 * `ip` / `device` / `method` are best-effort and may be null. */
export interface ActiveSession {
	id: string;
	created_at: string;
	expires_at: string;
	ip: string | null;
	device: string | null;
	method: string | null;
	current: boolean;
}

interface User {
	id: string;
	email: string;
	full_name: string;
	organization_id: string;
	is_active: boolean;
	must_change_password: boolean;
	mfa_enabled: boolean;
	mfa_required_by_org: boolean;
	// Does THIS account have a password at all? False for every account
	// JIT-provisioned by OIDC/SAML or created by SCIM — `User.hashed_password
	// is not None` on the server. Independent of `password_sign_in_closed`
	// below, which is a property of the ORG, not the account: a member can
	// have no password in an org that has not closed password sign-in.
	// `/profile` reads it to replace the Change-password form, which such an
	// account could never submit, with a sentence.
	has_password: boolean;
	// The org has closed password sign-in (`sso_only`), so the server refuses
	// the password as a step-up proof too. The server's own predicate — the
	// one login and the step-up call — so `/profile` can stop offering a
	// password field exactly where it would be refused (docs/decisions.md §201).
	password_sign_in_closed: boolean;
	roles: string[];
	// Effective granular permissions (the union over the user's roles), from
	// GET /api/auth/me. Drives `can(perm)` for the split sensitive controls.
	// Older tokens / responses may omit it — treated as "no granular perms".
	permissions?: string[];
}

interface TokenResponse {
	access_token: string;
	token_type: string;
	must_change_password?: boolean;
}

export interface MFAChallenge {
	mfa_required: true;
	mfa_challenge_token: string;
	methods: string[]; // "totp" | "passkey" | "email"
	must_enroll: boolean;
}

type LoginResult =
	| { kind: 'ok' }
	| { kind: 'mfa'; challenge: MFAChallenge };

function createAuthStore() {
	let user = $state<User | null>(null);
	let loggedIn = $state(hasToken());

	async function login(email: string, password: string): Promise<LoginResult> {
		const res = await api.post<TokenResponse | MFAChallenge>('/api/auth/login', {
			email,
			password,
		});
		// MFA challenge — caller routes to the verify page
		if ('mfa_required' in res && res.mfa_required) {
			return { kind: 'mfa', challenge: res };
		}
		const tok = res as TokenResponse;
		setToken(tok.access_token);
		loggedIn = true;
		await fetchUser();
		return { kind: 'ok' };
	}

	async function completeMfa(challengeToken: string, code: string, method: 'totp' | 'email') {
		const res = await api.post<TokenResponse>('/api/auth/mfa/verify', {
			challenge_token: challengeToken,
			code,
			method,
		});
		setToken(res.access_token);
		loggedIn = true;
		await fetchUser();
	}

	async function requestEmailMfa(challengeToken: string) {
		await api.post('/api/auth/mfa/challenge/email', { challenge_token: challengeToken });
	}

	// --- WebAuthn / passkey LOGIN (the passkey factor of the MFA step) -----

	async function completePasskey(challengeToken: string) {
		// 1. Ask the server for an authentication challenge scoped to this user.
		const start = await api.post<{ options: any }>('/api/auth/mfa/passkey/authenticate', {
			challenge_token: challengeToken,
		});
		// 2. Run the browser ceremony (prompts Touch ID / security key).
		const credential = await performAuthentication(start.options);
		// 3. Hand the signed assertion back for verification + a real token.
		const res = await api.post<TokenResponse>('/api/auth/mfa/passkey/authenticate/verify', {
			challenge_token: challengeToken,
			credential,
		});
		setToken(res.access_token);
		loggedIn = true;
		await fetchUser();
	}

	// --- WebAuthn / passkey ENROLLMENT (authenticated, on the profile) -----

	async function listPasskeys(): Promise<Passkey[]> {
		return api.get<Passkey[]>('/api/auth/mfa/passkey');
	}

	/**
	 * Re-prove account control with a passkey the account already has, so it
	 * can change its factors. This is the ONLY step-up an SSO-only account can
	 * offer — it has no password to re-type and no authenticator code.
	 *
	 * Deliberately the same browser code as the passkey LOGIN ceremony
	 * (`performAuthentication`); only the challenge differs. The server mints
	 * that challenge bound to `operation`, so the returned proof authorizes
	 * that action and nothing else — pass it to the matching call.
	 */
	async function passkeyStepUp(operation: StepUpOperation): Promise<StepUpProof> {
		const start = await api.post<{ options: any }>('/api/auth/mfa/step-up/passkey', {
			operation,
		});
		return { assertion: await performAuthentication(start.options) };
	}

	/**
	 * Register a passkey. `stepUp` re-proves control of the account — the
	 * backend requires it whenever a second factor is ALREADY in force, so a
	 * stolen session can't quietly bind an attacker's authenticator. The
	 * account password, a code from the current authenticator, or an assertion
	 * from `passkeyStepUp('passkey_register')` all work; omit it for the first
	 * factor on an account that has none.
	 */
	async function registerPasskey(
		name: string,
		stepUp: StepUpProof = {},
	): Promise<Passkey> {
		const start = await api.post<{ options: any }>('/api/auth/mfa/passkey/register', stepUp);
		const credential = await performRegistration(start.options);
		const saved = await api.post<Passkey>('/api/auth/mfa/passkey/register/verify', {
			credential,
			name,
		});
		// A new factor may change the displayed MFA state — refresh the user.
		await fetchUser();
		return saved;
	}

	/**
	 * Remove a passkey. Deleting a factor is a step-up operation for the same
	 * reason adding one is — a stolen session must not be able to strip the
	 * account's second factor — so the backend always requires `stepUp` here
	 * (the passkey being deleted is itself a live factor). Credentials go in
	 * the request BODY, never the URL.
	 */
	async function deletePasskey(id: string, stepUp: StepUpProof = {}): Promise<void> {
		await api.delete(`/api/auth/mfa/passkey/${id}`, stepUp);
		await fetchUser();
	}

	// --- Active sessions ("where you're signed in") -----------------------
	//
	// The remedy for a token you believe is compromised. All three are scoped
	// server-side to the calling account, so there is nothing to pass but the
	// session handle.

	async function listSessions(): Promise<ActiveSession[]> {
		return api.get<ActiveSession[]>('/api/auth/sessions');
	}

	async function revokeSession(id: string): Promise<void> {
		await api.delete(`/api/auth/sessions/${id}`);
	}

	/** Sign out everywhere except this device. Returns how many ended. */
	async function revokeOtherSessions(): Promise<number> {
		const res = await api.post<{ revoked: number }>('/api/auth/sessions/revoke-others', {});
		return res.revoked;
	}

	async function fetchUser() {
		try {
			user = await api.get<User>('/api/auth/me');
			// A successful /me means the token is good — keep `loggedIn` in sync.
			// The SSO callback (and any flow that obtains a token without going
			// through login()/completeMfa()) relies on this to flip the flag,
			// otherwise the layout sees `!loggedIn` and bounces back to /login.
			loggedIn = true;
		} catch {
			user = null;
		}
	}

	async function logout() {
		try {
			await api.post('/api/auth/logout', {});
		} catch {
			// Proceed with client-side logout even if server call fails
		}
		clearToken();
		user = null;
		loggedIn = false;
		window.location.href = '/login';
	}

	function hasRole(role: string): boolean {
		return user?.roles.includes(role) ?? false;
	}

	function hasAnyRole(...roles: string[]): boolean {
		return roles.some((r) => hasRole(r));
	}

	/** True if the user's effective granular permissions include `perm`.
	 * Mirrors the backend `require_permission(...)` gate so the gated UI
	 * control and the API gate can't drift. Use this for the split sensitive
	 * actions (payment execute/void, run approve, vendor bank-change approve,
	 * vendor block/unblock, user management); keep `isManager`/`isCfo` for
	 * everything still on `require_roles`. */
	function can(perm: string): boolean {
		return user?.permissions?.includes(perm) ?? false;
	}

	return {
		get user() { return user; },
		get loggedIn() { return loggedIn; },
		get isAdmin() { return hasRole('admin'); },
		get isManager() { return hasAnyRole('admin', 'ap_manager'); },
		get isCfo() { return hasAnyRole('admin', 'cfo'); },
		get isClerkOnly() { return user?.roles.length === 1 && hasRole('ap_clerk'); },
		login,
		completeMfa,
		requestEmailMfa,
		completePasskey,
		listPasskeys,
		passkeyStepUp,
		registerPasskey,
		deletePasskey,
		listSessions,
		revokeSession,
		revokeOtherSessions,
		logout,
		fetchUser,
		hasRole,
		hasAnyRole,
		can,
	};
}

export const auth = createAuthStore();
