import { API_BASE, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * /admin/health — background-sweep health (admin only).
 *
 * Surfaces `GET /api/health/sweeps` (`backend/app/api/health.py`), which had no
 * frontend caller. The public `GET /api/health` deliberately stays a static
 * `ok` and reports nothing about sweeps, so a misconfigured audit sink can't
 * drive a rolling restart (`backend/docs/background-sweeps.md`) — which makes
 * this page the only place a dead, stalled or repeatedly-failing sweep shows.
 */

test.describe('/admin/health (admin)', () => {
	test('renders the sweep roster with per-sweep state', async ({ page }) => {
		const health = page.waitForResponse(
			(r) => r.url().includes('/api/health/sweeps') && r.request().method() === 'GET'
		);
		await page.goto('/admin/health');
		expect((await health).status()).toBe(200);

		await expect(page.getByRole('heading', { name: 'Sweep Health' })).toBeVisible();
		await expect(page.getByTestId('sweep-health-loading')).toHaveCount(0, { timeout: 10_000 });

		// Aggregate summary.
		const summary = page.getByTestId('sweep-health-summary');
		await expect(summary).toBeVisible();
		await expect(summary.getByText('Overall')).toBeVisible();
		await expect(summary.getByText('Failure-alert streak')).toBeVisible();

		// Every known sweep is reported, enabled or not — `snapshot()` returns the
		// full roster so "supposed to be running and isn't" is visible.
		const rows = page.getByTestId('sweep-row');
		await expect(rows.first()).toBeVisible();
		await expect(page.locator('[data-testid="sweep-row"][data-sweep="audit-log-shipper"]')).toHaveCount(1);
	});

	test('reports a died sweep and its error class without leaking a message', async ({ page }) => {
		await page.route('**/api/health/sweeps', async (route) => {
			await route.fulfill({
				json: {
					state: 'failing',
					failure_alert_streak: 3,
					sweeps: [
						{
							name: 'audit-log-shipper',
							state: 'died',
							enabled: true,
							started_at: '2026-08-16T09:00:00Z',
							last_run_started_at: '2026-08-16T11:04:00Z',
							last_run_finished_at: '2026-08-16T11:04:02Z',
							last_outcome: 'error',
							last_error_class: 'ClientError',
							last_failure_count: 4,
							consecutive_failures: 37,
							total_runs: 128,
							total_failed_runs: 37,
							exit_error_class: 'ClientError'
						},
						{
							name: 'billing-dunning',
							state: 'disabled',
							enabled: false,
							started_at: null,
							last_run_started_at: null,
							last_run_finished_at: null,
							last_outcome: null,
							last_error_class: null,
							last_failure_count: 0,
							consecutive_failures: 0,
							total_runs: 0,
							total_failed_runs: 0,
							exit_error_class: null
						}
					]
				}
			});
		});
		await page.goto('/admin/health');
		await expect(page.getByTestId('sweep-health-loading')).toHaveCount(0, { timeout: 10_000 });

		const died = page.locator('[data-testid="sweep-row"][data-sweep="audit-log-shipper"]');
		await expect(died).toHaveAttribute('data-state', 'died');
		// State and last-outcome are separate badges, both alarm-toned here.
		await expect(died.locator('.badge.danger.state-died')).toBeVisible();
		await expect(died.locator('.badge.danger.outcome-error')).toBeVisible();
		await expect(died).toContainText('Error class: ClientError');
		await expect(died).toContainText('37');
		await expect(died).toContainText('37 failed of 128');

		// A disabled sweep is an expected state, not a fault — no alarm tone, and
		// it never ran, so the last-run cell says so.
		const off = page.locator('[data-testid="sweep-row"][data-sweep="billing-dunning"]');
		await expect(off.locator('.badge.neutral.state-disabled')).toBeVisible();
		await expect(off).toContainText('Never');

		// One sweep needs attention (died); the disabled one does not.
		const summary = page.getByTestId('sweep-health-summary');
		const attention = summary.locator('.kpi', { hasText: 'Needing attention' });
		await expect(attention).toContainText('1');
		await expect(attention).toHaveClass(/highlight-red/);
	});

	test('the loaded roster is axe-clean at WCAG 2.2 AA', async ({ page }) => {
		await page.goto('/admin/health');
		await expect(page.getByTestId('sweep-health-loading')).toHaveCount(0, { timeout: 10_000 });
		await expect(page.getByTestId('sweep-health-summary')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('surfaces a load failure with a retry rather than a blank page', async ({ page }) => {
		await page.route('**/api/health/sweeps', (route) => route.fulfill({ status: 500, body: '{}' }));
		await page.goto('/admin/health');
		const error = page.getByTestId('sweep-health-error');
		await expect(error).toBeVisible({ timeout: 10_000 });
		await expect(error.getByRole('button', { name: 'Retry' })).toBeVisible();
	});
});

test.describe('/admin/health (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/health');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Sweep Health' })).toHaveCount(0);
		// The nav row is admin-only too, so there is no dead end to click into.
		await expect(page.locator('aside.sidebar a[href="/admin/health"]')).toHaveCount(0);

		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/health/sweeps`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
	});
});
