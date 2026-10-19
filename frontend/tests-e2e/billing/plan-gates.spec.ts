import type { Page } from '@playwright/test';

import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * Plan-gated settings show an upgrade prompt instead of a control the server
 * would refuse with a 402 (docs/decisions.md §253 the tiers, §258 the gates,
 * §281 Free's direct ERP and the Scale-only Merge.dev route).
 *
 * Every e2e worker tenant is seeded on `scale`, which grants every feature, so
 * the real `/api/auth/me` drives the "entitled" side. The FREE and GROWTH sides
 * stub `/api/auth/me`'s `entitlements` — the real response with that one field
 * overridden, like `auth/profile-no-password.spec.ts` — rather than moving the
 * worker's subscription, which would outlive a crashed test and leak into the
 * billing specs. The server-side refusals themselves are pytest's
 * (`backend/tests/test_plan_feature_gates.py`); this pins only what the browser
 * shows.
 */

const FREE = ['erp_integrations'];
const GROWTH = ['erp_integrations', 'public_api', 'sso'];

async function stubEntitlements(page: Page, entitlements: string[]): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/auth/me',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			await route.fulfill({ response: real, json: { ...body, entitlements } });
		}
	);
}

test.describe('plan-gated settings', () => {
	test('a tenant whose plan grants everything sees the controls, not the prompt', async ({
		page
	}) => {
		await page.goto('/admin/entities');
		await expect(page.getByRole('button', { name: '+ Create entity' })).toBeVisible();
		await expect(page.getByTestId('entities-plan-upgrade')).toHaveCount(0);

		await page.goto('/organization?section=sso');
		await expect(page.getByTestId('sso-panel').getByLabel('Enable single sign-on')).toBeEnabled();
		await expect(page.getByTestId('sso-plan-upgrade')).toHaveCount(0);
		await expect(page.getByTestId('sso-only-plan-upgrade')).toHaveCount(0);
	});

	test('on Free, each gated setting names its tier and links to billing', async ({ page }) => {
		await stubEntitlements(page, FREE);

		await page.goto('/admin/entities');
		const entities = page.getByTestId('entities-plan-upgrade');
		await expect(entities).toBeVisible();
		await expect(entities.getByText('Available on Scale')).toBeVisible();
		await expect(entities.getByRole('link', { name: 'Upgrade to Scale' })).toHaveAttribute(
			'href',
			'/billing'
		);
		await expect(page.getByRole('button', { name: '+ Create entity' })).toHaveCount(0);
		await expectNoA11yViolations(page);

		await page.goto('/admin/api-keys');
		await expect(
			page.getByTestId('api-keys-plan-upgrade').getByText('Available on Growth')
		).toBeVisible();

		await page.goto('/admin/webhooks');
		await expect(
			page.getByTestId('webhooks-plan-upgrade').getByText('Available on Growth')
		).toBeVisible();

		// Free carries the direct ERPs, so the ERP form is open — but Merge.dev
		// is a Scale connection.
		await page.goto('/organization?section=erp');
		const erp = page.getByTestId('erp-panel');
		await expect(erp.getByTestId('erp-provider-select')).toBeVisible();
		await expect(page.getByTestId('erp-plan-upgrade')).toHaveCount(0);
		await erp.getByTestId('erp-provider-select').selectOption('netsuite');
		await expect(erp.getByRole('button', { name: 'Test Connection' })).toBeVisible();
		await expect(erp.getByTestId('erp-merge-plan-upgrade')).toHaveCount(0);
		await erp.getByTestId('erp-provider-select').selectOption('merge_dev');
		const merge = erp.getByTestId('erp-merge-plan-upgrade');
		await expect(merge.getByText('Available on Scale')).toBeVisible();
		await expect(erp.getByTestId('erp-merge-type')).toHaveCount(0);
		await expect(erp.getByRole('button', { name: 'Save ERP Settings' })).toHaveCount(0);
		await expectNoA11yViolations(page);

		await page.goto('/organization?section=sso');
		await expect(page.getByTestId('sso-plan-upgrade')).toBeVisible();
		// The worker's SSO is off, and turning it ON is what the plan gates.
		await expect(page.getByTestId('sso-enabled-toggle')).toBeDisabled();
	});

	test('on Free, a stored Merge.dev connection opens on the Scale notice', async ({ page }) => {
		// A Scale tenant that saved Merge and then dropped to Free: the panel
		// loads the stored choice, and offers no form to save it again. The
		// worker is really on Scale, so the save below is allowed; only the
		// browser's view of the plan is Free.
		await page.goto('/organization?section=erp');
		const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
		const saved = await page.request.patch(`${API_BASE}/api/organization`, {
			headers,
			data: { settings: { erp: { type: 'sage_intacct', integration_method: 'merge_dev' } } }
		});
		expect(saved.ok()).toBe(true);
		try {
			await stubEntitlements(page, FREE);
			await page.reload();
			const erp = page.getByTestId('erp-panel');
			await expect(erp.getByTestId('erp-provider-select')).toHaveValue('merge_dev');
			await expect(
				erp.getByTestId('erp-merge-plan-upgrade').getByText('Available on Scale')
			).toBeVisible();
			await expect(erp.getByRole('button', { name: 'Save ERP Settings' })).toHaveCount(0);
			await expect(erp.getByRole('button', { name: 'Test Connection' })).toHaveCount(0);
		} finally {
			const reset = await page.request.patch(`${API_BASE}/api/organization`, {
				headers,
				data: { settings: { erp: null } }
			});
			expect(reset.ok()).toBe(true);
		}
	});

	test('a negotiated plan without the direct ERPs names Free as the tier', async ({ page }) => {
		// No catalog plan leaves `erp_integrations` out since §281; an
		// operator-configured contract can, and its prompt must not send the
		// admin to a paid tier for something Free already has.
		await stubEntitlements(page, []);
		await page.goto('/organization?section=erp');
		const notice = page.getByTestId('erp-plan-upgrade');
		await expect(notice.getByText('Available on Free')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Test Connection' })).toHaveCount(0);
	});

	test('on Growth, Merge.dev is still a Scale connection', async ({ page }) => {
		await stubEntitlements(page, GROWTH);
		await page.goto('/organization?section=erp');
		const erp = page.getByTestId('erp-panel');
		await erp.getByTestId('erp-provider-select').selectOption('merge_dev');
		await expect(erp.getByTestId('erp-merge-plan-upgrade').getByText('Available on Scale')).toBeVisible();
	});

	test('on Growth, SSO is open but "require SSO" is a Scale feature', async ({ page }) => {
		await stubEntitlements(page, GROWTH);

		await page.goto('/organization?section=sso');
		await expect(page.getByTestId('sso-enabled-toggle')).toBeEnabled();
		await expect(page.getByTestId('sso-plan-upgrade')).toHaveCount(0);
		const enforcement = page.getByTestId('sso-only-plan-upgrade');
		await expect(enforcement).toBeVisible();
		await expect(enforcement.getByText('Available on Scale')).toBeVisible();
		await expect(page.getByTestId('sso-only-toggle')).toBeDisabled();
	});
});
