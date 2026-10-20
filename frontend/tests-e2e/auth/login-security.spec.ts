import { API_BASE, currentTenantSlug, expect, signIn, test } from '../fixtures/helpers';

// Start unauthenticated — this spec tests the login surface.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Login security — the contract is "the only way past this gate is a
 * correct (email, password) pair against an active user in the
 * tenant." The tests below pin a few security-relevant edge cases.
 *
 * What's covered:
 *   - Failed attempts don't return a token (no silent success).
 *   - Wrong-password and unknown-email return the same shape so an
 *     attacker can't enumerate valid emails by response diff.
 *   - Successive failures don't accidentally unlock anything (no
 *     state machine that breaks at attempt N).
 *   - Login endpoint requires tenant context, and refuses a sign-in that
 *     names another tenant (decisions §288).
 *
 * What's intentionally NOT covered:
 *   - Rate-limiting / lockout — the seed deployment doesn't enforce
 *     a numeric attempt cap, and asserting "I tried 1000 times and
 *     still got 401" is a fragile substitute for the real test.
 */

test.describe('login security', () => {
	test('failed login does not set auth_token in localStorage', async ({ page }) => {
		await signIn(page, { email: 'noone@nowhere.test', password: 'wrong-password' });
		await expect(page).toHaveURL(/\/login/);

		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		expect(token, 'failed login must not leave a token behind').toBeNull();
	});

	test('wrong password and unknown email return the same status', async ({
		request,
		tenantAdmin
	}) => {
		// Email enumeration via response code is a CWE-204 violation.
		// Both shapes must produce the same 4xx so an attacker can't
		// distinguish "valid user, wrong password" from "no such user."
		const slug = currentTenantSlug();
		const wrongPwd = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: tenantAdmin.email, password: 'definitely-not-the-password' }
		});
		const unknownEmail = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: 'noone@nowhere.test', password: 'whatever' }
		});

		expect(wrongPwd.status()).toBe(unknownEmail.status());
		expect(wrongPwd.status()).toBeGreaterThanOrEqual(400);
		expect(wrongPwd.status()).toBeLessThan(500);
	});

	test('login response carries no token on failure', async ({ request, tenantAdmin }) => {
		const res = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: tenantAdmin.email, password: 'wrong-password' }
		});
		expect(res.status()).toBeGreaterThanOrEqual(400);

		// Even on a 4xx, frameworks occasionally serialize a partial
		// success body. Make sure no token leaks through.
		let body: unknown = null;
		try {
			body = await res.json();
		} catch {
			body = null;
		}
		const serialised = JSON.stringify(body ?? '');
		expect(serialised).not.toMatch(/eyJ[A-Za-z0-9_-]{10,}/); // JWT prefix
		expect(serialised.toLowerCase()).not.toContain('access_token');
	});

	test('three sequential failed attempts all return 4xx — no state escape', async ({
		request,
		tenantAdmin
	}) => {
		// The point is not rate-limiting (we don't enforce a hard
		// lockout). The point is that retrying doesn't accidentally
		// pass after the Nth attempt because of some hidden cache,
		// counter, or session-mutation bug.
		const slug = currentTenantSlug();
		for (let i = 0; i < 3; i++) {
			const res = await request.post(`${API_BASE}/api/auth/login`, {
				headers: { 'X-Tenant-Slug': slug },
				data: { email: tenantAdmin.email, password: 'still-wrong' }
			});
			expect(res.status(), `attempt ${i + 1} should fail`).toBeGreaterThanOrEqual(400);
			expect(res.status()).toBeLessThan(500);
		}
	});

	test('a sign-in naming another tenant is refused with no token, and the own tenant gets its own org', async ({
		request,
		tenantAdmin
	}) => {
		// The security property: a tenant header can never obtain a token for
		// an org the account does not belong to. Login used to ignore the
		// header and mint the account's own org's token whatever page the form
		// was on, which left a user who signed in on another workspace's page
		// on a screen where every call was refused. It now refuses that
		// sign-in outright, after the password verifies, and names the
		// account's own sign-in page instead (docs/decisions.md §288).
		const slug = currentTenantSlug();
		const otherSlug = slug === 'acme' ? 'techflow' : 'acme';

		const refused = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': otherSlug },
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		expect(refused.status()).toBe(403);
		const refusedBody = (await refused.json()) as {
			access_token?: string;
			detail?: { code?: string; params?: { sign_in_url?: string | null } };
		};
		expect(refusedBody.access_token, 'a refused sign-in must carry no token').toBeUndefined();
		expect(refusedBody.detail?.code).toBe('wrong_workspace');
		// The link is the account's OWN tenant's sign-in page, never the
		// header's.
		const signInUrl = refusedBody.detail?.params?.sign_in_url;
		expect(signInUrl, 'the refusal names the account’s own sign-in page').toBeTruthy();
		expect(new URL(signInUrl!).hostname.split('.')[0]).toBe(slug);

		// With its own tenant's header the same account signs in, and the
		// token's org claim is that tenant's real org id.
		const ok = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		expect(ok.status()).toBe(200);
		const token = ((await ok.json()) as { access_token?: string }).access_token;
		expect(token).toBeTruthy();
		const payloadB64 = token!.split('.')[1];
		const payload = JSON.parse(
			Buffer.from(
				payloadB64
					.replace(/-/g, '+')
					.replace(/_/g, '/')
					.padEnd(payloadB64.length + ((4 - (payloadB64.length % 4)) % 4), '='),
				'base64'
			).toString('utf-8')
		) as { org?: string };

		const org = await request.get(`${API_BASE}/api/organization`, {
			headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': slug }
		});
		expect(org.status()).toBe(200);
		const orgBody = (await org.json()) as { id: string; slug: string };
		expect(orgBody.slug).toBe(slug);
		expect(payload.org, 'org claim must be the account’s own org').toBe(orgBody.id);
	});

	test('a token minted on the tenant is scoped to that tenant (org claim)', async ({
		request,
		tenantAdmin
	}) => {
		// Positive contract: a successful login produces a JWT whose
		// payload identifies the org. The payload is base64url JSON
		// (middle segment of the three-part JWT).
		const res = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		expect(res.status()).toBe(200);
		const body = (await res.json()) as { access_token?: string; token?: string };
		const token = body.access_token ?? body.token;
		expect(token, 'login must return a token field').toBeTruthy();

		const [, payloadB64] = token!.split('.');
		const payloadJson = Buffer.from(
			payloadB64.replace(/-/g, '+').replace(/_/g, '/').padEnd(payloadB64.length + ((4 - (payloadB64.length % 4)) % 4), '='),
			'base64'
		).toString('utf-8');
		const payload = JSON.parse(payloadJson) as { org?: string; sub?: string; jti?: string };
		expect(payload.org, 'JWT must carry an org claim').toBeTruthy();
		expect(payload.sub, 'JWT must carry a sub (user id) claim').toBeTruthy();
		expect(payload.jti, 'JWT must carry a jti (for the logout blocklist)').toBeTruthy();
	});
});
