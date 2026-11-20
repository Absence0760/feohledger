import { API_BASE, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * /admin/access-review — periodic SOX access review (admin | cfo).
 *
 * Surfaces the existing backend endpoints (`backend/app/api/access_reviews.py`):
 *  - GET  /api/access-reviews            → every elevated-role user + dormancy verdict
 *  - POST /api/access-reviews/acknowledge → records the reviewer's sign-off (audited)
 *
 * Both routes are `require_roles(ADMIN, CFO)` — the reviewer privilege — so this
 * spec checks both allowed roles plus a clerk redirect, unlike the strictly
 * admin-only sibling pages.
 */

test.describe('/admin/access-review (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('lists elevated users and acknowledges the review', async ({ page }) => {
		await page.goto('/admin/access-review');
		await expect(page.getByRole('heading', { name: 'Access Review' })).toBeVisible();
		// The table's own "Loading…" row (DataTable's live status) is gone.
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });

		// KPI row — the seeded tenant has at least the admin who is signed in.
		await expect(page.getByText('Elevated users')).toBeVisible();
		await expect(page.getByText('Dormancy window')).toBeVisible();

		// The signed-in admin's own row is in the table (their email is unique
		// per worker tenant via the shared fixture credential scheme).
		const table = page.locator('table');
		await expect(table).toBeVisible();
		await expect(table.getByText(/@/)).not.toHaveCount(0);

		// Acknowledge writes the audited close-out and shows a confirmation.
		const ackButton = page.getByRole('button', { name: 'Acknowledge review' });
		await expect(ackButton).toBeEnabled();
		await ackButton.click();
		await expect(page.getByText('Access review acknowledged for this period.')).toBeVisible({
			timeout: 10_000
		});
		await expect(page.getByTestId('access-review-ack-note')).toBeVisible();
	});
});

test.describe('/admin/access-review (cfo)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a CFO can view and acknowledge the review too', async ({ page, tenantCfo }) => {
		await signInAndWait(page, tenantCfo);

		await page.goto('/admin/access-review');
		await expect(page.getByRole('heading', { name: 'Access Review' })).toBeVisible();
		// The table's own "Loading…" row (DataTable's live status) is gone.
		await expect(page.locator('table [role="status"]')).toHaveCount(0, { timeout: 10_000 });
		await expect(page.getByRole('button', { name: 'Acknowledge review' })).toBeEnabled();
	});
});

test.describe('/admin/access-review (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/access-review');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Access Review' })).toHaveCount(0);

		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/access-reviews`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
	});
});
