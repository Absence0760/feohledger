import { expect, tenantPsql, test } from '../fixtures/helpers';
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
 * Commerce sync against Lightspeed Retail (X-Series), decisions §268: the real
 * `lightspeed` adapter talking to tools/fake-commerce (`pnpm commerce:up`,
 * :12113) through the committed FEOH_COMMERCE_API_BASE_OVERRIDE. Connect from
 * Organization → Commerce with a domain prefix + token, map an outlet, record a
 * receipt: the SKU Lightspeed knows lands as a RECEIVED stock order carrying the
 * line's idempotency key, and one it does not know is skipped, never guessed.
 */

const TOKEN = 'fake-lightspeed-token';
const OUTLET = 'ls-outlet-main';
const PRODUCT = 'ls-prod-1';

type Consignment = {
	id: string;
	type: string;
	status: string;
	reference: string | null;
	outlet_id: string;
};

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

async function lightspeedLevel(): Promise<number> {
	const state = await fakeCommerceState('lightspeed');
	const levels = state.levels as Record<string, string>;
	return Number(levels[`${PRODUCT}@${OUTLET}`] ?? '0');
}

test.describe('/organization commerce, Lightspeed against fake-commerce', () => {
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

	test('connect, map an outlet, and a receipt becomes a received stock order', async ({
		page
	}) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('lightspeed');
		await panel.locator('input[name="domain_prefix"]').fill('fakeshop');
		await panel.locator('input[name="access_token"]').fill(TOKEN);
		await panel.getByTestId('commerce-save').click();
		await expect(page.getByText('Commerce connection saved.')).toBeVisible();

		// The token is write-only: after the save it reads back as kept, never echoed.
		await expect(panel.locator('input[name="access_token"]')).toHaveAttribute(
			'data-secret-saved',
			'true'
		);
		await expect(panel.locator('input[name="access_token"]')).toHaveValue('');

		// Connected: Lightspeed's live outlets load (the deleted one is not offered).
		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		const picker = locations.getByTestId('commerce-default-location');
		await expect(picker.locator(`option[value="ls-outlet-closed"]`)).toHaveCount(0);
		await picker.selectOption(OUTLET);
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 2.'
		);

		const before = await lightspeedLevel();
		const { poId } = createPo({
			total: 1100,
			lines: [
				{ description: 'A racket', quantity: 10, unitPrice: 100, total: 1000, sku: 'LS-SKU-1' },
				{ description: 'B unknown', quantity: 10, unitPrice: 10, total: 100, sku: 'NOT-IN-LS' }
			]
		});
		created.poIds.push(poId);
		const [racket, unknown] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[racket, '4'],
			[unknown, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['LS-SKU-1', 'synced', null],
				['NOT-IN-LS', 'skipped', 'unmapped_sku']
			].sort()
		);
		expect(await lightspeedLevel()).toBe(before + 4);

		// Lightspeed holds it as a received stock order keyed by our line.
		const synced = rows.find((r) => r.sku === 'LS-SKU-1')!;
		const orders = (await fakeCommerceState('lightspeed')).consignments as Consignment[];
		const order = orders.find((c) => c.reference === `feoh:${synced.gr_line_item_id}:receive`);
		expect(order, 'no Lightspeed stock order carries the line key').toBeTruthy();
		expect([order!.type, order!.status, order!.outlet_id]).toEqual([
			'SUPPLIER',
			'RECEIVED',
			OUTLET
		]);

		// The receipt shows the same, line by line; the unmapped line can be retried.
		await page.goto('/goods-receipts');
		await page.getByRole('button', { name: `View goods receipt ${receipt.gr_number}` }).click();
		const sync = page.getByTestId('commerce-sync');
		await expect(sync).toBeVisible();
		await expect(
			sync.locator('[data-testid="commerce-sync-row"][data-status="synced"]')
		).toContainText('LS-SKU-1');
		await expect(
			sync.locator('[data-testid="commerce-sync-row"][data-status="skipped"]')
		).toContainText('NOT-IN-LS');
	});
});
