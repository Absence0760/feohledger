import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
,
	isVendorListResponse} from '../fixtures/helpers';

/**
 * /vendors — the vendor performance score panel in VendorModal.
 *
 * `GET /api/enrichment/vendors/{id}/score` is advisory + compute-on-read: it
 * derives an accuracy, a dispute and an on-time sub-score from the tenant's own
 * invoice / PO history, then reports a weight-renormalized composite over the
 * ones that HAVE data. Nothing is persisted and reading it changes nothing.
 *
 * What this spec is really guarding is that the number never appears alone. A
 * score attached to a business relationship with no visible inputs is worse
 * than no score — so each sub-score row must carry both the sample it was
 * computed over and the backend's own evidence sentence, and an N/A sub-score
 * must say why it is N/A rather than silently reading as zero.
 *
 * Each test provisions its own vendor + invoices so the counts asserted below
 * are exact, rather than depending on whatever the seed or a previous spec left
 * on a shared vendor.
 */

const VENDOR_MARKER = 'SCORE-TEST-';

async function createVendor(
	page: import('@playwright/test').Page,
	name: string
): Promise<{ id: string; name: string }> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status()).toBe(201);
	return (await resp.json()) as { id: string; name: string };
}

/** Create an invoice and hard-link it to `vendorId` (POST /api/invoices resolves
 *  the vendor by name, but we want the link to be unambiguous for the count). */
async function createLinkedInvoice(
	page: import('@playwright/test').Page,
	vendorName: string,
	vendorId: string,
	number: string
): Promise<string> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: vendorName, invoice_number: number, amount: '100.00', currency: 'USD' }
	});
	expect(resp.status()).toBe(201);
	const id = ((await resp.json()) as { id: string }).id;
	tenantPsql(`UPDATE invoices SET vendor_id='${vendorId}' WHERE id='${id}'`);
	return id;
}

/**
 * Remove a test vendor and everything that points at it.
 *
 * `deleteVendorsWhere` owns the vendor's whole child graph — including the
 * invoices this spec creates — so the ordering and the child list are not
 * this file's to maintain.
 */
function purgeVendor(vendorId: string): void {
	try {
		deleteVendorsWhere(`id='${vendorId}'`);
	} catch {
		/* best-effort */
	}
}

test.describe('/vendors performance score (admin)', () => {
	test('the score renders with the inputs that produced it', async ({ page }) => {
		await page.goto('/vendors');

		const vendor = await createVendor(page, `${VENDOR_MARKER}${Date.now()}`);
		try {
			// Two invoices, no exceptions → the dispute sub-score is computable
			// over a sample of exactly 2, which makes the composite non-null.
			await createLinkedInvoice(page, vendor.name, vendor.id, `SCORE-A-${Date.now()}`);
			await createLinkedInvoice(page, vendor.name, vendor.id, `SCORE-B-${Date.now()}`);

			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse(
				(r) => isVendorListResponse(r.url(), 'search=')
			);

			// Identity, not position. `/api/vendors/counts?search=` answers the
			// same predicate as the list and often lands first, so the wait above
			// used to resolve before the table had re-rendered and this clicked
			// whichever vendor the unfiltered page was showing — a vendor WITH
			// history, against a case whose whole point is a vendor without any.
			const row = page.locator('table tbody tr').first();
			await expect(row).toContainText(vendor.name);
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
			await expect(modal).toBeVisible();

			const panel = modal.locator('[data-testid="vendor-score"]');
			await expect(panel).toBeVisible();
			await expect(panel.getByRole('heading', { name: 'Performance score' })).toBeVisible();

			// The composite is a real number, not the "no history" placeholder.
			const composite = panel.locator('[data-testid="vendor-score-composite"]');
			await expect(composite).toBeVisible();
			await expect(composite).toHaveText(/^\d+(\.\d+)?$/);

			// ...and it is explained: the dispute row states the exact sample it
			// was computed over, in the backend's own words.
			const disputeRow = panel.locator('[data-testid="vendor-score-row-dispute"]');
			await expect(disputeRow).toContainText('Dispute-free');
			await expect(disputeRow).toContainText('0 of 2 invoices raised an exception');

			// An N/A sub-score says WHY it is N/A — this vendor has no PO with an
			// expected delivery date, so on-time is excluded from the composite
			// rather than dragging it down as a zero.
			const onTimeRow = panel.locator('[data-testid="vendor-score-row-on_time"]');
			await expect(onTimeRow).toContainText('On-time delivery');
			await expect(onTimeRow).toContainText('N/A');
			await expect(onTimeRow).toContainText('No POs with an expected delivery date');

			// The accuracy row is present with its own evidence sentence too —
			// no approvals yet, which is a stated fact rather than a blank cell.
			const accuracyRow = panel.locator('[data-testid="vendor-score-row-accuracy"]');
			await expect(accuracyRow).toContainText('No approved invoices yet');
		} finally {
			purgeVendor(vendor.id);
		}
	});

	test('a vendor with no history says so instead of showing a zero', async ({ page }) => {
		await page.goto('/vendors');

		const vendor = await createVendor(page, `${VENDOR_MARKER}EMPTY-${Date.now()}`);
		try {
			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse(
				(r) => isVendorListResponse(r.url(), 'search=')
			);

			// Identity, not position. `/api/vendors/counts?search=` answers the
			// same predicate as the list and often lands first, so the wait above
			// used to resolve before the table had re-rendered and this clicked
			// whichever vendor the unfiltered page was showing — a vendor WITH
			// history, against a case whose whole point is a vendor without any.
			const row = page.locator('table tbody tr').first();
			await expect(row).toContainText(vendor.name);
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
			await expect(modal).toBeVisible();

			const panel = modal.locator('[data-testid="vendor-score"]');
			await expect(panel).toBeVisible();
			// Composite is N/A — every sub-score is N/A, so there is nothing to
			// average. Rendering 0 here would read as "this vendor is terrible".
			await expect(panel.locator('[data-testid="vendor-score-composite"]')).toHaveText('N/A');
			await expect(panel).toContainText('No history to score this vendor on yet');
		} finally {
			purgeVendor(vendor.id);
		}
	});
});

test.describe('/vendors performance score (clerk has no access)', () => {
	// `_SCORE_ROLES` is admin / ap_manager / cfo — the clerk is excluded. `GET
	// /api/vendors` excludes the clerk too, so the modal is unreachable through
	// the UI for that role; the invariant is asserted against the API directly,
	// exactly as the sibling enrichment spec does.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk cannot call the vendor score API (403)', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const vendorId = tenantPsql(`SELECT id FROM vendors LIMIT 1`).trim();
		expect(vendorId).toMatch(/[0-9a-f-]{36}/);

		const resp = await page.request.get(
			`${API_BASE}/api/enrichment/vendors/${vendorId}/score`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(resp.status()).toBe(403);
	});
});
