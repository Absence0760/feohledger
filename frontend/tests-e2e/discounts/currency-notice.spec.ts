import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /discounts — the foreign-currency exclusion notice.
 *
 * The optimizer sums money across offers, and a sum across currencies is not a
 * number, so an offer denominated in anything other than the org's reporting
 * currency is excluded from `total_savings_*` / `total_outlay_selected` /
 * `projected_savings` and reported as a COUNT instead
 * (`DiscountDashboard.unconvertible_offer_count`,
 * `OptimizerResponse.unconvertible_count` — two responses, two field names).
 *
 * That makes the figures honest but silently low: a multi-currency tenant sees
 * a projected-savings number smaller than the offers listed under it imply,
 * with nothing on the page accounting for the gap. These tests lock the notice
 * that closes it — present with its count and currency when the backend
 * excluded something, absent when it excluded nothing (a standing amber banner
 * nobody can clear is its own kind of noise).
 *
 * Modelled on the /cfo cash-position card's `unconverted-outflows` notice,
 * which reports the mirror-image gap on the outflow side.
 */

interface Dashboard {
	currency: string;
	unconvertible_offer_count: number;
}

async function readDashboard(page: import('@playwright/test').Page): Promise<Dashboard> {
	const resp = await page.request.get(`${API_BASE}/api/discounts/dashboard`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status(), await resp.text()).toBe(200);
	return (await resp.json()) as Dashboard;
}

function cleanupInvoice(id: string): void {
	tenantPsql(
		`DELETE FROM workflow_steps WHERE instance_id IN (SELECT id FROM workflow_instances WHERE invoice_id='${id}')`
	);
	tenantPsql(`DELETE FROM workflow_instances WHERE invoice_id='${id}'`);
	tenantPsql(`DELETE FROM exceptions WHERE invoice_id='${id}'`);
	deleteInvoicesWhere(`id='${id}'`);
}

test.describe('/discounts foreign-currency notice', () => {
	test('no notice while every open offer is in the reporting currency', async ({ page }) => {
		const dash = await readDashboard(page);
		// State the premise rather than assuming it: if a foreign open offer is
		// lying around, this test's subject is the OTHER case and the failure
		// should say so instead of silently asserting the wrong thing.
		expect(
			dash.unconvertible_offer_count,
			'baseline expects no foreign-currency open offers in this tenant'
		).toBe(0);

		await page.goto('/discounts');
		await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();
		await expect(page.locator('.kpi').first()).toBeVisible();
		await expect(page.getByTestId('unconvertible-offers')).toHaveCount(0);
	});

	test('an offer in another currency explains the shortfall on both surfaces', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const before = await readDashboard(page);
		// A currency the totals are definitively NOT in, whatever this tenant
		// reports in.
		const foreign = before.currency === 'EUR' ? 'JPY' : 'EUR';

		const vendors = await page.request.get(`${API_BASE}/api/vendors`, { headers });
		const vendor = ((await vendors.json()) as { items: { id: string; name: string }[] }).items[0];
		const invResp = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: {
				vendor: vendor.name,
				invoice_number: `DISC-FX-${Date.now()}-${Math.floor(Math.random() * 1e6)}`,
				amount: 1000,
				currency: 'USD'
			}
		});
		const invoiceId = ((await invResp.json()) as { id: string }).id;
		tenantPsql(
			`UPDATE invoices SET status='approved', vendor_id='${vendor.id}' WHERE id='${invoiceId}'`
		);

		// One reference date for the offer window and the net due date — see
		// `money-path.spec.ts::referenceDate` for why two clocks is a bug.
		const ref = tenantPsql('SELECT CURRENT_DATE').match(/\d{4}-\d{2}-\d{2}/)![0];
		const offerResp = await page.request.post(`${API_BASE}/api/discounts/offers`, {
			headers,
			data: {
				scope: 'invoice',
				invoice_id: invoiceId,
				tiers: [{ days: 10, percent: '2.00' }],
				currency: foreign,
				valid_from: ref
			}
		});
		expect(offerResp.status(), await offerResp.text()).toBe(201);
		const offerId = ((await offerResp.json()) as { id: string }).id;
		tenantPsql(`UPDATE invoices SET due_date = DATE '${ref}' + 30 WHERE id='${invoiceId}'`);

		try {
			// The backend really did exclude it — the notice is reporting a fact,
			// not decorating one.
			const after = await readDashboard(page);
			expect(after.unconvertible_offer_count).toBeGreaterThan(
				before.unconvertible_offer_count
			);

			await page.goto('/discounts');
			const notice = page.getByTestId('unconvertible-offers');
			await expect(notice).toBeVisible();
			// Both load-bearing halves: how many offers, and which currency the
			// totals above are in.
			await expect(notice).toContainText(`${after.unconvertible_offer_count} offer`);
			await expect(notice).toContainText(after.currency);

			// The optimizer response carries its own count on its own field name,
			// and its three totals are the ones the panel prints.
			const optimize = page.waitForResponse((r) => r.url().includes('/api/discounts/optimize'));
			await page.getByRole('button', { name: 'Optimize' }).click();
			const optimizeBody = (await (await optimize).json()) as {
				currency: string;
				unconvertible_count: number;
			};
			expect(optimizeBody.unconvertible_count).toBeGreaterThan(0);

			const optNotice = page.getByTestId('unconvertible-optimizer');
			await expect(optNotice).toBeVisible();
			await expect(optNotice).toContainText(`${optimizeBody.unconvertible_count} offer`);
			await expect(optNotice).toContainText(optimizeBody.currency);
		} finally {
			tenantPsql(`DELETE FROM discount_offers WHERE id='${offerId}'`);
			cleanupInvoice(invoiceId);
		}
	});
});
