import { API_BASE, authedTenantHeaders, expect, tenantPsql, test } from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import { cleanup, createPo } from '../matching/setup';
import {
	disconnectCommerce,
	fakeCommerceState,
	recordReceipt,
	resetFakeCommerce,
	settledSyncs
} from './helpers';

/**
 * Commerce sync, Cin7 Core (decisions §273; backend/docs/commerce-integration.md
 * § Cin7 Core): the real `cin7` adapter against tools/fake-commerce
 * (`pnpm commerce:up`, :12113). Connect from Organization → Commerce with an
 * account id and application key, map a Cin7 location, record a receipt, and
 * see the line land as a completed Cin7 stock adjustment referenced by our
 * idempotency key; cancel the receipt and see it taken back.
 *
 * Fixture literals come from tools/fake-commerce/providers/cin7.py.
 */

const MAIN_WAREHOUSE = '4b5c1e9a-1d2f-4c3b-9a8e-0f1e2d3c4b5a';
const PRODUCT_1 = '11111111-1111-4111-8111-111111111111';

interface Cin7State {
	levels: Record<string, string>;
	stock_adjustments: { Reference: string; Status: string; Comment: string }[];
}

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

test.describe.serial('/organization commerce, Cin7 Core', () => {
	const created: { grIds: string[]; poIds: string[] } = { grIds: [], poIds: [] };

	test.beforeAll(async () => {
		await resetFakeCommerce();
	});

	test.beforeEach(async () => {
		await skipUnlessReachable(SERVICES.fakeCommerce);
	});

	test.afterAll(async ({ browser }) => {
		cleanup(created);
		const page = await browser.newPage();
		try {
			await page.goto('/');
			await disconnectCommerce(page);
		} finally {
			await page.close();
		}
	});

	test('connect, map a location, then a receipt and its cancellation reach Cin7', async ({
		page
	}) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('cin7');
		await panel.getByLabel('Account ID').fill('fake-cin7-account');
		await panel.getByLabel('Application key').fill('fake-cin7-key');
		await expect(panel.getByText('use a clearing account', { exact: false })).toBeVisible();
		await panel.getByTestId('commerce-save').click();

		// The key is write-only from here on.
		await expect(panel.getByLabel('Application key')).toHaveAttribute('data-secret-saved', 'true');

		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		// Main Warehouse and Shopfront; the fixed-asset location is not offered.
		const picker = locations.getByTestId('commerce-default-location');
		await expect(picker.locator('option', { hasText: 'Office equipment' })).toHaveCount(0);
		await picker.selectOption(MAIN_WAREHOUSE);
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 2.'
		);

		const { poId } = createPo({
			total: 1300,
			lines: [
				{ description: 'A racket', quantity: 10, unitPrice: 100, total: 1000, sku: 'CIN7-SKU-1' },
				{ description: 'B unknown', quantity: 10, unitPrice: 30, total: 300, sku: 'NO-SUCH-SKU' }
			]
		});
		created.poIds.push(poId);
		const [racket, unknown] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[racket, '2.5'],
			[unknown, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['CIN7-SKU-1', 'synced', null],
				['NO-SUCH-SKU', 'skipped', 'unmapped_sku']
			].sort()
		);
		const synced = rows.find((r) => r.status === 'synced');
		expect(synced?.provider_location_id).toBe(MAIN_WAREHOUSE);

		// Cin7 holds the fractional quantity, and the adjustment carries our key.
		let state = (await fakeCommerceState('cin7')) as unknown as Cin7State;
		expect(state.levels[`${PRODUCT_1}@${MAIN_WAREHOUSE}`]).toBe('2.5');
		expect(state.stock_adjustments).toHaveLength(1);
		expect(state.stock_adjustments[0].Status).toBe('COMPLETED');
		expect(state.stock_adjustments[0].Reference).toBe(`feoh:${synced?.gr_line_item_id}:receive`);

		// Cancelling the receipt takes the stock back off in Cin7.
		const cancel = await page.request.post(
			`${API_BASE}/api/goods-receipts/${receipt.id}/cancel`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(cancel.ok(), `cancel receipt (${cancel.status()})`).toBe(true);
		const after = await settledSyncs(page, receipt.id);
		const reverse = after.find((r) => r.direction === 'reverse' && r.sku === 'CIN7-SKU-1');
		expect(reverse?.status).toBe('synced');

		state = (await fakeCommerceState('cin7')) as unknown as Cin7State;
		expect(state.levels[`${PRODUCT_1}@${MAIN_WAREHOUSE}`]).toBe('0.0');
		expect(state.stock_adjustments.map((a) => a.Reference)).toEqual([
			`feoh:${synced?.gr_line_item_id}:receive`,
			`feoh:${synced?.gr_line_item_id}:reverse`
		]);
	});
});
