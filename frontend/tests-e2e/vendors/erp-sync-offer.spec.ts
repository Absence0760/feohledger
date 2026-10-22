import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * "Sync from ERP" is offered only where it can work (#635 finding 21). It was
 * gated on role alone, so a workspace with no ERP got a button whose only
 * outcome was a 400 in an English toast — and the empty state recommended it.
 *
 * The page reads `settings.erp` from `GET /api/organization`, the field the
 * Organization → ERP panel reads, and the plan from `/api/auth/me`'s
 * `entitlements`. Both are stubbed as the real response with one field
 * overridden (the `billing/plan-gates.spec.ts` shape), so the worker's real
 * org and subscription are never touched. The server-side refusals are
 * pytest's; this pins what the browser offers.
 */

async function stubErp(page: Page, erp: Record<string, unknown> | null) {
	await page.route(
		(url) => url.pathname === '/api/organization',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			const settings = { ...(body.settings ?? {}) };
			if (erp) settings.erp = erp;
			else delete settings.erp;
			await route.fulfill({ response: real, json: { ...body, settings } });
		}
	);
}

async function stubEntitlements(page: Page, entitlements: string[]) {
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

async function stubEmptyTenant(page: Page) {
	await page.route(
		(url) => url.pathname === '/api/vendors',
		(route) => route.fulfill({ json: { items: [], total: 0 } })
	);
	await page.route(
		(url) => url.pathname === '/api/vendors/counts',
		(route) => route.fulfill({ json: { total: 0, by_status: {} } })
	);
}

const syncButton = (page: Page) => page.getByRole('button', { name: 'Sync from ERP' });

test.describe('/vendors ERP sync offer', () => {
	test('no ERP configured: a link to set one up, and no sync button', async ({ page }) => {
		await stubErp(page, null);
		await page.goto('/vendors');

		const connect = page.getByRole('link', { name: 'Connect an ERP' });
		await expect(connect).toHaveAttribute('href', '/organization?section=erp');
		await expect(syncButton(page)).toHaveCount(0);
	});

	test('no ERP configured: the fresh-tenant copy does not recommend syncing', async ({ page }) => {
		await stubErp(page, null);
		await stubEmptyTenant(page);
		await page.goto('/vendors');

		const empty = page.getByTestId('vendors-empty-state');
		await expect(empty).toContainText('or import a CSV');
		// Positive first (the ERP read has landed: the link is drawn), then absence.
		await expect(page.getByRole('link', { name: 'Connect an ERP' })).toBeVisible();
		await expect(empty).not.toContainText('ERP');
	});

	test('a direct ERP is offered on Free', async ({ page }) => {
		await stubEntitlements(page, ['erp_integrations']);
		await stubErp(page, { type: 'netsuite', integration_method: 'direct' });
		await stubEmptyTenant(page);
		await page.goto('/vendors');

		await expect(syncButton(page)).toBeVisible();
		await expect(page.getByTestId('vendors-empty-state')).toContainText('sync from your ERP');
		await expect(page.getByTestId('vendors-erp-upgrade')).toHaveCount(0);
	});

	test('a Merge.dev config without Scale shows the upgrade notice instead', async ({ page }) => {
		await stubEntitlements(page, ['erp_integrations']);
		await stubErp(page, { type: 'sage_intacct', integration_method: 'merge_dev' });
		await page.goto('/vendors');

		const notice = page.getByTestId('vendors-erp-upgrade');
		await expect(notice).toBeVisible();
		await expect(notice.getByText('Available on Scale')).toBeVisible();
		await expect(syncButton(page)).toHaveCount(0);
		await expect(page.getByRole('link', { name: 'Connect an ERP' })).toHaveCount(0);
	});

	test('a Merge.dev config on Scale is offered', async ({ page }) => {
		await stubErp(page, { type: 'sage_intacct', integration_method: 'merge_dev' });
		await page.goto('/vendors');
		await expect(syncButton(page)).toBeVisible();
	});
});
