import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /payments history-tab pagination. The history tab previously fetched
 * page_size=100 and never paginated; it now uses the shared Load-More at
 * page_size=20. Bulk-insert payments (against a seeded invoice) past the
 * boundary and assert the contract. The History tab isn't the default
 * (queue is), so the test switches to it first.
 */

const MARKER = 'PAGE-PAY-';
const INV_MARKER = 'PAY-STUB-';

function getOrgId(): string {
	return tenantPsql(`SELECT organization_id FROM invoices LIMIT 1`).trim();
}

function seedPayments(n: number): void {
	const orgId = getOrgId();
	// One stub invoice per payment to satisfy uq_payments_one_live_per_invoice
	// (at most one LIVE payment per invoice — 'completed' counts as live, since
	// an invoice must not be paid twice). Stacking N completed payments on a
	// single invoice — as this helper used to — is exactly the double-pay the
	// index prevents. The PAY-STUB- prefix lets purge() remove both the stub
	// invoices and their payments. Mirrors cards-pagination.spec.ts.
	tenantPsql(
		`INSERT INTO invoices (id, organization_id, correlation_id, invoice_number, vendor_name, amount, currency, status, created_at, updated_at)
		 SELECT gen_random_uuid(), '${orgId}', gen_random_uuid(), '${INV_MARKER}' || g, 'Pay Stub Vendor', 100.00, 'USD', 'new', now(), now()
		 FROM generate_series(1, ${n}) g`
	);
	tenantPsql(
		`INSERT INTO payments (id, invoice_id, amount, method, status, reference, created_at, updated_at)
		 SELECT gen_random_uuid(), i.id, 100.00, 'ach', 'completed', '${MARKER}' || row_number() OVER (), now(), now()
		 FROM invoices i WHERE i.invoice_number LIKE '${INV_MARKER}%'`
	);
}

function purge(): void {
	tenantPsql(`DELETE FROM payments WHERE reference LIKE '${MARKER}%'`);
	deleteInvoicesWhere(`invoice_number LIKE '${INV_MARKER}%'`);
}

test.describe('/payments pagination', () => {
	test.afterEach(() => purge());

	test('history tab Load more appends the next page', async ({ page }) => {
		seedPayments(22);

		await page.goto('/payments');
		// The wait is armed BEFORE the click, not after it. `waitForResponse`
		// only observes responses that arrive after it is called, so awaiting it
		// afterwards loses a race the local dev stack wins routinely — the fetch
		// completes before the listener attaches and the test times out having
		// missed the very response it was waiting for. The Load-more step below
		// already used this shape; the tab click did not.
		//
		// The predicate matches on the PATHNAME, not a substring: the Queue tab
		// is the default and its mount fetch is `/api/payments/queue?page=1…`,
		// which `url().includes('/api/payments')` also matches. Only the exact
		// path distinguishes the History list from it, so the waiter cannot be
		// satisfied by whichever request happens to be in flight.
		const firstPage = page.waitForResponse(
			(r) => new URL(r.url()).pathname.endsWith('/api/payments') && r.url().includes('page=1')
		);
		await page.getByRole('tab', { name: /History/ }).click();
		await firstPage;

		const firstPageRows = await page.locator('table tbody tr').count();
		expect(firstPageRows).toBeLessThanOrEqual(20);

		const loadMore = page.getByRole('button', { name: /Load more/ });
		await expect(loadMore).toBeVisible();
		const total = Number((await loadMore.textContent())?.match(/of\s+(\d+)/)?.[1]);
		expect(total).toBeGreaterThanOrEqual(22);

		const next = page.waitForResponse(
			(r) => new URL(r.url()).pathname.endsWith('/api/payments') && r.url().includes('page=2')
		);
		await loadMore.click();
		await next;
		expect(await page.locator('table tbody tr').count()).toBeGreaterThan(firstPageRows);
	});

	test('API default page size is 20', async ({ page }) => {
		seedPayments(25);
		const resp = await page.request.get(`${API_BASE}/api/payments`, {
			headers: await authedTenantHeaders(page)
		});
		const body = (await resp.json()) as { items: unknown[]; total: number; page_size: number };
		expect(body.page_size).toBe(20);
		expect(body.items.length).toBeLessThanOrEqual(20);
		expect(body.total).toBeGreaterThanOrEqual(25);
	});
});
