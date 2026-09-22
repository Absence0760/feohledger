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
 * An invoice coded to an account that has since been RETIRED stays editable in
 * the invoice modal, and the modal never quietly re-uses that code for new work.
 *
 * Since `docs/decisions.md` §199 every invoice GL write must name an active
 * account of the invoice's chart whenever it has any
 * (`backend/app/services/gl_chart.py`) — but only a code the request SETS is
 * judged, so a stored code that was valid when it was written rides through an
 * unrelated edit. Two things in the modal had to agree with that:
 *
 * - a LINE carrying the retired code must show it: the line picker offers only
 *   the active chart, and without an option of its own the cell rendered blank
 *   while the row still carried (and re-saved) the code — the header picker
 *   already did this;
 * - a NEW line must not inherit the header's retired code: a new line naming
 *   it is a new coding decision the line-items save refuses, and the picker
 *   could not even show it, so the refusal named a code the user never saw.
 *
 * The spec builds its own chart rows (one live and one soon-retired shared
 * account) and invoice, codes the invoice while the account is live, retires
 * it through the real `PATCH /api/gl-accounts/{id}`, and removes everything
 * after. `/api/gl-accounts` has no DELETE, which is why that teardown is SQL.
 */

const MARK = 'GLR2E';

interface Fixture {
	liveCode: string;
	retiredCode: string;
	invoiceId: string;
}

async function seed(page: Page, opts: { withLine: boolean }): Promise<Fixture> {
	const suffix = Date.now().toString(36);
	await page.goto('/');
	await expect(page.locator('aside.sidebar')).toBeVisible();
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };

	const liveCode = `${MARK}-L-${suffix}`;
	const retiredCode = `${MARK}-R-${suffix}`;
	let retiredId = '';
	for (const code of [liveCode, retiredCode]) {
		// Consolidated view → a shared account, in every entity's chart.
		const r = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers,
			data: { code, name: `${code} name` }
		});
		expect(r.ok(), await r.text()).toBe(true);
		if (code === retiredCode) retiredId = ((await r.json()) as { id: string }).id;
	}

	// Coded while the account is live — a write the server accepts.
	const inv = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: {
			vendor: `${MARK} Vendor ${suffix}`,
			invoice_number: `${MARK}-INV-${suffix}`,
			amount: '42.00',
			gl_account: retiredCode
		}
	});
	expect(inv.ok(), await inv.text()).toBe(true);
	const invoiceId = ((await inv.json()) as { id: string }).id;

	if (opts.withLine) {
		const lines = await page.request.put(`${API_BASE}/api/invoices/${invoiceId}/line-items`, {
			headers,
			data: [{ description: 'Coded before retirement', total: '42.00', gl_account: retiredCode }]
		});
		expect(lines.ok(), await lines.text()).toBe(true);
	}

	// Now the account is retired.
	const retired = await page.request.patch(`${API_BASE}/api/gl-accounts/${retiredId}`, {
		headers,
		data: { is_active: false }
	});
	expect(retired.ok(), await retired.text()).toBe(true);

	return { liveCode, retiredCode, invoiceId };
}

/** Open the invoice's edit modal once its own chart has loaded. */
async function openModal(page: Page, invoiceId: string) {
	const chart = page.waitForResponse(
		(r) =>
			new URL(r.url()).pathname.endsWith('/api/gl-accounts') &&
			r.url().includes('chart_entity_id=')
	);
	await page.goto(`/invoices?id=${invoiceId}`);
	const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
	await expect(modal).toBeVisible();
	await chart;
	return modal;
}

function lineGlSelect(modal: ReturnType<Page['locator']>, n: number) {
	return modal.getByRole('combobox', { name: `Line ${n} GL account` });
}

function storedLineCodes(invoiceId: string): string[] {
	return tenantPsql(
		`SELECT COALESCE(gl_account, '') FROM invoice_line_items WHERE invoice_id = '${invoiceId}' ORDER BY line_number`
	)
		.trim()
		.split('\n');
}

test.describe('an invoice coded to a since-retired GL account', () => {
	test.afterEach(() => {
		deleteInvoicesWhere(`invoice_number LIKE '${MARK}-INV-%'`);
		deleteVendorsWhere(`name LIKE '${MARK} Vendor %'`);
		tenantPsql(`DELETE FROM gl_accounts WHERE code LIKE '${MARK}-%'`);
	});

	test('shows the retired code on its line and still saves an unrelated edit', async ({
		page
	}) => {
		const fx = await seed(page, { withLine: true });
		const modal = await openModal(page, fx.invoiceId);

		// The header and the line both show the code they carry, although the
		// active chart no longer offers it.
		const headerSelect = modal.locator('label', { hasText: 'GL Account' }).first().locator('select');
		await expect(headerSelect).toHaveValue(fx.retiredCode);
		await expect(lineGlSelect(modal, 1)).toHaveValue(fx.retiredCode);

		// An edit that does not touch the GL code goes through: the form echoes
		// the stored code back, and an unchanged code is not judged.
		await modal
			.locator('label', { hasText: 'Description' })
			.first()
			.locator('input')
			.fill('Re-described');
		const patched = page.waitForResponse(
			(r) =>
				r.request().method() === 'PATCH' &&
				new URL(r.url()).pathname === `/api/invoices/${fx.invoiceId}`
		);
		await modal.getByRole('button', { name: 'Save', exact: true }).click();
		expect((await patched).status()).toBe(200);
		expect(
			tenantPsql(`SELECT gl_account FROM invoices WHERE id = '${fx.invoiceId}'`).trim()
		).toBe(fx.retiredCode);
	});

	test('a new line does not inherit a header code the chart no longer offers', async ({
		page
	}) => {
		const fx = await seed(page, { withLine: false });
		const modal = await openModal(page, fx.invoiceId);

		await modal.getByRole('button', { name: '+ Add Line' }).click();
		await expect(lineGlSelect(modal, 1)).toHaveValue('');
		await modal.getByRole('textbox', { name: 'Line 1 description' }).fill('New line');

		const saved = page.waitForResponse(
			(r) =>
				r.request().method() === 'PUT' &&
				new URL(r.url()).pathname === `/api/invoices/${fx.invoiceId}/line-items`
		);
		await modal.getByRole('button', { name: 'Save Line Items' }).click();
		const resp = await saved;
		expect(resp.status(), await resp.text()).toBe(200);
		expect(storedLineCodes(fx.invoiceId)).toEqual(['']);

		// A live account is still offered to the new line, and saves.
		await lineGlSelect(modal, 1).selectOption(fx.liveCode);
		const recoded = page.waitForResponse(
			(r) =>
				r.request().method() === 'PUT' &&
				new URL(r.url()).pathname === `/api/invoices/${fx.invoiceId}/line-items`
		);
		await modal.getByRole('button', { name: 'Save Line Items' }).click();
		expect((await recoded).status()).toBe(200);
		expect(storedLineCodes(fx.invoiceId)).toEqual([fx.liveCode]);
	});
});
