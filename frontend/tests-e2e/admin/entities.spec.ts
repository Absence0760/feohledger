import { API_BASE, authedTenantHeaders, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * /admin/entities — multi-entity (legal entity / subsidiary) admin.
 *
 * Surfaces `backend/app/api/entities.py`:
 *  - GET    /api/entities              → any authenticated user (the sidebar switcher reads it)
 *  - POST   /api/entities              → admin; 409 on a duplicate slug
 *  - PATCH  /api/entities/{id}         → admin; 400 when deactivating the default
 *  - POST   /api/entities/{id}/set-default → admin
 *
 * The point of the page: without a create UI a tenant was stuck on the single
 * entity provisioning made, so the sidebar switcher — which hides below two
 * entities by design — could never appear. The first test drives that whole
 * path, from the create modal to the switcher becoming visible.
 *
 * Login model mirrors the sibling admin pages (`/admin/api-keys`,
 * `/admin/retention`): an explicit sign-in rather than the shared storage-state
 * cache, so the gated page is reliably authed before each test.
 */

interface EntityRow {
	id: string;
	name: string;
	slug: string;
	currency: string | null;
	is_default: boolean;
	is_active: boolean;
}

/** A slug unique per run so reruns can't collide on the per-tenant unique
 *  constraint (entities have no delete route — deactivation is the exit). */
function uniqueSuffix(): string {
	return `${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`;
}

test.describe('/admin/entities (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('creating a second entity makes the sidebar switcher appear', async ({ page }) => {
		const suffix = uniqueSuffix();
		const name = `E2E Admin Sub ${suffix}`;
		const slug = `e2e-admin-sub-${suffix}`;

		await page.goto('/admin/entities');
		await expect(page.getByRole('heading', { name: 'Entities' })).toBeVisible();
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });

		// The seeded tenant ships with exactly one (Default) entity, so the
		// switcher starts hidden — that is precisely why the page is needed.
		const rowsBefore = await page.getByTestId('entity-row').count();
		if (rowsBefore === 1) {
			await expect(page.locator('.entity-btn')).toHaveCount(0);
		}

		await page.getByRole('button', { name: '+ Create entity' }).click();
		const createModal = page.getByRole('dialog', { name: 'Create entity' });
		await expect(createModal).toBeVisible();

		// The slug auto-derives from the name until the admin edits it directly.
		await page.getByTestId('entity-name-input').fill(name);
		await expect(page.getByTestId('entity-slug-input')).toHaveValue(
			name.toLowerCase().replace(/[^a-z0-9]+/g, '-')
		);
		await page.getByTestId('entity-slug-input').fill(slug);
		await page.getByTestId('entity-currency-input').fill('gbp');
		await createModal.getByRole('button', { name: 'Create' }).click();

		await expect(createModal).toBeHidden({ timeout: 10_000 });
		const row = page.locator(`[data-testid="entity-row"][data-slug="${slug}"]`);
		await expect(row).toBeVisible();
		// Currency is upper-cased on the way in (backend `.upper()`).
		await expect(row).toContainText('GBP');
		await expect(row).toContainText('Active');

		// The end-to-end point: with a second entity the sidebar switcher renders,
		// with no page reload — the page re-syncs the store after a mutation.
		await expect(page.locator('.entity-btn')).toBeVisible({ timeout: 10_000 });
		await page.locator('.entity-btn').click();
		await expect(page.locator('.entity-option', { hasText: name })).toBeVisible();
	});

	test('a duplicate slug renders the backend refusal inline', async ({ page }) => {
		const suffix = uniqueSuffix();
		const slug = `e2e-dup-${suffix}`;

		// Seed the collision through the API so the test asserts on the refusal,
		// not on a second create flow.
		const headers = await authedTenantHeaders(page);
		const seed = await page.request.post(`${API_BASE}/api/entities`, {
			headers,
			data: { name: `E2E Dup ${suffix}`, slug }
		});
		expect(seed.status()).toBe(201);

		await page.goto('/admin/entities');
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });

		await page.getByRole('button', { name: '+ Create entity' }).click();
		const createModal = page.getByRole('dialog', { name: 'Create entity' });
		await page.getByTestId('entity-name-input').fill(`E2E Dup Retry ${suffix}`);
		await page.getByTestId('entity-slug-input').fill(slug);
		await createModal.getByRole('button', { name: 'Create' }).click();

		// The backend's own sentence, not a generic failure — and the modal stays
		// open so the admin can correct the slug in place.
		const err = page.getByTestId('entity-create-error');
		await expect(err).toBeVisible({ timeout: 10_000 });
		await expect(err).toHaveText('An entity with that slug already exists.');
		await expect(createModal).toBeVisible();
	});

	test('deactivating the default entity renders the backend refusal inline', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const listed = (await (
			await page.request.get(`${API_BASE}/api/entities`, { headers })
		).json()) as EntityRow[];
		const defaultEntity = listed.find((e) => e.is_default);
		expect(defaultEntity, 'the tenant must have a default entity').toBeTruthy();

		await page.goto('/admin/entities');
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });

		const row = page.locator(`[data-testid="entity-row"][data-slug="${defaultEntity!.slug}"]`);
		await expect(row).toContainText('Default');
		await row.getByRole('button', { name: `Edit ${defaultEntity!.name}` }).click();

		const editModal = page.getByRole('dialog', { name: 'Edit entity' });
		await expect(editModal).toBeVisible();
		// The checkbox is deliberately NOT disabled — the server owns the rule and
		// its refusal is the explanation the admin gets.
		const activeBox = page.getByTestId('entity-edit-active');
		await expect(activeBox).toBeEnabled();
		await activeBox.uncheck();
		await editModal.getByRole('button', { name: 'Save' }).click();

		const err = page.getByTestId('entity-edit-error');
		await expect(err).toBeVisible({ timeout: 10_000 });
		await expect(err).toHaveText('The default entity cannot be deactivated.');

		// And it really is still active server-side.
		const after = (await (
			await page.request.get(`${API_BASE}/api/entities`, { headers })
		).json()) as EntityRow[];
		expect(after.find((e) => e.id === defaultEntity!.id)!.is_active).toBe(true);
	});

	test('a rename persists and set-default moves the default', async ({ page }) => {
		const suffix = uniqueSuffix();
		const slug = `e2e-def-${suffix}`;
		const headers = await authedTenantHeaders(page);

		const created = (await (
			await page.request.post(`${API_BASE}/api/entities`, {
				headers,
				data: { name: `E2E Default ${suffix}`, slug }
			})
		).json()) as EntityRow;

		const originalDefault = (
			(await (await page.request.get(`${API_BASE}/api/entities`, { headers })).json()) as EntityRow[]
		).find((e) => e.is_default)!;

		try {
			await page.goto('/admin/entities');
			await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });

			// Rename through the edit modal.
			const row = page.locator(`[data-testid="entity-row"][data-slug="${slug}"]`);
			await row.getByRole('button', { name: `Edit E2E Default ${suffix}` }).click();
			const renamed = `E2E Renamed ${suffix}`;
			await page.getByTestId('entity-edit-name-input').fill(renamed);
			await page
				.getByRole('dialog', { name: 'Edit entity' })
				.getByRole('button', { name: 'Save' })
				.click();
			await expect(page.getByRole('dialog', { name: 'Edit entity' })).toBeHidden({
				timeout: 10_000
			});
			await expect(row).toContainText(renamed);

			// Make it the default — armed two-click, mirroring the api-keys revoke.
			await row.getByRole('button', { name: 'Make default' }).click();
			await row.getByRole('button', { name: 'Confirm' }).click();
			await expect(row).toContainText('Default', { timeout: 10_000 });

			const after = (await (
				await page.request.get(`${API_BASE}/api/entities`, { headers })
			).json()) as EntityRow[];
			expect(after.find((e) => e.id === created.id)!.is_default).toBe(true);
			expect(after.find((e) => e.id === originalDefault.id)!.is_default).toBe(false);
		} finally {
			// Restore the tenant's original default even on failure. Entities are
			// worker-tenant-wide state and the default is where every un-scoped and
			// new row lands, so leaving an E2E-only entity as default would poison
			// every later spec in this worker — not just a re-run of this one.
			const restore = await page.request.post(
				`${API_BASE}/api/entities/${originalDefault.id}/set-default`,
				{ headers }
			);
			expect(restore.ok()).toBe(true);
		}
	});

	test('the list and the create modal are axe-clean (WCAG 2.2 AA)', async ({ page }) => {
		// The a11y guard lives here rather than in `a11y/axe.spec.ts`'s route
		// table because this page's interactive surface is the MODAL — a form
		// with three labelled inputs, a checkbox, and a persistent role="alert"
		// region the backend's refusal lands in. Scanning only the list would
		// report the page clean while every one of those went unchecked.
		await page.goto('/admin/entities');
		await expect(page.locator('aside.sidebar').first()).toBeVisible();
		await expect(page.getByRole('heading', { name: 'Entities', exact: true })).toBeVisible();
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });
		await expectNoA11yViolations(page);

		await page.getByRole('button', { name: '+ Create entity' }).click();
		await expect(page.getByRole('dialog', { name: 'Create entity' })).toBeVisible();
		await expectNoA11yViolations(page);
	});
});

test.describe('/admin/entities (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and every mutation 403s them', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/entities');
		// admin-only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Entities' })).toHaveCount(0);

		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const headers = {
			Authorization: `Bearer ${token}`,
			'X-Tenant-Slug': currentTenantSlug()
		};

		// The LIST is deliberately open to any authenticated user — the sidebar
		// entity switcher reads it — so this is a 200, not a 403.
		const list = await page.request.get(`${API_BASE}/api/entities`, { headers });
		expect(list.status()).toBe(200);
		const rows = (await list.json()) as EntityRow[];
		expect(rows.length).toBeGreaterThan(0);

		// Every mutation 403s.
		const create = await page.request.post(`${API_BASE}/api/entities`, {
			headers,
			data: { name: 'Clerk Attempt', slug: `clerk-attempt-${uniqueSuffix()}` }
		});
		expect(create.status()).toBe(403);

		const patch = await page.request.patch(`${API_BASE}/api/entities/${rows[0].id}`, {
			headers,
			data: { name: 'Clerk Rename' }
		});
		expect(patch.status()).toBe(403);

		const setDefault = await page.request.post(
			`${API_BASE}/api/entities/${rows[0].id}/set-default`,
			{ headers }
		);
		expect(setDefault.status()).toBe(403);
	});
});
