import { beforeEach, describe, expect, it, vi } from 'vitest';

// The `userStatus` state machine of `fetchUser` / `logout`, driven against a
// stubbed api client. `#lib/api.ts` reaches `$app/env/public` through
// `#lib/tenant.ts`, which only SvelteKit resolves, so it is replaced with the
// seams the store uses: `api.get` / `api.post`, `ApiError`, and a token held
// in a variable instead of localStorage. A 401 really clears the token inside
// `api.ts::endSessionIfRejected` before the promise rejects, so the stub does
// the same where a test needs it.
let token: string | null = null;
const get = vi.fn();
const post = vi.fn();

vi.mock('#lib/api.ts', () => {
	class ApiError extends Error {
		status: number;
		code: string | null;
		constructor(message: string, status: number, code: string | null = null) {
			super(message);
			this.status = status;
			this.code = code;
		}
	}
	return {
		ApiError,
		api: {
			get: (...args: unknown[]) => get(...args),
			post: (...args: unknown[]) => post(...args)
		},
		hasToken: () => token !== null,
		setToken: (t: string) => {
			token = t;
		},
		clearToken: () => {
			token = null;
		}
	};
});
vi.mock('#lib/utils/postLoginRedirect.ts', () => ({
	rememberNextForSso: vi.fn(),
	withNext: (path: string) => path
}));
vi.mock('#lib/webauthn.ts', () => ({
	performAuthentication: vi.fn(),
	performRegistration: vi.fn()
}));

const { ApiError } = await import('#lib/api.ts');

const ALICE = { id: 'u-1', email: 'alice@acme.example', full_name: 'Alice', roles: ['admin'] };
const BOB = { id: 'u-2', email: 'bob@acme.example', full_name: 'Bob', roles: ['ap_clerk'] };

/** A `/me` the test settles when it chooses. The settle functions are read
 * through the returned object, because they exist only once the store has
 * actually called `api.get` (the Promise executor runs then, not here). */
function pendingMe() {
	const settle = {
		resolve: (_user: unknown): void => {
			throw new Error('/me was never requested');
		},
		reject: (_err: unknown): void => {
			throw new Error('/me was never requested');
		}
	};
	get.mockImplementationOnce(
		() =>
			new Promise((res, rej) => {
				settle.resolve = res;
				settle.reject = rej;
			})
	);
	return {
		resolve: (user: unknown) => settle.resolve(user),
		reject: (err: unknown) => settle.reject(err)
	};
}

/** A fresh store singleton, as if the page had just loaded holding `withToken`. */
async function freshStore(withToken: string | null) {
	token = withToken;
	vi.resetModules();
	const { auth } = await import('./auth.svelte.ts');
	return auth;
}

describe('auth.fetchUser — userStatus', () => {
	beforeEach(() => {
		get.mockReset();
		post.mockReset();
		vi.stubGlobal('window', { location: { href: '' } });
	});

	it('starts idle, goes loading while /me is in flight, then ready', async () => {
		const auth = await freshStore('tok');
		expect(auth.userStatus).toBe('idle');
		expect(auth.loggedIn).toBe(true);

		const me = pendingMe();
		const load = auth.fetchUser();
		expect(auth.userStatus).toBe('loading');

		me.resolve(ALICE);
		await load;
		expect(auth.userStatus).toBe('ready');
		expect(auth.user?.email).toBe(ALICE.email);
	});

	it('a 401 that ended the session stays loading while api.ts navigates to /login', async () => {
		const auth = await freshStore('tok');
		get.mockImplementationOnce(async () => {
			// What `endSessionIfRejected` does before the rejection reaches us.
			token = null;
			throw new ApiError('Session expired', 401);
		});

		await auth.fetchUser();

		expect(token).toBeNull();
		expect(auth.user).toBeNull();
		// Not `error`: an error panel would flash and race the navigation.
		expect(auth.userStatus).toBe('loading');
	});

	it('a 403 token-for-another-workspace is treated as a session that ended', async () => {
		const auth = await freshStore('tok');
		get.mockImplementationOnce(async () => {
			token = null;
			throw new ApiError('Wrong workspace', 403, 'token_tenant_mismatch');
		});

		await auth.fetchUser();

		expect(auth.user).toBeNull();
		expect(auth.userStatus).toBe('loading');
	});

	it('with no token at all, a refused /me is a signed-out visitor: idle, not logged in', async () => {
		const auth = await freshStore(null);
		expect(auth.loggedIn).toBe(false);
		get.mockRejectedValueOnce(new ApiError('Not authenticated', 401));

		await auth.fetchUser();

		expect(auth.user).toBeNull();
		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});

	it('a token cleared by another tab while /me was in flight signs this page out too', async () => {
		const auth = await freshStore('tok');
		const me = pendingMe();
		const load = auth.fetchUser();

		token = null; // another tab signed out
		me.reject(new ApiError('Service unavailable', 503));
		await load;

		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});

	it('a 5xx with no account loaded yet is an error the layout offers Retry for', async () => {
		const auth = await freshStore('tok');
		get.mockRejectedValueOnce(new ApiError('Bad gateway', 502));

		await auth.fetchUser();

		expect(auth.user).toBeNull();
		// Still a signed-in session: a valid token must not bounce to /login.
		expect(auth.loggedIn).toBe(true);
		expect(token).toBe('tok');
		expect(auth.userStatus).toBe('error');
	});

	it('a network failure (not an ApiError) behaves like a 5xx', async () => {
		const auth = await freshStore('tok');
		get.mockRejectedValueOnce(new TypeError('Failed to fetch'));

		await auth.fetchUser();

		expect(auth.loggedIn).toBe(true);
		expect(auth.userStatus).toBe('error');
	});

	it('a 5xx while refreshing an already-loaded account keeps it, and stays ready', async () => {
		const auth = await freshStore('tok');
		get.mockResolvedValueOnce(ALICE);
		await auth.fetchUser();
		expect(auth.userStatus).toBe('ready');

		get.mockRejectedValueOnce(new ApiError('Internal error', 500));
		await auth.fetchUser();

		expect(auth.user?.email).toBe(ALICE.email);
		expect(auth.userStatus).toBe('ready');
	});

	it('an older /me answering after a newer one is ignored', async () => {
		const auth = await freshStore('tok');
		const older = pendingMe();
		const first = auth.fetchUser();
		const newer = pendingMe();
		const second = auth.fetchUser();

		newer.resolve(BOB);
		await second;
		expect(auth.user?.email).toBe(BOB.email);

		older.resolve(ALICE);
		await first;
		expect(auth.user?.email).toBe(BOB.email);
		expect(auth.userStatus).toBe('ready');
	});

	it('an older /me failing after a newer one succeeded does not turn it into an error', async () => {
		const auth = await freshStore('tok');
		const older = pendingMe();
		const first = auth.fetchUser();
		const newer = pendingMe();
		const second = auth.fetchUser();

		newer.resolve(BOB);
		await second;

		older.reject(new ApiError('Bad gateway', 502));
		await first;
		expect(auth.user?.email).toBe(BOB.email);
		expect(auth.userStatus).toBe('ready');
	});

	it('the newer load stays loading while only the older one has answered', async () => {
		const auth = await freshStore('tok');
		const older = pendingMe();
		const first = auth.fetchUser();
		pendingMe();
		void auth.fetchUser();

		older.resolve(ALICE);
		await first;
		expect(auth.user).toBeNull();
		expect(auth.userStatus).toBe('loading');
	});
});

describe('auth.logout during an in-flight /me', () => {
	beforeEach(() => {
		get.mockReset();
		post.mockReset();
		vi.stubGlobal('window', { location: { href: '' } });
	});

	it('a /me answering after logout does not sign the page back in', async () => {
		const auth = await freshStore('tok');
		post.mockResolvedValueOnce({});
		const me = pendingMe();
		const load = auth.fetchUser();

		await auth.logout();
		expect(post).toHaveBeenCalledWith('/api/auth/logout', {});
		expect(token).toBeNull();
		expect(window.location.href).toBe('/login');

		me.resolve(ALICE);
		await load;

		expect(auth.user).toBeNull();
		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});

	it('a /me failing after logout leaves the signed-out state, not an error', async () => {
		const auth = await freshStore('tok');
		post.mockResolvedValueOnce({});
		const me = pendingMe();
		const load = auth.fetchUser();

		await auth.logout();
		me.reject(new ApiError('Bad gateway', 502));
		await load;

		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});

	it('a /me answering while the logout request itself is in flight is undone by logout', async () => {
		const auth = await freshStore('tok');
		let finishLogout!: () => void;
		post.mockImplementationOnce(() => new Promise<void>((res) => (finishLogout = res)));
		const me = pendingMe();
		const load = auth.fetchUser();

		const out = auth.logout();
		me.resolve(ALICE);
		await load;
		finishLogout();
		await out;

		expect(auth.user).toBeNull();
		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});

	it('logout still signs out locally when the server call fails', async () => {
		const auth = await freshStore('tok');
		get.mockResolvedValueOnce(ALICE);
		await auth.fetchUser();
		post.mockRejectedValueOnce(new ApiError('Bad gateway', 502));

		await auth.logout();

		expect(token).toBeNull();
		expect(auth.user).toBeNull();
		expect(auth.loggedIn).toBe(false);
		expect(auth.userStatus).toBe('idle');
	});
});
