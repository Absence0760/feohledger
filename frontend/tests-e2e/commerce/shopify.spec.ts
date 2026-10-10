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
 * Commerce sync, Shopify (decisions §268): the real `shopify` adapter against
 * tools/fake-commerce/providers/shopify.py (`pnpm commerce:up`). Connect from
 * Organization → Commerce with a shop domain and Admin API token, map the
 * store's location, record a receipt, and see Shopify's available stock move —
 * once for the line whose SKU resolves, not at all for the SKU two variants
 * share (skipped, unmapped). Cancelling the receipt takes the stock back off.
 *
 * Fixture literals (shop token, location and item gids, SKUs) are the fake's.
 */

const TOKEN = 'shpat_fake_shopify_token';
const LOCATION = 'gid://shopify/Location/1';
const TEE_LEVEL = `gid://shopify/InventoryItem/101@${LOCATION}`;

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

async function teeLevel(): Promise<string | undefined> {
	const state = await fakeCommerceState('shopify');
	return (state.levels as Record<string, string>)[TEE_LEVEL];
}

test.describe('/organization commerce, Shopify', () => {
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

	test('connect a shop, map its location, and a receipt moves Shopify stock', async ({
		page
	}) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('shopify');
		await panel.getByLabel('Shop domain').fill('feoh-e2e.myshopify.com');
		await panel.getByLabel('Access token').fill(TOKEN);
		await panel.getByTestId('commerce-save').click();

		// The token is write-only from here on.
		await expect(panel.getByLabel('Access token')).toHaveAttribute('data-secret-saved', 'true');

		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		await locations.getByTestId('commerce-default-location').selectOption(LOCATION);
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		// Two active locations; the fake's inactive pop-up is not offered.
		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 2.'
		);

		const { poId } = createPo({
			total: 1100,
			lines: [
				{ description: 'A tee', quantity: 10, unitPrice: 100, total: 1000, sku: 'SHOP-TEE-M' },
				{ description: 'B shared SKU', quantity: 10, unitPrice: 10, total: 100, sku: 'SHOP-DUP' }
			]
		});
		created.poIds.push(poId);
		const [tee, dup] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[tee, '4'],
			[dup, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['SHOP-TEE-M', 'synced', null],
				['SHOP-DUP', 'skipped', 'unmapped_sku']
			].sort()
		);
		const synced = rows.find((r) => r.sku === 'SHOP-TEE-M');
		expect(synced?.provider_reference).toMatch(/^gid:\/\/shopify\/InventoryAdjustmentGroup\//);
		expect(await teeLevel()).toBe('4');

		// Cancelling the receipt reverses the push at Shopify.
		const cancel = await page.request.post(
			`${API_BASE}/api/goods-receipts/${receipt.id}/cancel`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(cancel.ok(), `cancel receipt (${cancel.status()})`).toBe(true);
		await settledSyncs(page, receipt.id);
		await expect.poll(teeLevel, { message: 'the reversal never reached Shopify' }).toBe('0');
	});
});
