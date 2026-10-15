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
 * Commerce sync, Square (decisions §273): the real `square` adapter against
 * tools/fake-commerce's Square module (`pnpm commerce:up`). Connect from
 * Organization → Commerce with an access token in the sandbox environment, map
 * the Square location, record a receipt — a fractional quantity lands exactly
 * (Square holds decimals), a SKU two variations share is skipped rather than
 * guessed — then cancel it and see the stock taken back off.
 *
 * Fixtures (tools/fake-commerce/providers/square.py): token
 * `fake-square-token`; locations `L-MAIN`, `L-WAREHOUSE`, `L-CLOSED`
 * (inactive); SKU `SQ-TEE-M` → variation `VAR-TEE-M`; `SQ-DUP` is ambiguous.
 */

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

async function squareLevel(itemAtLocation: string): Promise<string | undefined> {
	const state = await fakeCommerceState('square');
	return (state.levels as Record<string, string>)[itemAtLocation];
}

test.describe('/organization commerce, Square', () => {
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

	test('connect with a token, receive a fraction exactly, skip an ambiguous SKU, reverse on cancel', async ({
		page
	}) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('square');
		// Sandbox is the default — the form never starts on the live host.
		await expect(panel.getByLabel('Environment')).toHaveValue('sandbox');
		await panel.getByLabel('Access token').fill('fake-square-token');
		await panel.getByTestId('commerce-save').click();

		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		await locations.getByTestId('commerce-default-location').selectOption('L-MAIN');
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 3.'
		);

		const { poId } = createPo({
			total: 300,
			lines: [
				{ description: 'A tee', quantity: 10, unitPrice: 20, total: 200, sku: 'SQ-TEE-M' },
				{ description: 'B duplicate', quantity: 10, unitPrice: 10, total: 100, sku: 'SQ-DUP' }
			]
		});
		created.poIds.push(poId);
		const [tee, dup] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[tee, '2.5'],
			[dup, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['SQ-TEE-M', 'synced', null],
				['SQ-DUP', 'skipped', 'unmapped_sku']
			].sort()
		);
		const synced = rows.find((r) => r.sku === 'SQ-TEE-M')!;
		expect(synced.provider).toBe('square');
		expect(synced.provider_location_id).toBe('L-MAIN');
		expect(synced.provider_reference).toMatch(/^SQADJ-/);
		expect(await squareLevel('VAR-TEE-M@L-MAIN')).toBe('2.5');

		// Cancel: the synced line comes back off at the same variation and location.
		const cancel = await page.request.post(
			`${API_BASE}/api/goods-receipts/${receipt.id}/cancel`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(cancel.status(), await cancel.text()).toBe(200);
		const after = await settledSyncs(page, receipt.id);
		const reverse = after.find((r) => r.direction === 'reverse' && r.sku === 'SQ-TEE-M');
		expect(reverse?.status).toBe('synced');
		expect(reverse?.quantity_delta).toMatch(/^-2\.50*$/);
		expect(await squareLevel('VAR-TEE-M@L-MAIN')).toBe('0');
	});
});
