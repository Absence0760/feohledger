import {
	API_BASE,
	currentTenantSlug,
	deleteInvoicesWhere,
	escapeRegExp,
	expect,
	tenantBase,
	test
} from '../fixtures/helpers';

// Start unauthenticated — each test logs in as a specific role to probe endpoint gates.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Endpoint-level RBAC. `rbac.spec.ts` covers sidebar visibility
 * (what the user sees); this spec covers what the backend actually
 * lets each role *do*. A frontend that forgets to hide a button is a
 * UX bug; a backend that accepts the call anyway is a privilege
 * escalation.
 *
 * The matrix below targets the highest-value gates:
 *
 *   admin-only:    POST /api/admin/users, DELETE /api/admin/users/{id}
 *   admin/manager: POST /api/vendors, DELETE /api/vendors/{id}
 *   CFO-only:      POST /api/payments/runs/{id}/approve
 *   entry roles:   POST /api/invoices (ap_clerk included), but
 *                  POST /api/invoices/{id}/approve stays off-limits to it
 *   any-role read: GET /api/dashboard (positive control)
 *
 * For each gate we hit it with a *lower-privilege* token and assert
 * 4xx, then a *correctly-privileged* token and assert it doesn't
 * 403 (it might 404 for a random UUID — the security property is
 * "role wasn't the blocker").
 */

async function tokenAfterLogin(
	page: import('@playwright/test').Page,
	creds: { email: string; password: string }
): Promise<string> {
	const origin = tenantBase(currentTenantSlug());
	await page.goto(`${origin}/login`);
	await page.locator('input[type="email"]').fill(creds.email);
	await page.locator('input[type="password"]').fill(creds.password);
	await page.locator('form button[type="submit"]').click();
	await page.waitForURL(new RegExp(`^${escapeRegExp(origin)}/?$`), {
		timeout: 15_000
	});
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	if (!token) throw new Error('expected auth_token after sign-in');
	return token;
}

function expectRoleDeny(status: number, what: string) {
	expect(status, `${what}: role must be the blocker`).toBe(403);
}

test.describe('RBAC at the API layer', () => {
	// ---- admin-only ------------------------------------------------------

	test('clerk cannot POST /api/admin/users', async ({ page, request, tenantClerk }) => {
		const clerkToken = await tokenAfterLogin(page, tenantClerk);
		const r = await request.post(`${API_BASE}/api/admin/users`, {
			headers: { Authorization: `Bearer ${clerkToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: 'newperson@test.local', full_name: 'New', role: 'ap_clerk' }
		});
		expectRoleDeny(r.status(), 'clerk POST admin/users');
	});

	test('manager cannot POST /api/admin/users', async ({ page, request, tenantManager }) => {
		const managerToken = await tokenAfterLogin(page, tenantManager);
		const r = await request.post(`${API_BASE}/api/admin/users`, {
			headers: { Authorization: `Bearer ${managerToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: 'newperson@test.local', full_name: 'New', role: 'ap_clerk' }
		});
		expectRoleDeny(r.status(), 'manager POST admin/users');
	});

	test('cfo cannot POST /api/admin/users', async ({ page, request, tenantCfo }) => {
		const cfoToken = await tokenAfterLogin(page, tenantCfo);
		const r = await request.post(`${API_BASE}/api/admin/users`, {
			headers: { Authorization: `Bearer ${cfoToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { email: 'newperson@test.local', full_name: 'New', role: 'ap_clerk' }
		});
		expectRoleDeny(r.status(), 'cfo POST admin/users');
	});

	test('admin CAN POST /api/admin/users (positive control)', async ({
		page,
		request,
		tenantAdmin
	}) => {
		const adminToken = await tokenAfterLogin(page, tenantAdmin);
		const r = await request.post(`${API_BASE}/api/admin/users`, {
			headers: { Authorization: `Bearer ${adminToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: {
				email: `playwright-rbac-${Date.now()}@test.local`,
				full_name: 'Playwright RBAC',
				role: 'ap_clerk'
			}
		});
		// 200/201 if it succeeds; anything in [400, 422] for a body
		// validation issue is fine — the contract is "role wasn't the
		// blocker." 403 would mean RBAC denied admin.
		expect(r.status(), 'admin should not be RBAC-denied').not.toBe(403);
	});

	// ---- admin/manager only ---------------------------------------------

	test('clerk cannot POST /api/vendors', async ({ page, request, tenantClerk }) => {
		const clerkToken = await tokenAfterLogin(page, tenantClerk);
		const r = await request.post(`${API_BASE}/api/vendors`, {
			headers: { Authorization: `Bearer ${clerkToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { name: 'Sneak Vendor' }
		});
		expectRoleDeny(r.status(), 'clerk POST vendor');
	});

	test('cfo cannot POST /api/vendors', async ({ page, request, tenantCfo }) => {
		// CFO is read-many but cannot mutate vendor records.
		const cfoToken = await tokenAfterLogin(page, tenantCfo);
		const r = await request.post(`${API_BASE}/api/vendors`, {
			headers: { Authorization: `Bearer ${cfoToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { name: 'CFO Sneak Vendor' }
		});
		expectRoleDeny(r.status(), 'cfo POST vendor');
	});

	test('manager CAN POST /api/vendors (positive control)', async ({
		page,
		request,
		tenantManager
	}) => {
		const managerToken = await tokenAfterLogin(page, tenantManager);
		const r = await request.post(`${API_BASE}/api/vendors`, {
			headers: { Authorization: `Bearer ${managerToken}`, 'X-Tenant-Slug': currentTenantSlug() },
			data: { name: `Playwright Vendor ${Date.now()}` }
		});
		expect(r.status(), 'manager should not be RBAC-denied').not.toBe(403);
	});

	// ---- CFO-only --------------------------------------------------------

	test('clerk cannot POST /api/payments/runs/{id}/approve', async ({
		page,
		request,
		tenantClerk
	}) => {
		// CFO sign-off endpoint — clerks must never reach it.
		const clerkToken = await tokenAfterLogin(page, tenantClerk);
		const fakeRunId = '00000000-0000-0000-0000-000000000001';
		const r = await request.post(`${API_BASE}/api/payments/runs/${fakeRunId}/approve`, {
			headers: { Authorization: `Bearer ${clerkToken}`, 'X-Tenant-Slug': currentTenantSlug() }
		});
		expectRoleDeny(r.status(), 'clerk approve run');
	});

	test('manager cannot POST /api/payments/runs/{id}/approve', async ({
		page,
		request,
		tenantManager
	}) => {
		const managerToken = await tokenAfterLogin(page, tenantManager);
		const fakeRunId = '00000000-0000-0000-0000-000000000001';
		const r = await request.post(`${API_BASE}/api/payments/runs/${fakeRunId}/approve`, {
			headers: { Authorization: `Bearer ${managerToken}`, 'X-Tenant-Slug': currentTenantSlug() }
		});
		expectRoleDeny(r.status(), 'manager approve run');
	});

	test('admin cannot POST /api/payments/runs/{id}/approve', async ({
		page,
		request,
		tenantAdmin
	}) => {
		// Yes — CFO sign-off is CFO-only by design. Admin is high-
		// privilege but not the segregation-of-duties role for
		// large-dollar payment approval.
		const adminToken = await tokenAfterLogin(page, tenantAdmin);
		const fakeRunId = '00000000-0000-0000-0000-000000000001';
		const r = await request.post(`${API_BASE}/api/payments/runs/${fakeRunId}/approve`, {
			headers: { Authorization: `Bearer ${adminToken}`, 'X-Tenant-Slug': currentTenantSlug() }
		});
		expectRoleDeny(r.status(), 'admin approve run');
	});

	test('CFO is NOT RBAC-denied on POST /api/payments/runs/{id}/approve', async ({
		page,
		request,
		tenantCfo
	}) => {
		const cfoToken = await tokenAfterLogin(page, tenantCfo);
		const fakeRunId = '00000000-0000-0000-0000-000000000001';
		const r = await request.post(`${API_BASE}/api/payments/runs/${fakeRunId}/approve`, {
			headers: { Authorization: `Bearer ${cfoToken}`, 'X-Tenant-Slug': currentTenantSlug() }
		});
		// CFO will hit 404 (run doesn't exist) — the security property
		// is that the RBAC layer accepted them.
		expect(r.status(), 'CFO must not be RBAC-denied').not.toBe(403);
	});

	// ---- Read endpoints all roles can hit -------------------------------

	test('every role can GET /api/dashboard (positive control)', async ({
		page,
		request,
		tenantAdmin,
		tenantManager,
		tenantClerk,
		tenantCfo
	}) => {
		// If this fails for a role it's not an RBAC bug — it's a
		// regression somewhere else (auth, tenant resolution, etc).
		for (const [label, creds] of [
			['admin', tenantAdmin] as const,
			['manager', tenantManager] as const,
			['cfo', tenantCfo] as const,
			['clerk', tenantClerk] as const
		]) {
			const token = await tokenAfterLogin(page, creds);
			const r = await request.get(`${API_BASE}/api/dashboard`, {
				headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() }
			});
			expect(r.status(), `${label} GET /dashboard`).toBe(200);
		}
	});

	// A clerk browses Catalogs (+ guided buying) to raise a requisition, so
	// the catalogs read endpoint grants ap_clerk and the sidebar surfaces it
	// (see rbac.spec.ts). Pin the backend side here so the two can't drift:
	// if this 403s, the gate silently dropped clerk and the nav item lies.
	// Entering invoices is the AP clerk's job (`backend/app/api/invoice_entry.py`);
	// approving them is not. The toolbar's Create / Upload buttons show for a
	// clerk, so pin that the backend takes the write — and still refuses the
	// sign-off on the very invoice the clerk just keyed.
	test('clerk CAN POST /api/invoices but cannot approve it', async ({
		page,
		request,
		tenantClerk
	}) => {
		const marker = `E2E-RBAC-CLERK-ENTRY-${Date.now()}`;
		try {
			const token = await tokenAfterLogin(page, tenantClerk);
			const headers = { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() };
			const created = await request.post(`${API_BASE}/api/invoices`, {
				headers,
				data: { vendor: 'E2E Clerk Entry Vendor', invoice_number: marker, amount: '42.00' }
			});
			expect(created.status(), 'clerk POST /invoices').toBe(201);
			const { id } = (await created.json()) as { id: string };

			const submit = await request.post(`${API_BASE}/api/invoices/${id}/complete`, { headers });
			expect(submit.status(), 'clerk submit for review').toBe(200);

			const approve = await request.post(`${API_BASE}/api/invoices/${id}/approve`, {
				headers,
				data: {}
			});
			expectRoleDeny(approve.status(), 'clerk approve invoice');
		} finally {
			await deleteInvoicesWhere(`invoice_number = '${marker}'`);
		}
	});

	test('clerk CAN GET /api/catalogs (sidebar/backend RBAC parity)', async ({
		page,
		request,
		tenantClerk
	}) => {
		const token = await tokenAfterLogin(page, tenantClerk);
		const r = await request.get(`${API_BASE}/api/catalogs`, {
			headers: { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() }
		});
		expect(r.status(), 'clerk GET /catalogs must not be RBAC-denied').not.toBe(403);
		expect(r.status(), 'clerk GET /catalogs').toBe(200);
	});
});
