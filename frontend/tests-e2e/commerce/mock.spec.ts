import { expect, tenantPsql, test } from '../fixtures/helpers';
import { cleanup, createPo } from '../matching/setup';
import { disconnectCommerce, recordReceipt, settledSyncs } from './helpers';

/**
 * Commerce sync, mock path (decisions §273): connect the in-process `mock`
 * platform from Organization → Commerce, map where stock lands, record a
 * receipt, and see each line's push on the receipt — synced for a line with a
 * SKU, skipped (`no_sku`) for one without. Needs nothing beyond the normal
 * stack; the real providers each add a `<provider>.spec.ts` beside this one,
 * against tools/fake-commerce (see ./helpers.ts).
 */

function poLineIds(poId: string): string[] {
	const out = tenantPsql(
		`select string_agg(id::text, ',' order by description) from po_line_items where po_id = '${poId}';`
	).trim();
	return out ? out.split(',') : [];
}

test.describe('/organization commerce, mock platform', () => {
	const created: { grIds: string[]; poIds: string[] } = { grIds: [], poIds: [] };

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

	test('connect, map a location, and a receipt pushes its SKU lines', async ({ page }) => {
		await page.goto('/organization?section=commerce');
		const panel = page.getByTestId('commerce-panel');
		await panel.getByTestId('commerce-provider-select').selectOption('mock');
		await panel.getByTestId('commerce-save').click();

		// Connected: the platform's locations load into the picker.
		const locations = panel.getByTestId('commerce-locations');
		await expect(locations).toBeVisible();
		await locations.getByTestId('commerce-default-location').selectOption('mock-main');
		await locations.getByTestId('commerce-save-locations').click();
		await expect(page.getByText('Locations saved.')).toBeVisible();

		await panel.getByTestId('commerce-test').click();
		await expect(panel.getByTestId('commerce-status')).toHaveText(
			'Connected. Stock locations found: 2.'
		);

		const { poId } = createPo({
			total: 1100,
			lines: [
				{ description: 'A racket', quantity: 10, unitPrice: 100, total: 1000, sku: 'WPS-97' },
				{ description: 'B free text', quantity: 10, unitPrice: 10, total: 100 }
			]
		});
		created.poIds.push(poId);
		const [racket, freeText] = poLineIds(poId);
		const receipt = await recordReceipt(page, poId, [
			[racket, '4'],
			[freeText, '1']
		]);
		created.grIds.push(receipt.id);

		const rows = await settledSyncs(page, receipt.id);
		expect(rows.map((r) => [r.sku, r.status, r.reason]).sort()).toEqual(
			[
				['WPS-97', 'synced', null],
				[null, 'skipped', 'no_sku']
			].sort()
		);

		// The receipt shows the same, line by line.
		await page.goto('/goods-receipts');
		await page.getByRole('button', { name: `View goods receipt ${receipt.gr_number}` }).click();
		const sync = page.getByTestId('commerce-sync');
		await expect(sync).toBeVisible();
		await expect(sync.locator('[data-testid="commerce-sync-row"][data-status="synced"]')).toContainText(
			'WPS-97'
		);
		await expect(
			sync.locator('[data-testid="commerce-sync-row"][data-status="skipped"]')
		).toContainText('The line has no SKU.');
		await expect(sync.getByTestId('commerce-sync-retry')).toHaveCount(0);
	});
});
