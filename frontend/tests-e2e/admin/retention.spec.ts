import { API_BASE, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * /admin/retention — SOX records-management retention policy (admin only).
 *
 * Surfaces the existing backend endpoints (`backend/app/api/retention.py`):
 *  - GET /api/retention-policy → effective per-class windows + platform default + sweep enabled
 *  - PUT /api/retention-policy → update one or more classes (months, > 0); audited
 *
 * Login model mirrors the sibling admin pages (`/admin/api-keys`,
 * `/admin/webhooks`): a deterministic explicit sign-in rather than the shared
 * storage-state cache, so the gated page is reliably authed before each test.
 */

interface RetentionPolicyResponse {
	policy: Record<string, number>;
	default_months: number;
	enabled: boolean;
}

async function apiHeaders(page: import('@playwright/test').Page) {
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	return {
		Authorization: `Bearer ${token}`,
		'X-Tenant-Slug': currentTenantSlug(),
		'Content-Type': 'application/json'
	};
}

test.describe('/admin/retention (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('shows the effective policy and saves an edited window', async ({ page }) => {
		// Read the current server-side value first so the edit is deterministic
		// (some invoices_months) regardless of what a prior run left behind.
		const headers = await apiHeaders(page);
		const before = (await (
			await page.request.get(`${API_BASE}/api/retention-policy`, { headers })
		).json()) as RetentionPolicyResponse;

		await page.goto('/admin/retention');
		await expect(page.getByRole('heading', { name: 'Retention Policy' })).toBeVisible();
		await expect(page.getByTestId('retention-loading')).toHaveCount(0, { timeout: 10_000 });

		const invoicesInput = page.getByTestId('retention-input-invoices');
		await expect(invoicesInput).toHaveValue(String(before.policy.invoices));

		// Save is disabled until the value actually changes.
		const saveBtn = page.getByRole('button', { name: 'Save changes' });
		await expect(saveBtn).toBeDisabled();

		const newValue = before.policy.invoices === 60 ? 72 : 60;
		await invoicesInput.fill(String(newValue));
		await expect(saveBtn).toBeEnabled();
		await saveBtn.click();

		await expect(page.getByText('Retention policy saved.')).toBeVisible({ timeout: 10_000 });
		await expect(saveBtn).toBeDisabled();

		// Persisted server-side — confirm via the API directly, then restore the
		// original value so the run is idempotent for the next test invocation.
		const after = (await (
			await page.request.get(`${API_BASE}/api/retention-policy`, { headers })
		).json()) as RetentionPolicyResponse;
		expect(after.policy.invoices).toBe(newValue);

		await page.request.put(`${API_BASE}/api/retention-policy`, {
			headers,
			data: { policy: { invoices: before.policy.invoices } }
		});
	});

	test('a cleared or zero window is refused at the field, not after a round trip', async ({
		page
	}) => {
		// The PUT accepts only a positive whole number of months. A cleared field
		// parsed to 0 and used to enable Save, so the refusal arrived as a toast
		// after the request; now Save stays off and the field says why.
		let puts = 0;
		page.on('request', (req) => {
			if (req.method() === 'PUT' && req.url().includes('/api/retention-policy')) puts += 1;
		});

		await page.goto('/admin/retention');
		await expect(page.getByTestId('retention-loading')).toHaveCount(0, { timeout: 10_000 });

		const input = page.getByTestId('retention-input-invoices');
		const saveBtn = page.getByRole('button', { name: 'Save changes' });

		await input.fill('');
		await expect(page.getByTestId('retention-error-invoices')).toHaveText(
			'Enter a whole number of months, 1 or more.'
		);
		await expect(input).toHaveAttribute('aria-invalid', 'true');
		await expect(saveBtn).toBeDisabled();

		await input.fill('0');
		await expect(saveBtn).toBeDisabled();

		await input.fill('12');
		await expect(page.getByTestId('retention-error-invoices')).toHaveCount(0);
		await expect(input).not.toHaveAttribute('aria-invalid', 'true');

		// Reset puts the saved value back without sending anything.
		await page.getByRole('button', { name: 'Reset' }).click();
		expect(puts).toBe(0);
	});

	test('reload reflects the persisted value', async ({ page }) => {
		const headers = await apiHeaders(page);
		const before = (await (
			await page.request.get(`${API_BASE}/api/retention-policy`, { headers })
		).json()) as RetentionPolicyResponse;

		await page.goto('/admin/retention');
		await expect(page.getByTestId('retention-input-audit_log')).toHaveValue(
			String(before.policy.audit_log)
		);
	});
});

test.describe('/admin/retention (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/retention');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Retention Policy' })).toHaveCount(0);

		const headers = await apiHeaders(page);
		const resp = await page.request.get(`${API_BASE}/api/retention-policy`, { headers });
		expect(resp.status()).toBe(403);
	});
});
