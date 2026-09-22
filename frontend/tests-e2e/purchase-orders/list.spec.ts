import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /purchase-orders — list page + detail modal.
 *
 * The seed creates 5 POs per tenant via scripts/seed.py. The list
 * endpoint paginates at 20/page (well above seed count) so the table
 * renders all of them on the first page.
 */

test.describe('/purchase-orders', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/purchase-orders');
	});

	test('renders the seeded POs', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Purchase Orders' })).toBeVisible();
		const rows = page.locator('table tbody tr');
		await expect(rows.first()).toBeVisible({ timeout: 5_000 });
		expect(await rows.count()).toBeGreaterThan(0);
	});

	test('search input filters the visible PO list', async ({ page }) => {
		// `.count()` is a one-shot read with no auto-wait of its own, and it is
		// the first thing this test does after the beforeEach navigation — so
		// gate on the first row being rendered rather than on a quiet network.
		await expect(page.locator('table tbody tr').first()).toBeVisible();
		const before = await page.locator('table tbody tr').count();
		// Pick the first row's PO number, search for a substring.
		const firstPoNumber = await page.locator('table tbody tr td.mono').first().textContent();
		expect(firstPoNumber).toBeTruthy();
		const stem = firstPoNumber!.trim().slice(0, -1); // drop the last char to keep it a substring

		const filtered = page.waitForResponse(
			// Pathname-exact: `/api/purchase-orders/counts` carries the same
			// `search=` and can answer first, which resolves this wait while the
			// table still holds the unfiltered result.
			(r) =>
				new URL(r.url()).pathname.endsWith('/api/purchase-orders') &&
				r.url().includes('search=')
		);
		await page.getByPlaceholder('Search PO number...').fill(stem);
		await filtered;

		const after = await page.locator('table tbody tr').count();
		expect(after).toBeGreaterThan(0);
		expect(after).toBeLessThanOrEqual(before);
	});

	test('clicking a row opens the detail modal with line items', async ({ page }) => {
		// Wait for a DATA row, not just any row. Clicking before the list has
		// landed hits whatever placeholder the table is showing, and the modal
		// then never opens — the failure reads as "the modal is missing" rather
		// than "there was nothing to click".
		const firstRow = page.locator('table tbody tr').filter({ has: page.locator('td.mono') }).first();
		await expect(firstRow).toBeVisible();
		await firstRow.click();

		const modal = page.locator('div.modal[role="dialog"][aria-label="Purchase order"]');
		await expect(modal).toBeVisible({ timeout: 5_000 });
		await expect(modal.locator('h2')).toHaveText('Purchase Order');
		await expect(modal.getByRole('heading', { name: 'Line Items' })).toBeVisible();
		// At least one line item row in the line-items table.
		const lineRows = modal.locator('.line-table').first().locator('tbody tr');
		expect(await lineRows.count()).toBeGreaterThan(0);

		// Linked invoices section is rendered (count may be 0).
		await expect(modal.getByRole('heading', { name: /Linked Invoices/ })).toBeVisible();

		// Close.
		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();
	});

	test('GET /api/purchase-orders/{id} returns the matching PO with linked invoices', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const list = await page.request.get(`${API_BASE}/api/purchase-orders`, { headers });
		const listBody = (await list.json()) as { items: Array<{ id: string; po_number: string }> };
		expect(listBody.items.length).toBeGreaterThan(0);

		const target = listBody.items[0];
		const detail = await page.request.get(`${API_BASE}/api/purchase-orders/${target.id}`, {
			headers
		});
		expect(detail.status()).toBe(200);
		const body = (await detail.json()) as {
			id: string;
			po_number: string;
			line_items: unknown[];
			linked_invoices: unknown[];
		};
		expect(body.id).toBe(target.id);
		expect(body.po_number).toBe(target.po_number);
		expect(Array.isArray(body.line_items)).toBe(true);
		expect(Array.isArray(body.linked_invoices)).toBe(true);
	});

	test('GET /api/purchase-orders/{id} returns 404 for an unknown id', async ({ page }) => {
		const resp = await page.request.get(
			`${API_BASE}/api/purchase-orders/00000000-0000-0000-0000-000000000000`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(resp.status()).toBe(404);
	});

	test('list endpoint returns paginated {items, total} shape', async ({ page }) => {
		const resp = await page.request.get(`${API_BASE}/api/purchase-orders?page_size=2`, {
			headers: await authedTenantHeaders(page)
		});
		expect(resp.status()).toBe(200);
		const body = (await resp.json()) as { items: Record<string, unknown>[]; total: number };
		expect(Array.isArray(body.items)).toBe(true);
		expect(body.items.length).toBeLessThanOrEqual(2);
		expect(typeof body.total).toBe('number');
		expect(body.total).toBeGreaterThanOrEqual(body.items.length);
		// Every row names its own currency — `null` included, never omitted
		// (decisions §197). The value depends on the seed, the key does not.
		for (const item of body.items) expect(item).toHaveProperty('currency');
	});

	test("labels each PO's figures by the PO's own currency, and bare when it has none", async ({
		page
	}) => {
		// Every row used to wear the org's currency: a EUR order read as dollars.
		// Stubbed so the three currencies — and a PO recording none — are
		// guaranteed on screen whatever the seed holds.
		const row = (id: string, po_number: string, total: number, currency: string | null) => ({
			id,
			po_number,
			vendor_id: null,
			vendor_name: 'Stub Vendor',
			total,
			currency,
			status: 'open',
			line_items: [],
			created_at: '2026-09-01T10:00:00'
		});
		const items = [
			row('00000000-0000-0000-0000-00000000e001', 'PO-CCY-EUR', 1234.5, 'EUR'),
			row('00000000-0000-0000-0000-00000000e002', 'PO-CCY-GBP', 99, 'GBP'),
			row('00000000-0000-0000-0000-00000000e003', 'PO-CCY-NONE', 750, null)
		];
		await page.route(
			(url) => url.pathname === '/api/purchase-orders',
			(route) =>
				route.fulfill({ json: { items, total: items.length, page: 1, page_size: 20 } })
		);
		await page.route(
			(url) => url.pathname === `/api/purchase-orders/${items[0].id}`,
			(route) =>
				route.fulfill({
					json: {
						...items[0],
						line_items: [
							{ id: 'li-1', description: 'Widget', quantity: 1, unit_price: 1234.5, total: 1234.5 }
						],
						linked_invoices: [
							{
								id: 'inv-1',
								invoice_number: 'INV-GBP-1',
								vendor_name: 'Stub Vendor',
								amount: 10,
								currency: 'GBP',
								status: 'approved'
							}
						]
					}
				})
		);
		await page.reload();

		const totalFor = (poNumber: string) =>
			page.locator('table tbody tr', { hasText: poNumber }).getByTestId('po-total');
		await expect(totalFor('PO-CCY-EUR')).toContainText(/€|EUR/);
		await expect(totalFor('PO-CCY-EUR')).not.toContainText('$');
		await expect(totalFor('PO-CCY-GBP')).toContainText(/£|GBP/);
		// No currency recorded: grouped digits, no symbol — not the org's.
		await expect(totalFor('PO-CCY-NONE')).toHaveText(/^\s*750[.,]00\s*$/);

		// The detail labels the total and every line with the PO's currency, and
		// a linked invoice with ITS own.
		await page.locator('table tbody tr', { hasText: 'PO-CCY-EUR' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Purchase order"]');
		await expect(modal.getByTestId('po-detail-total')).toContainText(/€|EUR/);
		await expect(modal.locator('.line-table').first().locator('tbody tr').first()).toContainText(
			/€|EUR/
		);
		await expect(
			modal.locator('.line-table').nth(1).locator('tbody tr', { hasText: 'INV-GBP-1' })
		).toContainText(/£|GBP/);
	});
});
