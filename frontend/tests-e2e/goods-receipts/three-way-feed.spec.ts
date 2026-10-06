import { expect, test } from '../fixtures/helpers';
import { cleanup, createGr, createMatchedInvoice, createPo, issueText } from '../matching/setup';

/**
 * Goods Receipts as the 3-way-match feeder.
 *
 * The /goods-receipts page is a read surface, but a GR's real job is to drive
 * the 3-way leg of `po_matching`: once a GR exists for a PO, an invoice
 * matched to that PO is compared on received quantity too. These specs prove
 * the GR actually changes the match outcome (presence → 3-way; short receipt →
 * partial), which the thin list spec doesn't touch.
 */

test.describe('goods receipt drives the 3-way match leg', () => {
	const created: { invoiceIds: string[]; grIds: string[]; poIds: string[] } = {
		invoiceIds: [],
		grIds: [],
		poIds: []
	};
	test.afterAll(() => cleanup(created));

	test('no GR → match stays 2-way; adding a full GR makes it a matched 3-way', async ({
		page
	}) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'unit', quantity: 4, unitPrice: 250, total: 1000 }]
		});
		created.poIds.push(poId);

		// Before any GR: a clean amount match is 2-way.
		const first = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(first.invoiceId);
		expect(first.poMatch!.match_type).toBe('2-way');
		expect(first.poMatch!.gr_id).toBeNull();

		// After a full GR: a fresh invoice on the same PO is a matched 3-way.
		const { grId } = createGr({ poId, lines: [{ description: 'unit', quantityReceived: 4 }] });
		created.grIds.push(grId);
		const second = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(second.invoiceId);
		expect(second.poMatch!.match_type).toBe('3-way');
		expect(second.poMatch!.gr_id).toBe(grId);
		expect(second.poMatch!.status).toBe('matched');
	});

	test('a short GR drives the match to partial', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 800,
			lines: [{ description: 'unit', quantity: 8, unitPrice: 100, total: 800 }]
		});
		created.poIds.push(poId);
		// 3 of 8 received.
		const { grId } = createGr({ poId, lines: [{ description: 'unit', quantityReceived: 3 }] });
		created.grIds.push(grId);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 800 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('3-way');
		expect(poMatch!.status).toBe('partial');
		// 3/8 = 37.5% → rounded "38%" in the issue text.
		expect(issueText(poMatch!)).toMatch(/Partial receipt: 38% of ordered quantity/i);
	});
});
