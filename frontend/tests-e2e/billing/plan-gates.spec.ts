import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * Plan-gated settings show an upgrade prompt instead of a control the server
 * would refuse with a 402 (docs/decisions.md §253 the tiers, §258 the gates).
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
		await stubEntitlements(page, []);

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

		await page.goto('/organization?section=erp');
		await expect(page.getByTestId('erp-plan-upgrade')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Test Connection' })).toHaveCount(0);

		await page.goto('/organization?section=sso');
		await expect(page.getByTestId('sso-plan-upgrade')).toBeVisible();
		// The worker's SSO is off, and turning it ON is what the plan gates.
		await expect(page.getByTestId('sso-enabled-toggle')).toBeDisabled();
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
