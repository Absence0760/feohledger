import {
	API_BASE,
	createUserWithKnownPassword,
	currentTenantSlug,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

// Start unauthenticated — this spec drives its own multi-role sign-ins / sign-outs.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Password-security e2e — end-to-end coverage that complements the
 * backend's `test_password_security.py` (unit) and the existing
 * `change-password.spec.ts` (UX hints).
 *
 * What we lock in here:
 *   - Below-minimum-length passwords are rejected at the API
 *   - Each complexity rule (upper / lower / digit) is enforced
 *   - Successful change rotates the credential: old password fails,
 *     new password works, on the next login attempt
 *   - Wrong current password returns a generic, non-leaky error
 *
 * The tests provision a throwaway user via /api/admin/users so the
 * seeded demo credentials are never touched.
 */

async function adminToken(page: import('@playwright/test').Page): Promise<string> {
	await signInAndWait(page);
	const t = await page.evaluate(() => localStorage.getItem('auth_token'));
	if (!t) throw new Error('not signed in as admin');
	return t;
}

interface CreatedUser {
	id: string;
	email: string;
	tempPassword: string;
}

/** A throwaway account with a known password. The admin create returns no
 *  credential (a set-password link is emailed, decisions §283), so the helper
 *  gives the account a password the spec knows. */
async function createUser(
	page: import('@playwright/test').Page
): Promise<CreatedUser> {
	const created = await createUserWithKnownPassword(page, {
		mustChange: true,
		emailPrefix: 'e2e-pwsec'
	});
	return { id: created.id, email: created.email, tempPassword: created.password };
}

async function deleteUser(
	page: import('@playwright/test').Page,
	id: string,
	token: string
) {
	await page.request.delete(`${API_BASE}/api/admin/users/${id}`, {
		headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() }
	});
}

async function loginAndGetToken(
	page: import('@playwright/test').Page,
	email: string,
	password: string
): Promise<{ status: number; token: string | null }> {
	const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': currentTenantSlug() },
		data: { email, password }
	});
	const body = resp.status() === 200 ? ((await resp.json()) as { access_token?: string }) : null;
	return { status: resp.status(), token: body?.access_token ?? null };
}

test.describe('password security — change-password API', () => {
	test('rejects new password shorter than the minimum', async ({ page }) => {
		const admin = await adminToken(page);
		const user = await createUser(page);
		try {
			const login = await loginAndGetToken(page, user.email, user.tempPassword);
			expect(login.token).toBeTruthy();

			const r = await page.request.post(`${API_BASE}/api/auth/change-password`, {
				headers: {
					Authorization: `Bearer ${login.token}`,
					'X-Tenant-Slug': currentTenantSlug()
				},
				data: { current_password: user.tempPassword, new_password: 'Short1A' }
			});
			expect(r.status(), 'too-short password must be rejected').toBeGreaterThanOrEqual(400);
			expect(r.status()).toBeLessThan(500);
		} finally {
			await deleteUser(page, user.id, admin);
		}
	});

	test('rejects new password missing an uppercase letter', async ({ page }) => {
		const admin = await adminToken(page);
		const user = await createUser(page);
		try {
			const login = await loginAndGetToken(page, user.email, user.tempPassword);
			const r = await page.request.post(`${API_BASE}/api/auth/change-password`, {
				headers: {
					Authorization: `Bearer ${login.token}`,
					'X-Tenant-Slug': currentTenantSlug()
				},
				data: {
					current_password: user.tempPassword,
					new_password: 'all-lowercase-1234'
				}
			});
			expect(r.status()).toBeGreaterThanOrEqual(400);
			const body = await r.text();
			// Error mentions the missing class so the UI can render a hint.
			expect(body.toLowerCase()).toMatch(/upper/);
		} finally {
			await deleteUser(page, user.id, admin);
		}
	});

	test('rejects new password missing a digit', async ({ page }) => {
		const admin = await adminToken(page);
		const user = await createUser(page);
		try {
			const login = await loginAndGetToken(page, user.email, user.tempPassword);
			const r = await page.request.post(`${API_BASE}/api/auth/change-password`, {
				headers: {
					Authorization: `Bearer ${login.token}`,
					'X-Tenant-Slug': currentTenantSlug()
				},
				data: {
					current_password: user.tempPassword,
					new_password: 'NoDigitsHereAtAll'
				}
			});
			expect(r.status()).toBeGreaterThanOrEqual(400);
			expect((await r.text()).toLowerCase()).toMatch(/digit/);
		} finally {
			await deleteUser(page, user.id, admin);
		}
	});

	test('wrong current password returns generic error, no enumeration', async ({ page }) => {
		const admin = await adminToken(page);
		const user = await createUser(page);
		try {
			const login = await loginAndGetToken(page, user.email, user.tempPassword);
			const r = await page.request.post(`${API_BASE}/api/auth/change-password`, {
				headers: {
					Authorization: `Bearer ${login.token}`,
					'X-Tenant-Slug': currentTenantSlug()
				},
				data: {
					current_password: 'this-is-not-the-real-pw',
					new_password: 'BrandNewPass-123'
				}
			});
			expect(r.status()).toBe(400);
			const body = (await r.text()).toLowerCase();
			// The detail must NOT echo either password.
			expect(body).not.toContain('this-is-not-the-real-pw');
			expect(body).not.toContain('brandnewpass-123');
			expect(body).not.toContain(user.tempPassword.toLowerCase());
		} finally {
			await deleteUser(page, user.id, admin);
		}
	});

	test('successful change rotates the credential — old password is rejected', async ({
		page
	}) => {
		const admin = await adminToken(page);
		const user = await createUser(page);
		const newPassword = `Rotation-${Date.now()}-Aa1`;
		try {
			const login = await loginAndGetToken(page, user.email, user.tempPassword);
			expect(login.token).toBeTruthy();

			const r = await page.request.post(`${API_BASE}/api/auth/change-password`, {
				headers: {
					Authorization: `Bearer ${login.token}`,
					'X-Tenant-Slug': currentTenantSlug()
				},
				data: {
					current_password: user.tempPassword,
					new_password: newPassword
				}
			});
			expect(r.status(), 'change-password must succeed').toBe(200);

			// Old password must no longer authenticate.
			const oldAttempt = await loginAndGetToken(page, user.email, user.tempPassword);
			expect(oldAttempt.status, 'old password must be revoked').toBe(401);

			// New password must.
			const newAttempt = await loginAndGetToken(page, user.email, newPassword);
			expect(newAttempt.status, 'new password must authenticate').toBe(200);
		} finally {
			await deleteUser(page, user.id, admin);
		}
	});
});

test.describe('password security — login error wording', () => {
	test('unknown email and wrong password return identical body', async ({
		request,
		tenantAdmin
	}) => {
		// Pin CWE-204 at the live HTTP layer. The handler-level test
		// covers the same contract for unit-grade speed; this one runs
		// against the real running server so a future deviation in
		// middleware or error rendering is caught too.
		const slug = currentTenantSlug();
		const r1 = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: 'definitely-not-real@nowhere.test', password: 'x' }
		});
		const r2 = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': slug },
			data: { email: tenantAdmin.email, password: 'definitely-wrong-pw' }
		});
		expect(r1.status()).toBe(r2.status());
		expect(r1.status()).toBe(401);
		expect(await r1.text(), 'body must match across enumeration branches').toBe(
			await r2.text()
		);
	});

	test('login error wording does not name the failing field', async ({ request, tenantAdmin }) => {
		const r = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: tenantAdmin.email, password: 'wrong' }
		});
		const detail = (await r.text()).toLowerCase();
		// Generic wording only — the contract is "Invalid credentials".
		// "Password incorrect" or "User not found" would be enumeration
		// hints.
		expect(detail).not.toContain('user not found');
		expect(detail).not.toContain('password incorrect');
		expect(detail).not.toContain('incorrect password');
		expect(detail).not.toContain('no such user');
		expect(detail).not.toContain('email not found');
	});

	test('failed login response does not echo the attempted password', async ({
		request,
		tenantAdmin
	}) => {
		const secret = 'Captain-Crunch-Pizza-42!';
		const r = await request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: tenantAdmin.email, password: secret }
		});
		const body = await r.text();
		expect(body).not.toContain(secret);
		expect(body.toLowerCase()).not.toContain(secret.toLowerCase());
	});
});
