import type { Locator, Page } from '@playwright/test';

import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * The recurring-template GL picker offers only the chart the template codes
 * against.
 *
 * Every invoice a template raises is coded with its `gl_account` and lands
 * under the template's entity, and the backend refuses — once, on save — any
 * code that is not an active account of THAT entity's chart
 * (`backend/app/services/gl_chart.py`, decisions §194/§199). The field used to
 * be free text, so the form let the user type a code it could have known was
 * wrong. It is now the invoice pickers' `<select>`
 * (`tests-e2e/invoices/gl-picker-entity-chart.spec.ts`): the template's OWN
 * entity on edit — read off the template, not the sidebar — and the entity a
 * create lands under (the selection, else the default) on create.
 *
 * The spec makes the worker tenant multi-entity for its duration — a second
 * entity, a shared GL account plus one in each entity's own chart, and a
 * template filed under the second entity — and removes all of it after, in FK
 * order. Neither `/api/entities` nor `/api/gl-accounts` has a DELETE, which is
 * why the teardown is SQL.
 */

const MARK = 'GLRE2E';
const ENTITY_SLUG_PREFIX = 'e2e-glrecur';

interface Fixture {
	defaultEntityId: string;
	subEntityId: string;
	sharedCode: string;
	defaultCode: string;
	subCode: string;
	templateId: string;
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
	const subEntityId = ((await sub.json()) as { id: string }).id;

	const entities = (await (
		await page.request.get(`${API_BASE}/api/entities`, { headers })
	).json()) as { id: string; is_default: boolean }[];
	const defaultEntityId = entities.find((e) => e.is_default)!.id;

	// One shared account (consolidated create) and one in each entity's OWN
	// chart — the backend decides the chart from `X-Entity-ID`, not the body.
	const sharedCode = `${MARK}-S-${suffix}`;
	const defaultCode = `${MARK}-A-${suffix}`;
	const subCode = `${MARK}-B-${suffix}`;
	for (const [code, entityId] of [
		[sharedCode, null],
		[defaultCode, defaultEntityId],
		[subCode, subEntityId]
	] as const) {
		const r = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers: entityId ? { ...headers, 'X-Entity-ID': entityId } : headers,
			data: { code, name: `${code} name` }
		});
		expect(r.ok(), await r.text()).toBe(true);
	}

	// A template filed under the SUBSIDIARY, coded to its own account.
	const tpl = await page.request.post(`${API_BASE}/api/recurring`, {
		headers: { ...headers, 'X-Entity-ID': subEntityId },
		data: {
			name: `${MARK} Rent ${suffix}`,
			amount: '100.00',
			currency: 'USD',
			cadence: 'monthly',
			day_of_period: 1,
			start_date: '2026-01-01',
			gl_account: subCode
		}
	});
	expect(tpl.ok(), await tpl.text()).toBe(true);
	const created = (await tpl.json()) as { id: string; entity_id: string | null };
	expect(created.entity_id).toBe(subEntityId);

	return { defaultEntityId, subEntityId, sharedCode, defaultCode, subCode, templateId: created.id };
}

function glSelect(dialog: Locator): Locator {
	return dialog.getByTestId('recurring-gl-select');
}

async function optionValues(select: Locator): Promise<string[]> {
	return select
		.locator('option')
		.evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
}

function chartResponse(page: Page, entityId: string) {
	return page.waitForResponse(
		(r) =>
			new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
			r.url().includes(`chart_entity_id=${entityId}`)
	);
}

test.describe('recurring-template GL picker is scoped to the template entity chart', () => {
	test.afterEach(() => {
		tenantPsql(`DELETE FROM recurring_invoice_templates WHERE name LIKE '${MARK} %'`);
		tenantPsql(`DELETE FROM gl_accounts WHERE code LIKE '${MARK}-%'`);
		tenantPsql(`DELETE FROM entities WHERE slug LIKE '${ENTITY_SLUG_PREFIX}-%'`);
	});

	test("edit offers the template's own entity chart, whatever the sidebar shows", async ({
		page
	}) => {
		const fx = await seed(page);

		// The consolidated view would offer every chart; the template is the
		// subsidiary's, so the picker must ask for THAT chart.
		const chart = chartResponse(page, fx.subEntityId);
		await page.goto(`/recurring?id=${fx.templateId}`);
		const dialog = page.getByRole('dialog', { name: 'Recurring template detail' });
		await expect(dialog).toBeVisible();
		await chart;

		const select = glSelect(dialog);
		await expect(select).toBeVisible();
		await expect.poll(() => optionValues(select)).toContain(fx.subCode);
		const values = await optionValues(select);
		expect(values).toContain(fx.sharedCode);
		expect(values).not.toContain(fx.defaultCode);
		// The stored code is what the select shows.
		await expect(select).toHaveValue(fx.subCode);
	});

	test('create offers the chart the new template will be filed under', async ({ page }) => {
		const fx = await seed(page);
		await page.goto('/recurring');

		// Nothing selected → a create lands under the default entity.
		const chart = chartResponse(page, fx.defaultEntityId);
		await page.getByRole('button', { name: '+ New template' }).click();
		const dialog = page.getByRole('dialog', { name: 'New recurring template' });
		await expect(dialog).toBeVisible();
		await chart;

		const select = glSelect(dialog);
		await expect(select).toBeVisible();
		await expect.poll(() => optionValues(select)).toContain(fx.defaultCode);
		const values = await optionValues(select);
		expect(values).toContain(fx.sharedCode);
		expect(values).not.toContain(fx.subCode);
	});
});
