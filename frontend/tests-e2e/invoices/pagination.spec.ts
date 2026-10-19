import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /invoices pagination. The lean seed has only ~10 invoices, so we bulk-insert
 * enough rows (via SQL — fast, no extraction side effects) to cross the
 * page_size=20 boundary, then assert the shared Load-More contract: 20 rows,
 * a "Load more (X of TOTAL)" button that appends page 2, and the API default
 * page size of 20.
 */

const MARKER = 'PAGE-INV-';

function seedInvoices(n: number): void {
	tenantPsql(
		`INSERT INTO invoices (id, correlation_id, organization_id, invoice_number, vendor_name, amount, currency, status, created_at, updated_at)
		 SELECT gen_random_uuid(), gen_random_uuid(), (SELECT organization_id FROM invoices LIMIT 1),
		        '${MARKER}' || g, 'Pagey Vendor', 100.00, 'USD', 'new', now(), now()
		 FROM generate_series(1, ${n}) g`
	);
}

function purge(): void {
	deleteInvoicesWhere(`invoice_number LIKE '${MARKER}%'`);
}

test.describe('/invoices pagination', () => {
	test.afterEach(() => purge());

	test('Load more appends the next page', async ({ page }) => {
		seedInvoices(22);

		await page.goto('/invoices');

		// `count()` does not auto-wait and `toBeLessThanOrEqual(20)` is satisfied
		// by zero rows, so anchor on the Load-more button first: it only renders
		// once page 1 has come back with more rows behind it, which is exactly
		// the "first page is on screen" signal this needs.
		const loadMore = page.getByRole('button', { name: /Load more/ });
		await expect(loadMore).toBeVisible();

		const firstPageRows = await page.locator('table tbody tr').count();
		expect(firstPageRows).toBeLessThanOrEqual(20);
		const total = Number((await loadMore.textContent())?.match(/of\s+(\d+)/)?.[1]);
		expect(total).toBeGreaterThanOrEqual(22);

		const next = page.waitForResponse(
			(r) => r.url().includes('/api/invoices') && r.url().includes('page=2')
		);
		await loadMore.click();
		await next;
		// The page-2 response landing is not the rows rendering: Svelte appends
		// them on the next tick, so a one-shot `count()` here raced the DOM and
		// read the first page back. Poll the rendered row count instead.
		await expect
			.poll(() => page.locator('table tbody tr').count())
			.toBeGreaterThan(firstPageRows);
	});

	test('API default page size is 20', async ({ page }) => {
		seedInvoices(25);
		const resp = await page.request.get(`${API_BASE}/api/invoices`, {
			headers: await authedTenantHeaders(page)
		});
		const body = (await resp.json()) as { items: unknown[]; total: number; page_size: number };
		expect(body.page_size).toBe(20);
		expect(body.items.length).toBeLessThanOrEqual(20);
		expect(body.total).toBeGreaterThanOrEqual(25);
	});
});
