import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /goods-receipts — list page + detail modal.
 *
 * The seed creates 2 GRs per tenant (linked to existing POs).
 * Endpoint paginates at 20/page (well above seed count).
 */

test.describe('/goods-receipts', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/goods-receipts');
	});

	test('renders the seeded goods receipts with their PO numbers', async ({ page }) => {
		await expect(page.getByRole('heading', { name: 'Goods Receipts' })).toBeVisible();
		// `tr.clickable` is a real receipt row — the bare `tbody tr` also
		// matches DataTable's "Loading…" placeholder before the list lands.
		const rows = page.locator('table tbody tr.clickable');
		await expect(rows.first()).toBeVisible({ timeout: 5_000 });
		const total = await rows.count();
		expect(total).toBeGreaterThan(0);

		// Every seeded GR is linked to a PO, so the PO column should be
		// populated (not '—') on at least the first row.
		const firstPo = await rows.first().locator('td.mono').nth(1).textContent();
		expect(firstPo?.trim()).not.toBe('—');
	});

	test('clicking a row opens the detail modal with line items', async ({ page }) => {
		await page.locator('table tbody tr.clickable').first().click();

		const modal = page.locator('div.modal[role="dialog"][aria-label="Goods receipt"]');
		await expect(modal).toBeVisible({ timeout: 5_000 });
		await expect(modal.locator('h2')).toHaveText('Goods Receipt');
		await expect(modal.getByRole('heading', { name: 'Line Items Received' })).toBeVisible();
		const lineRows = modal.locator('.line-table tbody tr');
		expect(await lineRows.count()).toBeGreaterThan(0);

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();
	});

	test('GET /api/goods-receipts/{id} returns the matching GR', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const list = await page.request.get(`${API_BASE}/api/goods-receipts`, { headers });
		const listBody = (await list.json()) as { items: Array<{ id: string; gr_number: string }> };
		expect(listBody.items.length).toBeGreaterThan(0);

		const target = listBody.items[0];
		const detail = await page.request.get(`${API_BASE}/api/goods-receipts/${target.id}`, {
			headers
		});
		expect(detail.status()).toBe(200);
		const body = (await detail.json()) as {
			id: string;
			gr_number: string;
			line_items: unknown[];
			po_number: string | null;
		};
		expect(body.id).toBe(target.id);
		expect(body.gr_number).toBe(target.gr_number);
		expect(Array.isArray(body.line_items)).toBe(true);
	});

	test('GET /api/goods-receipts/{id} returns 404 for an unknown id', async ({ page }) => {
		const resp = await page.request.get(
			`${API_BASE}/api/goods-receipts/00000000-0000-0000-0000-000000000000`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(resp.status()).toBe(404);
	});

	test('list endpoint supports filtering by po_id', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const all = await page.request.get(`${API_BASE}/api/goods-receipts`, { headers });
		const allBody = (await all.json()) as {
			items: Array<{ id: string; po_id: string | null }>;
			total: number;
		};
		const target = allBody.items.find((g) => g.po_id);
		expect(target?.po_id).toBeTruthy();

		const filtered = await page.request.get(
			`${API_BASE}/api/goods-receipts?po_id=${target!.po_id}`,
			{ headers }
		);
		const filteredBody = (await filtered.json()) as {
			items: Array<{ po_id: string | null }>;
		};
		// Every returned row should match the filter.
		for (const gr of filteredBody.items) {
			expect(gr.po_id).toBe(target!.po_id);
		}
	});
});
