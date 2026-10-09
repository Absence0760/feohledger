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
 * Commerce sync, WooCommerce (decisions §268; commerce-integration.md
 * § WooCommerce): the real adapter against tools/fake-commerce's
 * `woocommerce` module. Connect from Organization → Commerce (store URL +
 * consumer key/secret), map the one synthetic "Store" location, record a
 * receipt — a simple product, a variation, and a product whose stock
 * WooCommerce does not manage — then cancel it and see the stock come back.
 */

const STORE_URL = 'https://shop.example.com';
const CONSUMER_KEY = 'ck_fake_woocommerce';
const CONSUMER_SECRET = 'cs_fake_woocommerce';

type WooState = {
	products: Record<string, { stock_quantity: number | null }>;
	query_auth_requests: number;
};

async function stock(productId: number): Promise<number | null> {
	const state = (await fakeCommerceState('woocommerce')) as WooState;
	return state.products[String(productId)].stock_quantity;
}

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

test.describe('/organization commerce, WooCommerce', () => {
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

	test('connect, push a receipt, and take it back on cancel', async ({ page }) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('woocommerce');
		await panel.getByLabel('Store URL', { exact: true }).fill(STORE_URL);
		await panel.getByLabel('Consumer key', { exact: true }).fill(CONSUMER_KEY);
		await panel.getByLabel('Consumer secret', { exact: true }).fill(CONSUMER_SECRET);
		await panel.getByTestId('commerce-save').click();

		// Saved: both secrets are write-only from here on.
		await expect(panel.getByLabel('Consumer secret', { exact: true })).toHaveAttribute('data-secret-saved', 'true');
		await expect(panel.getByLabel('Consumer key', { exact: true })).toHaveAttribute('data-secret-saved', 'true');

		// Core WooCommerce has one stock pool: one synthetic location.
		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		await locations.getByTestId('commerce-default-location').selectOption('store');
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 1.'
		);

		const { poId } = createPo({
			total: 700,
			lines: [
				{ description: 'A racket', quantity: 10, unitPrice: 50, total: 500, sku: 'WOO-SIMPLE-1' },
				{ description: 'B grip red', quantity: 10, unitPrice: 10, total: 100, sku: 'WOO-VAR-RED' },
				{ description: 'C gift card', quantity: 10, unitPrice: 10, total: 100, sku: 'WOO-UNMANAGED' }
			]
		});
		created.poIds.push(poId);
		const [racket, grip, giftCard] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[racket, '4'],
			[grip, '2'],
			[giftCard, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['WOO-SIMPLE-1', 'synced', null],
				['WOO-VAR-RED', 'synced', null],
				['WOO-UNMANAGED', 'failed', 'stock_not_managed']
			].sort()
		);
		expect(await stock(101)).toBe(14);
		expect(await stock(201)).toBe(7);
		// Basic auth only — never a credential in a URL.
		expect(((await fakeCommerceState('woocommerce')) as WooState).query_auth_requests).toBe(0);

		// The receipt explains the unmanaged product in WooCommerce's terms.
		await page.goto('/goods-receipts');
		await page.getByRole('button', { name: `View goods receipt ${receipt.gr_number}` }).click();
		const sync = page.getByTestId('commerce-sync');
		await expect(
			sync.locator('[data-testid="commerce-sync-row"][data-status="failed"]')
		).toContainText('Stock management is off for this WooCommerce product');
		await expect(
			sync.locator('[data-testid="commerce-sync-row"][data-status="synced"]')
		).toHaveCount(2);

		// Cancelling takes back exactly what landed.
		const cancel = await page.request.post(
			`${API_BASE}/api/goods-receipts/${receipt.id}/cancel`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(cancel.ok(), `cancel receipt (${cancel.status()})`).toBe(true);
		const after = await settledSyncs(page, receipt.id);
		expect(
			after
				.filter((r) => r.direction === 'reverse' && r.status === 'synced')
				.map((r) => r.sku)
				.sort()
		).toEqual(['WOO-SIMPLE-1', 'WOO-VAR-RED']);
		expect(await stock(101)).toBe(10);
		expect(await stock(201)).toBe(5);
	});
});
