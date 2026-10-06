import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /payments cards-tab pagination. The cards tab previously fetched every card;
 * it now uses the shared Load-More at page_size=20. We bulk-insert virtual
 * cards (against stub invoices) past the boundary and assert the contract.
 * Cards isn't the default tab (queue is), so the test switches to it first.
 *
 * Real card issuance needs an approved invoice + a card-adapter round trip, so
 * the fixture inserts rows directly — the list/pagination path under test
 * doesn't care how the cards were created, only that there are >page_size of
 * them.
 *
 * `uq_virtual_cards_one_live_per_invoice` enforces at most one active card per
 * invoice, so we seed one stub invoice per card via a `generate_series` cross-
 * join. The stub invoices use a `CARD-STUB-` number prefix so `purge()` can
 * clean both the cards and the invoices atomically.
 */

const CARD_MARKER = 'PAGE-CARD-';
const INV_MARKER = 'CARD-STUB-';

function getOrgId(): string {
	return tenantPsql(`SELECT organization_id FROM invoices LIMIT 1`).trim();
}

function seedCards(n: number): void {
	const orgId = getOrgId();
	// One stub invoice per card to satisfy uq_virtual_cards_one_live_per_invoice
	// (one active card per invoice). The CARD-STUB- prefix lets purge() identify
	// and remove them. All NOT NULL columns must be provided: correlation_id,
	// vendor_name, amount, currency, status, organization_id.
	tenantPsql(
		`INSERT INTO invoices (id, organization_id, correlation_id, invoice_number, vendor_name, amount, currency, status, created_at, updated_at)
		 SELECT gen_random_uuid(), '${orgId}', gen_random_uuid(), '${INV_MARKER}' || g, 'Card Stub Vendor', 1.00, 'USD', 'new', now(), now()
		 FROM generate_series(1, ${n}) g`
	);
	tenantPsql(
		`INSERT INTO virtual_cards (id, invoice_id, organization_id, card_provider, provider_card_id, amount_limit, currency, status, last_four, created_at, updated_at)
		 SELECT gen_random_uuid(), i.id, i.organization_id, 'mock', '${CARD_MARKER}' || row_number() OVER (), 500.00, 'USD', 'active', '4242', now(), now()
		 FROM invoices i WHERE i.invoice_number LIKE '${INV_MARKER}%'`
	);
}

function purge(): void {
	tenantPsql(`DELETE FROM virtual_cards WHERE provider_card_id LIKE '${CARD_MARKER}%'`);
	deleteInvoicesWhere(`invoice_number LIKE '${INV_MARKER}%'`);
}

test.describe('/payments cards pagination', () => {
	test.afterEach(() => purge());

	test('cards tab Load more appends the next page', async ({ page }) => {
		seedCards(22);

		await page.goto('/payments');
		// The wait is armed BEFORE the click, not after it. `waitForResponse`
		// only observes responses that arrive after it is called, so awaiting it
		// afterwards loses a race the local dev stack wins routinely — the fetch
		// completes before the listener attaches and the test times out having
		// missed the very response it was waiting for. The Load-more step below
		// already used this shape; the tab click did not.
		const firstPage = page.waitForResponse(
			(r) => r.url().includes('/api/cards?') && r.url().includes('page=1')
		);
		await page.getByRole('tab', { name: 'Cards', exact: true }).click();
		await firstPage;

		// Scoped to the cards table: the Cards tab also renders a rebates table
		// above it, so an unscoped `table tbody tr` counts both and the page-size
		// assertion silently measures the wrong thing.
		//
		// Counts go through `toHaveCount`, never a one-shot `.count()`: the
		// response resolving is not the rows rendering, so a bare count taken
		// right after it can read the previous page's rows (or none) and fail
		// — or pass — on timing alone.
		const cardsTable = page.getByTestId('cards-table');
		const rows = cardsTable.locator('tbody tr');
		await expect(rows).toHaveCount(20);

		const loadMore = page.getByRole('button', { name: /Load more/ });
		await expect(loadMore).toBeVisible();
		const total = Number((await loadMore.textContent())?.match(/of\s+(\d+)/)?.[1]);
		expect(total).toBeGreaterThanOrEqual(22);

		const next = page.waitForResponse(
			(r) => r.url().includes('/api/cards?') && r.url().includes('page=2')
		);
		await loadMore.click();
		await next;
		await expect(rows).toHaveCount(Math.min(total, 40));
	});

	test('API default page size is 20', async ({ page }) => {
		seedCards(25);
		const resp = await page.request.get(`${API_BASE}/api/cards`, {
			headers: await authedTenantHeaders(page)
		});
		const body = (await resp.json()) as { items: unknown[]; total: number; page_size: number };
		expect(body.page_size).toBe(20);
		expect(body.items.length).toBeLessThanOrEqual(20);
		expect(body.total).toBeGreaterThanOrEqual(25);
	});
});
