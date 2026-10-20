import {
	API_BASE,
	authToken,
	authedTenantHeaders,
	expect,
	TECHFLOW_ADMIN,
	test
} from '../fixtures/helpers';

async function deleteUser(page: import('@playwright/test').Page, id: string) {
	await page.request.delete(`${API_BASE}/api/admin/users/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}

/**
 * /admin user lifecycle — create, edit, deactivate, delete.
 *
 * Each test that mutates state cleans up via DELETE in finally so the
 * suite is re-runnable. We never delete the seeded admin user; freshly
 * created ones are scoped to a per-test email like `e2e-<ts>@test.local`.
 */

test.describe('/admin user lifecycle', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/admin');
		// KEPT deliberately, unlike every other `networkidle` in this
		// directory. `table tbody tr` alone is satisfied by DataTable's own
		// loading-state placeholder row ("No users found." — isEmpty is true
		// until the fetch resolves, same markup shape as a genuine empty
		// result), so it isn't proof the real user list has loaded — and the
		// first test below reads that selector's `count()` as its "before"
		// baseline, which is a point-in-time read with no waiting of its own.
		//
		// The second half of the original rationale — a late mount fetch
		// replacing the whole list and stomping a just-created row — no longer
		// applies: `stores/admin.svelte.ts` retires the in-flight fetch
		// (`usersSequence.supersedeInFlight()`) on every create/update/delete.
		//
		// The durable replacement is the sibling `search-pagination.spec.ts`'s
		// `mountUsersFetch()` — an explicit `waitForResponse` on the very
		// `GET /api/admin/users` this comment names, rather than a
		// no-network-for-500ms guess. Lifting it into a shared helper is the
		// remaining networkidle work for this directory.
		await page.waitForLoadState('networkidle');
	});

	test('Create User submit is disabled while empty + creates a user on submit', async ({
		page
	}) => {
		const email = `e2e-create-${Date.now()}@test.local`;
		const fullName = 'E2E Created User';
		let createdId: string | null = null;

		try {
			await page.getByRole('button', { name: '+ Invite User' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="Invite user"]');
			await expect(modal).toBeVisible();

			// HTML5 `required` blocks submit on empty inputs — clicking the
			// submit button when fields are blank should not fire the POST.
			const before = await page.locator('table tbody tr').count();
			await modal.getByRole('button', { name: /Create User/ }).click();
			// Modal stays open because validation prevented submission.
			await expect(modal).toBeVisible();

			await modal.locator('input[type="text"]').fill(fullName);
			await modal.locator('input[type="email"]').fill(email);
			// Pick the AP Clerk role so the new user gets at least one role badge.
			await modal.locator('.check-label', { hasText: 'AP Clerk' }).locator('input').check();

			const created = page.waitForResponse(
				(r) =>
					r.url().includes('/api/admin/users') &&
					r.request().method() === 'POST' &&
					r.status() === 201
			);
			await modal.getByRole('button', { name: /Create User/ }).click();
			const resp = await created;
			const body = (await resp.json()) as { id: string; email: string };
			createdId = body.id;
			expect(body.email).toBe(email);

			// "User Created" credentials modal appears after successful create.
			const credModal = page.locator('div.modal[role="dialog"][aria-label="User created"]');
			await expect(credModal).toBeVisible();
			await credModal.getByRole('button', { name: /^Done$/ }).click();

			// Table grew by one row, and the new email appears.
			await expect(page.locator('table tbody tr')).toHaveCount(before + 1);
			await expect(
				page.locator('table tbody td.email-cell', { hasText: email })
			).toBeVisible();
		} finally {
			if (createdId) await deleteUser(page, createdId);
		}
	});

	test('inviting an address another workspace holds says so, and creates nobody', async ({
		page
	}) => {
		// One address is one account in one workspace (decisions §292). The
		// techflow seed admin's address is held in techflow, never in this
		// worker's tenant, so the invite is refused with a sentence that says
		// what to do — not the old bare "Email already in use".
		await page.getByRole('button', { name: '+ Invite User' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Invite user"]');
		await expect(modal).toBeVisible();
		await modal.locator('input[type="text"]').fill('Someone Elsewhere');
		await modal.locator('input[type="email"]').fill(TECHFLOW_ADMIN.email);
		await modal.locator('.check-label', { hasText: 'AP Clerk' }).locator('input').check();

		const refused = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname === '/api/admin/users' && r.request().method() === 'POST'
		);
		await modal.getByRole('button', { name: /Create User/ }).click();
		const resp = await refused;
		expect(resp.status()).toBe(409);
		expect(((await resp.json()) as { detail: { code: string } }).detail.code).toBe(
			'email_in_other_workspace'
		);

		await expect(
			page.locator('.toast.error .toast-text', {
				hasText: 'This email address already has an account in another workspace.'
			})
		).toBeVisible();
		await expect(
			page.locator('div.modal[role="dialog"][aria-label="User created"]')
		).toHaveCount(0);
	});

	test('Edit modal pre-fills the row and persists name change', async ({ page }) => {
		// Create a fresh user via API so we don't mutate seeded users.
		const headers = await authedTenantHeaders(page);
		const email = `e2e-edit-${Date.now()}@test.local`;
		const createResp = await page.request.post(`${API_BASE}/api/admin/users`, {
			headers,
			data: { full_name: 'Edit Me Original', email, role_names: ['ap_clerk'] }
		});
		const created = (await createResp.json()) as { id: string };

		try {
			await page.reload();
			const row = page.locator('table tbody tr', { hasText: email });
			await expect(row).toBeVisible();

			await row.getByRole('button', { name: 'Edit' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="Edit user"]');
			await expect(modal).toBeVisible();
			// Name + email inputs pre-fill from the row.
			await expect(modal.locator('input[type="text"]')).toHaveValue('Edit Me Original');
			await expect(modal.locator('input[type="email"]')).toHaveValue(email);
			// #566: no password input — a browser autofilled the admin's own
			// password into the old one and saving a rename reset the user.
			await expect(modal.locator('input[type="password"]')).toHaveCount(0);

			const newName = 'Edit Me Renamed';
			await modal.locator('input[type="text"]').fill(newName);

			const patched = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}`) &&
					r.request().method() === 'PATCH'
			);
			await modal.getByRole('button', { name: /Save Changes/ }).click();
			const resp = await patched;
			expect(resp.status()).toBe(200);
			// Only the field that changed is sent (#566 — the audit trail used to
			// name every field the form resent).
			expect(resp.request().postDataJSON()).toEqual({ full_name: newName });

			// Modal closes; row reflects the new name.
			await expect(modal).toBeHidden();
			await expect(
				page.locator('table tbody tr', { hasText: email }).locator('.name-cell')
			).toContainText(newName);
		} finally {
			await deleteUser(page, created.id);
		}
	});

	test('Edit modal can toggle a role on, persisting the change', async ({ page }) => {
		const token = await authToken(page);
		const headers = await authedTenantHeaders(page);
		const email = `e2e-roles-${Date.now()}@test.local`;
		const createResp = await page.request.post(`${API_BASE}/api/admin/users`, {
			headers,
			data: { full_name: 'Role Test', email, role_names: ['ap_clerk'] }
		});
		const created = (await createResp.json()) as { id: string };

		try {
			await page.reload();
			const row = page.locator('table tbody tr', { hasText: email });
			await expect(row).toBeVisible();

			await row.getByRole('button', { name: 'Edit' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="Edit user"]');
			await expect(modal).toBeVisible();

			// Toggle "AP Manager" on (AP Clerk is already assigned).
			await modal.locator('.check-label', { hasText: 'AP Manager' }).locator('input').check();

			const patched = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}`) &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
			await modal.getByRole('button', { name: /Save Changes/ }).click();
			await patched;

			// Verify via API — role-change forces a session revoke, so reading
			// the role badges from the now-stale DOM is racy. Token from before
			// the revoke still works for read-only API calls.
			void token;
			const get = await page.request.get(`${API_BASE}/api/admin/users`, { headers });
			const list = (await get.json()) as {
				items: Array<{ id: string; roles: Array<{ name: string }> }>;
			};
			const updated = list.items.find((u) => u.id === created.id);
			expect(updated).toBeTruthy();
			const names = updated!.roles.map((r) => r.name).sort();
			expect(names).toContain('ap_clerk');
			expect(names).toContain('ap_manager');
		} finally {
			await deleteUser(page, created.id);
		}
	});

	test('Deactivate flips status to Inactive; Activate restores it', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const email = `e2e-deactivate-${Date.now()}@test.local`;
		const createResp = await page.request.post(`${API_BASE}/api/admin/users`, {
			headers,
			data: { full_name: 'Deactivate Me', email, role_names: ['ap_clerk'] }
		});
		const created = (await createResp.json()) as { id: string };

		try {
			await page.reload();
			const row = page.locator('table tbody tr', { hasText: email });
			await expect(row.locator('.status-dot')).toContainText('Active');

			const deactivated = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}`) &&
					r.request().method() === 'PATCH'
			);
			// `exact` so we hit the action button, not the row's
			// "Edit user Deactivate Me" RowLink (substring-matches "Deactivate").
			await row.getByRole('button', { name: 'Deactivate', exact: true }).click();
			await deactivated;

			await expect(row.locator('.status-dot')).toContainText('Inactive');
			await expect(row).toHaveClass(/inactive/);

			const activated = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}`) &&
					r.request().method() === 'PATCH'
			);
			await row.getByRole('button', { name: 'Activate', exact: true }).click();
			await activated;

			await expect(row.locator('.status-dot')).toContainText('Active');
		} finally {
			await deleteUser(page, created.id);
		}
	});

	test('Delete requires a confirm click (two-step armed pattern)', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const email = `e2e-delete-${Date.now()}@test.local`;
		const createResp = await page.request.post(`${API_BASE}/api/admin/users`, {
			headers,
			data: { full_name: 'Delete Me', email, role_names: [] }
		});
		const created = (await createResp.json()) as { id: string };
		let didDelete = false;

		try {
			await page.reload();
			const row = page.locator('table tbody tr', { hasText: email });
			await expect(row).toBeVisible();

			// First click arms (no DELETE issued yet).
			await row.locator('button.row-action.variant-danger').click();
			await expect(row.locator('button.row-action.variant-danger.armed')).toBeVisible();

			// Second click commits.
			const deleted = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}`) &&
					r.request().method() === 'DELETE'
			);
			await row.locator('button.row-action.variant-danger').click();
			const resp = await deleted;
			expect(resp.status()).toBe(204);
			didDelete = true;

			// Row is gone.
			await expect(page.locator('table tbody tr', { hasText: email })).toHaveCount(0);
		} finally {
			if (!didDelete) await deleteUser(page, created.id);
		}
	});

	test('Cannot delete yourself — current user has no delete button', async ({
		page,
		tenantAdmin
	}) => {
		const youRow = page.locator('table tbody tr', { hasText: tenantAdmin.email });
		await expect(youRow.locator('.you-badge')).toBeVisible();
		await expect(youRow.locator('button.row-action.variant-danger')).toHaveCount(0);
	});
});
