import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

async function apiHeaders(page: import('@playwright/test').Page) {
	return {
		...(await authedTenantHeaders(page)),
		'Content-Type': 'application/json'
	};
}

interface RoleResponse {
	id: string;
	name: string;
	description: string | null;
	is_system: boolean;
}

async function listRoles(page: import('@playwright/test').Page): Promise<RoleResponse[]> {
	const headers = await apiHeaders(page);
	const resp = await page.request.get(`${API_BASE}/api/admin/roles`, { headers });
	return (await resp.json()) as RoleResponse[];
}

/**
 * Per-org custom roles. The four built-ins (admin / ap_manager /
 * ap_clerk / cfo) live with `organization_id IS NULL` and surface as
 * `is_system: true` — they are read-only. Custom roles are scoped to
 * the org that minted them. Cross-org reads return 404 (not 403) so a
 * probe can't infer that the id exists.
 */

test.describe('/api/admin/roles — per-org custom roles', () => {
	const cleanup: string[] = [];

	test.afterEach(async ({ page }) => {
		const headers = await apiHeaders(page);
		while (cleanup.length) {
			const id = cleanup.pop()!;
			await page.request.delete(`${API_BASE}/api/admin/roles/${id}`, { headers });
		}
	});

	test('list returns the four system roles flagged is_system=true', async ({ page }) => {
		const roles = await listRoles(page);
		const systemNames = roles.filter((r) => r.is_system).map((r) => r.name).sort();
		expect(systemNames).toEqual(['admin', 'ap_clerk', 'ap_manager', 'cfo']);
	});

	test('create + delete round-trip', async ({ page }) => {
		const headers = await apiHeaders(page);

		const createResp = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name: `e2e-role-${Date.now()}`, description: 'Sandbox role' }
		});
		expect(createResp.status()).toBe(201);
		const created = (await createResp.json()) as RoleResponse;
		expect(created.is_system).toBe(false);
		expect(created.description).toBe('Sandbox role');

		// Now visible in the list.
		const after = await listRoles(page);
		expect(after.some((r) => r.id === created.id)).toBe(true);

		// Delete succeeds.
		const delResp = await page.request.delete(`${API_BASE}/api/admin/roles/${created.id}`, {
			headers
		});
		expect(delResp.status()).toBe(204);
	});

	test('cannot create a role colliding with a system role name', async ({ page }) => {
		const headers = await apiHeaders(page);

		const resp = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name: 'admin' }
		});
		expect(resp.status()).toBe(400);
	});

	test('duplicate names within an org return 409', async ({ page }) => {
		const headers = await apiHeaders(page);

		const name = `dup-${Date.now()}`;
		const first = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name }
		});
		expect(first.status()).toBe(201);
		cleanup.push(((await first.json()) as RoleResponse).id);

		const second = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name }
		});
		expect(second.status()).toBe(409);
	});

	test('PATCH updates description but rejects system roles', async ({ page }) => {
		const headers = await apiHeaders(page);

		// Create a custom role we can edit.
		const created = (await (
			await page.request.post(`${API_BASE}/api/admin/roles`, {
				headers,
				data: { name: `patch-${Date.now()}` }
			})
		).json()) as RoleResponse;
		cleanup.push(created.id);

		const patch = await page.request.patch(`${API_BASE}/api/admin/roles/${created.id}`, {
			headers,
			data: { description: 'updated' }
		});
		expect(patch.status()).toBe(200);
		expect(((await patch.json()) as RoleResponse).description).toBe('updated');

		// System role: the admin role is the deterministic system row.
		const allRoles = await listRoles(page);
		const adminRole = allRoles.find((r) => r.is_system && r.name === 'admin');
		expect(adminRole).toBeDefined();
		const sysPatch = await page.request.patch(
			`${API_BASE}/api/admin/roles/${adminRole!.id}`,
			{ headers, data: { description: 'nope' } }
		);
		expect(sysPatch.status()).toBe(403);
	});

	test('DELETE refuses when role is assigned to a user', async ({ page, tenantClerk }) => {
		const headers = await apiHeaders(page);

		// Mint a role and grant it to a NON-admin user. Granting it to
		// the admin would revoke our own session (SOC 2: any role change
		// drops the user's existing JWTs) and the rest of the spec would
		// 401. The clerk works fine — they hold one role we keep.
		const created = (await (
			await page.request.post(`${API_BASE}/api/admin/roles`, {
				headers,
				data: { name: `assigned-${Date.now()}` }
			})
		).json()) as RoleResponse;
		cleanup.push(created.id);

		const usersResp = await page.request.get(`${API_BASE}/api/admin/users`, { headers });
		const usersBody = (await usersResp.json()) as {
			items: Array<{ id: string; email: string; roles: Array<{ name: string }> }>;
		};
		const target = usersBody.items.find((u) => u.email === tenantClerk.email)!;
		const baseRoles = target.roles.map((r) => r.name);

		const grant = await page.request.patch(`${API_BASE}/api/admin/users/${target.id}`, {
			headers,
			data: { role_names: [...baseRoles, created.name] }
		});
		expect(grant.status()).toBe(200);

		// Now delete refuses with 409 + a count.
		const del = await page.request.delete(`${API_BASE}/api/admin/roles/${created.id}`, {
			headers
		});
		expect(del.status()).toBe(409);

		// Detach + delete.
		await page.request.patch(`${API_BASE}/api/admin/users/${target.id}`, {
			headers,
			data: { role_names: baseRoles }
		});
		const delAgain = await page.request.delete(
			`${API_BASE}/api/admin/roles/${created.id}`,
			{ headers }
		);
		expect(delAgain.status()).toBe(204);
		cleanup.pop(); // already deleted
	});

	test('non-admin (clerk) gets 403 on every role endpoint', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const headers = await apiHeaders(page);

		const create = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name: 'should-403' }
		});
		expect(create.status()).toBe(403);

		const list = await page.request.get(`${API_BASE}/api/admin/roles`, { headers });
		expect(list.status()).toBe(403);
	});

	test('roles page renders with system + custom split and create modal', async ({ page }) => {
		const headers = await apiHeaders(page);

		// Seed one custom role so the Custom section is non-empty.
		const seedName = `ui-${Date.now()}`;
		const seedResp = await page.request.post(`${API_BASE}/api/admin/roles`, {
			headers,
			data: { name: seedName, description: 'seeded for the UI smoke test' }
		});
		const seeded = (await seedResp.json()) as RoleResponse;
		cleanup.push(seeded.id);

		// Roles now live on the Users & Roles page's Roles tab. (`/admin/roles`
		// still redirects here — that back-compat path is covered in admin.spec.ts.)
		await page.goto('/admin?tab=roles');

		await expect(page.getByRole('heading', { name: 'System roles' })).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Custom roles' })).toBeVisible();
		// All four system roles render, by their LABEL rather than their slug:
		// the table reads `types/admin.ts::ROLE_LABEL_KEYS` through
		// `roleLabelKey()` (`docs/decisions.md` §159), so the cell says
		// "AP Manager" where it used to say `ap_manager`. Exact matching, because
		// the default substring match would let "Admin" satisfy the assertion for
		// three of the four.
		for (const label of ['Admin', 'AP Manager', 'AP Clerk', 'CFO']) {
			await expect(page.getByRole('cell', { name: label, exact: true })).toBeVisible();
		}
		// Seeded custom role surfaces — and still by its stored name, which is the
		// point of the accessor's `null`: a name an admin typed into their own
		// tenant is data, and is never translated.
		await expect(page.getByRole('cell', { name: seedName })).toBeVisible();

		// Create modal opens.
		await page.getByRole('button', { name: '+ Create Role' }).click();
		await expect(page.getByRole('dialog', { name: 'Create role' })).toBeVisible();
	});
});
