import type { Page } from '@playwright/test';
import type { AnalyticsByEntity } from '$lib/types/analytics';

import { API_BASE, expect, test } from '../fixtures/helpers';

/**
 * /cfo — "By entity" consolidated-reporting section.
 *
 * The section (consolidated reporting ACROSS entities) self-hides for
 * single-entity tenants, mirroring the sidebar entity switcher. The seed
 * tenants ship with one Default entity, so this spec creates a second entity
 * via the API, then asserts the By-entity table renders on /cfo with the
 * consolidated cross-check row.
 *
 * Default storage state signs the worker's admin in, so admin reaches the
 * CFO surface (gated admin + cfo).
 */

const API = API_BASE;

async function createEntity(page: Page, name: string, slug: string): Promise<string> {
	return page.evaluate(
		async ({ api, name, slug }) => {
			const token = localStorage.getItem('auth_token');
			const tenant = window.location.hostname.split('.')[0];
			const res = await fetch(`${api}/api/entities`, {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json',
					Authorization: `Bearer ${token}`,
					'X-Tenant-Slug': tenant
				},
				body: JSON.stringify({ name, slug })
			});
			if (!res.ok) throw new Error(`create entity failed: ${res.status}`);
			return (await res.json()).id as string;
		},
		{ api: API, name, slug }
	);
}

test.describe('/cfo By-entity section', () => {
	test('renders the per-entity breakdown for a multi-entity tenant', async ({ page }) => {
		const suffix = `${Date.now().toString(36)}`;
		const name = `CFO Sub ${suffix}`;
		const slug = `cfo-sub-${suffix}`;

		// `createEntity` runs in the page context but only needs a document on the
		// tenant origin: it reads the storage-state token out of localStorage and
		// calls `fetch` itself, so it depends on nothing the page renders and
		// never needed a quiet network.
		await page.goto('/cfo');
		await createEntity(page, name, slug);

		// Reload so the entity store picks up the new entity and the section
		// stops self-hiding.
		const byEntityResp = page.waitForResponse((r) =>
			r.url().includes('/api/analytics/by-entity')
		);
		await page.reload();
		await byEntityResp;

		const section = page.getByTestId('by-entity-section');
		await expect(section).toBeVisible();
		await expect(section.getByRole('heading', { name: 'By entity' })).toBeVisible();
		// The new entity is one row; the consolidated cross-check is the total row.
		await expect(section.locator('tr', { hasText: name })).toBeVisible();
		await expect(section.locator('tr.be-total', { hasText: 'Consolidated' })).toBeVisible();
	});

	test('labels every figure by the currency the payload names, and Open POs by none', async ({
		page
	}) => {
		// The rows used to label the naive cross-currency `total_spend` with the
		// entity's configured `currency` — or `orgCurrency` (the seeded `USD`)
		// when it had none — so a EUR-reporting payload still rendered dollars.
		// Stubbed so the reporting currency deliberately disagrees with the
		// seeded org default; the second entity is real so the section renders.
		const suffix = `${Date.now().toString(36)}`;
		const name = `EUR Sub ${suffix}`;
		await page.goto('/cfo');
		const subId = await createEntity(page, name, `eur-sub-${suffix}`);

		const metrics = (spend: string, outstanding: string, po: string) => ({
			total_spend: '999999.00',
			reporting_total_spend: spend,
			reporting_total_spend_unconverted_count: 1,
			outstanding_amount: '999999.00',
			reporting_outstanding_amount: outstanding,
			reporting_currency: 'EUR',
			reporting_outstanding_unconverted_count: 0,
			invoice_count: 2,
			open_exceptions: 0,
			open_po_amount: po
		});
		const payload = {
			period_days: 365,
			period_start: '2025-09-21',
			entities: [
				{
					entity_id: subId,
					entity_name: name,
					entity_slug: `eur-sub-${suffix}`,
					currency: null,
					is_default: false,
					...metrics('1234.50', '200.00', '750.00')
				}
			],
			consolidated: metrics('1234.50', '200.00', '750.00')
		} satisfies AnalyticsByEntity;
		await page.route(
			(url) => url.pathname === '/api/analytics/by-entity',
			(route) => route.fulfill({ json: payload })
		);

		await page.reload();
		const row = page.getByTestId('by-entity-section').locator('tr', { hasText: name });
		await expect(row).toBeVisible();
		const cells = row.locator('td');
		// Spend + Outstanding: the reporting figure, in the reporting currency.
		await expect(cells.nth(1)).toContainText(/€|EUR/);
		await expect(cells.nth(1)).toContainText(/1[,.]?234[.,]50/);
		await expect(cells.nth(1)).not.toContainText('$');
		await expect(cells.nth(2)).toContainText(/€|EUR/);
		// Open POs: no currency recorded, so no symbol at all.
		await expect(cells.nth(5)).toHaveText(/^\s*750[.,]00\s*$/);

		const section = page.getByTestId('by-entity-section');
		await expect(section.getByTestId('unconverted-spend')).toContainText('face value');
		await expect(section.getByTestId('unconverted-outstanding')).toHaveCount(0);
		await expect(section.getByTestId('open-po-no-currency')).toBeVisible();
	});
});
