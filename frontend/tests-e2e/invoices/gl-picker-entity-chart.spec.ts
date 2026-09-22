import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	deleteVendorsWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * The invoice GL pickers offer only the invoice's OWN chart.
 *
 * An invoice's GL code resolves in the chart of the entity the invoice belongs
 * to — shared accounts ∪ that entity's own (`docs/multi-entity.md` § Chart of
 * accounts). In the consolidated view `GET /api/gl-accounts` returns every
 * subsidiary's chart at once, and both invoice pickers used to offer all of
 * it: subsidiary B's code for a subsidiary-A invoice, which the backend now
 * refuses (`backend/app/services/gl_chart.py`,
 * `backend/tests/test_gl_code_entity_chart.py`). Round 26 made the ambiguity
 * visible with an entity suffix on the label; this pins that the wrong
 * entity's option is not there to pick at all.
 *
 * The spec makes the worker tenant multi-entity for its duration — a second
 * entity, a shared GL account plus one in each entity's own chart, and one
 * invoice — and removes all of it after, in FK order (invoice and its
 * auto-linked vendor, the accounts, then the entity). Neither `/api/entities` nor `/api/gl-accounts`
 * has a DELETE, which is why the teardown is SQL.
 */

const MARK = 'GLE2E';
const ENTITY_SLUG_PREFIX = 'e2e-glchart';

interface Fixture {
	defaultEntityId: string;
	sharedCode: string;
	ownCode: string;
	foreignCode: string;
	invoiceId: string;
}

async function seed(page: Page): Promise<Fixture> {
	const suffix = Date.now().toString(36);
	await page.goto('/');
	await expect(page.locator('aside.sidebar')).toBeVisible();
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };

	const sub = await page.request.post(`${API_BASE}/api/entities`, {
		headers,
		data: { name: `${MARK} Sub ${suffix}`, slug: `${ENTITY_SLUG_PREFIX}-${suffix}` }
	});
	expect(sub.ok(), await sub.text()).toBe(true);
	const subId = ((await sub.json()) as { id: string }).id;

	const entities = (await (
		await page.request.get(`${API_BASE}/api/entities`, { headers })
	).json()) as { id: string; is_default: boolean }[];
	const defaultEntityId = entities.find((e) => e.is_default)!.id;

	// One shared account (consolidated create) and one in each entity's OWN
	// chart (created with that entity selected — the backend decides the chart
	// from `X-Entity-ID`, not the body).
	const sharedCode = `${MARK}-S-${suffix}`;
	const ownCode = `${MARK}-A-${suffix}`;
	const foreignCode = `${MARK}-B-${suffix}`;
	for (const [code, entityId] of [
		[sharedCode, null],
		[ownCode, defaultEntityId],
		[foreignCode, subId]
	] as const) {
		const r = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers: entityId ? { ...headers, 'X-Entity-ID': entityId } : headers,
			data: { code, name: `${code} name` }
		});
		expect(r.ok(), await r.text()).toBe(true);
	}

	// Consolidated create → filed under the default entity.
	const inv = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: {
			vendor: `${MARK} Vendor ${suffix}`,
			invoice_number: `${MARK}-INV-${suffix}`,
			amount: '42.00'
		}
	});
	expect(inv.ok(), await inv.text()).toBe(true);
	const created = (await inv.json()) as { id: string; entity_id: string | null };
	expect(created.entity_id).toBe(defaultEntityId);

	return { defaultEntityId, sharedCode, ownCode, foreignCode, invoiceId: created.id };
}

/** The option VALUES of the header GL select inside `scope`. */
async function glOptionValues(scope: ReturnType<Page['locator']>): Promise<string[]> {
	const select = scope.locator('label', { hasText: 'GL Account' }).first().locator('select');
	await expect(select).toBeVisible();
	return select
		.locator('option')
		.evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
}

test.describe('invoice GL pickers are scoped to the invoice entity chart', () => {
	test.afterEach(() => {
		deleteInvoicesWhere(`invoice_number LIKE '${MARK}-INV-%'`);
		deleteVendorsWhere(`name LIKE '${MARK} Vendor %'`);
		tenantPsql(`DELETE FROM gl_accounts WHERE code LIKE '${MARK}-%'`);
		tenantPsql(`DELETE FROM entities WHERE slug LIKE '${ENTITY_SLUG_PREFIX}-%'`);
	});

	test('the edit modal offers its own chart, not a sibling subsidiary', async ({ page }) => {
		const fx = await seed(page);

		const chart = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes(`chart_entity_id=${fx.defaultEntityId}`)
		);
		await page.goto(`/invoices?id=${fx.invoiceId}`);
		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();
		await chart;

		await expect.poll(() => glOptionValues(modal)).toContain(fx.ownCode);
		const values = await glOptionValues(modal);
		// A shared account is in every entity's chart.
		expect(values).toContain(fx.sharedCode);
		expect(values).not.toContain(fx.foreignCode);
	});

	test('the create modal offers the chart the new invoice will be filed under', async ({
		page
	}) => {
		const fx = await seed(page);
		await page.goto('/invoices');

		const chart = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
				r.url().includes(`chart_entity_id=${fx.defaultEntityId}`)
		);
		await page.getByRole('button', { name: 'Create Invoice' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
		await expect(modal).toBeVisible();
		await chart;

		await expect.poll(() => glOptionValues(modal)).toContain(fx.ownCode);
		const values = await glOptionValues(modal);
		expect(values).toContain(fx.sharedCode);
		expect(values).not.toContain(fx.foreignCode);
	});
});
