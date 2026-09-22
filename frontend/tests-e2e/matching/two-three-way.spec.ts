import { expect, test } from '../fixtures/helpers';
import { cleanup, createGr, createMatchedInvoice, createPo, exceptionsFor, recompute } from './setup';

/**
 * 2-way (invoice↔PO) and 3-way (invoice↔PO↔GR) matching outcomes.
 *
 * The matcher (`services/po_matching.match_invoice_to_po`) is the control that
 * stops overpayment: it compares the invoice amount against the PO total
 * within a tolerance band, and — when a goods receipt exists — folds in the
 * received-quantity comparison. These specs pin the exact status / match_type
 * each amount + receipt combination produces, the tolerance boundary, and the
 * exception routing, because a silent regression here lets a wrong amount sail
 * through to payment.
 */

test.describe('2-way invoice↔PO matching', () => {
	const created: { invoiceIds: string[]; poIds: string[] } = { invoiceIds: [], poIds: [] };
	test.afterAll(() => cleanup(created));

	test('amount within tolerance → matched, within_tolerance true', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		// +1% variance — inside the 5% default band.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1010 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch).not.toBeNull();
		expect(poMatch!.match_type).toBe('2-way');
		expect(poMatch!.status).toBe('matched');
		expect(poMatch!.within_tolerance).toBe(true);
		expect(poMatch!.po_total).toBeCloseTo(1000, 2);
		expect(poMatch!.amount_variance).toBeCloseTo(10, 2);
		expect(poMatch!.amount_variance_pct).toBeCloseTo(1, 2);
		// A clean match raises no po_mismatch exception.
		expect(exceptionsFor(invoiceId)).not.toContain('po_mismatch:warning');
	});

	test('amount at the exact tolerance boundary (5%) → still matched', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		// Exactly +5% — the matcher uses `<=` so the boundary is inclusive.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1050 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.amount_variance_pct).toBeCloseTo(5, 2);
		expect(poMatch!.within_tolerance).toBe(true);
		expect(poMatch!.status).toBe('matched');
	});

	test('amount just outside tolerance → mismatch + po_mismatch warning exception', async ({
		page
	}) => {
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		// +6% variance — outside the 5% band.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1060 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.status).toBe('mismatch');
		expect(poMatch!.within_tolerance).toBe(false);
		expect(poMatch!.amount_variance_pct).toBeCloseTo(6, 2);
		expect(poMatch!.issues.join(' ')).toMatch(/Amount mismatch/i);
		// An over-tolerance invoice must surface a po_mismatch exception so it
		// can be blocked before payment.
		expect(exceptionsFor(invoiceId)).toContain('po_mismatch:warning');
	});

	test('an invoice in another currency than its PO → mismatch, no variance, both codes named', async ({
		page
	}) => {
		// EUR 1,000 against a USD 1,000 order used to read `matched` at 0% — the
		// amount control passing on quantities in different units (decisions §197).
		const { poId, poNumber } = createPo({ total: 1000, currency: 'USD' });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1000,
			currency: 'EUR'
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.status).toBe('mismatch');
		expect(poMatch!.currency_check).toBe('different');
		expect(poMatch!.po_currency).toBe('USD');
		expect(poMatch!.amount_variance).toBeNull();
		expect(poMatch!.within_tolerance).toBe(false);
		expect(poMatch!.issues.join(' ')).toMatch(/Currency mismatch: invoice in EUR, PO in USD/);
		expect(exceptionsFor(invoiceId)).toContain('po_mismatch:warning');
	});

	test('a PO that records no currency is compared at face value, and says so', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber,
			amount: 1000,
			currency: 'EUR'
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.status).toBe('matched');
		expect(poMatch!.currency_check).toBe('unknown');
		expect(poMatch!.po_currency).toBeNull();
		expect(exceptionsFor(invoiceId)).not.toContain('po_mismatch:warning');
	});

	test('po_number with no matching PO → no_po + error exception', async ({ page }) => {
		// No PO seeded for this number.
		const { invoiceId, poMatch } = await createMatchedInvoice(page, {
			poNumber: 'PO-DOES-NOT-EXIST-XYZ',
			amount: 500
		});
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.status).toBe('no_po');
		expect(poMatch!.po_id).toBeNull();
		expect(poMatch!.issues.join(' ')).toMatch(/not found/i);
		expect(exceptionsFor(invoiceId)).toContain('po_mismatch:error');
	});

	test('amounts/variance carry full cents precision (Decimal, not float-rounded)', async ({
		page
	}) => {
		// 1234.56 vs 1200.00 → 34.56 variance, 2.88% — a fractional-cent PO total
		// to catch any float truncation in the variance math.
		const { poId, poNumber } = createPo({ total: 1200.0 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1234.56 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.amount_variance).toBeCloseTo(34.56, 2);
		expect(poMatch!.amount_variance_pct).toBeCloseTo(2.88, 2);
		expect(poMatch!.within_tolerance).toBe(true);
		expect(poMatch!.status).toBe('matched');
	});
});

test.describe('3-way invoice↔PO↔GR matching', () => {
	const created: { invoiceIds: string[]; grIds: string[]; poIds: string[] } = {
		invoiceIds: [],
		grIds: [],
		poIds: []
	};
	test.afterAll(() => cleanup(created));

	test('full receipt → matched, match_type 3-way', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'widget', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		const { grId } = createGr({ poId, lines: [{ description: 'widget', quantityReceived: 10 }] });
		created.grIds.push(grId);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('3-way');
		expect(poMatch!.gr_id).toBe(grId);
		// Quantity fully received → stays matched, not downgraded to partial.
		expect(poMatch!.status).toBe('matched');
	});

	test('partial receipt → status partial + po_mismatch info exception', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'widget', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		// Only 6 of 10 received.
		const { grId } = createGr({ poId, lines: [{ description: 'widget', quantityReceived: 6 }] });
		created.grIds.push(grId);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);

		expect(poMatch!.match_type).toBe('3-way');
		expect(poMatch!.status).toBe('partial');
		expect(poMatch!.issues.join(' ')).toMatch(/Partial receipt: 60% of ordered quantity/i);
		// Partial receipt is informational, not a hard block.
		expect(exceptionsFor(invoiceId)).toContain('po_mismatch:info');
	});

	test('amount mismatch on a 3-way stays mismatch (amount wins over GR)', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1000,
			lines: [{ description: 'widget', quantity: 10, unitPrice: 100, total: 1000 }]
		});
		created.poIds.push(poId);
		// Full receipt, but the invoiced amount is +10% over the PO.
		const { grId } = createGr({ poId, lines: [{ description: 'widget', quantityReceived: 10 }] });
		created.grIds.push(grId);

		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1100 });
		created.invoiceIds.push(invoiceId);

		// 3-way is detected (GR present) but the amount mismatch is not masked
		// by the full receipt — partial only downgrades a *matched* status.
		expect(poMatch!.match_type).toBe('3-way');
		expect(poMatch!.status).toBe('mismatch');
		expect(poMatch!.within_tolerance).toBe(false);
		expect(exceptionsFor(invoiceId)).toContain('po_mismatch:warning');
	});

	test('po_match clears when the po_number is removed from the invoice', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 1000 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 1000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.status).toBe('matched');

		// Removing the PO number re-runs the matcher with no PO → po_match null.
		const { authToken, tenantHeaders, API_BASE } = await import('../fixtures/helpers');
		const headers = {
			...tenantHeaders(await authToken(page)),
			'Content-Type': 'application/json'
		};
		const resp = await page.request.patch(`${API_BASE}/api/invoices/${invoiceId}`, {
			headers,
			data: { po_number: '' }
		});
		expect(resp.ok()).toBe(true);
		const after = (await resp.json()) as { po_match: unknown };
		expect(after.po_match).toBeNull();
	});

	test('recompute is idempotent — re-PATCH yields the same match', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 2000 });
		created.poIds.push(poId);
		const { invoiceId, poMatch } = await createMatchedInvoice(page, { poNumber, amount: 2000 });
		created.invoiceIds.push(invoiceId);
		expect(poMatch!.status).toBe('matched');

		const again = await recompute(page, invoiceId);
		expect(again!.status).toBe('matched');
		expect(again!.po_id).toBe(poMatch!.po_id);
		// Same-currency match, so the variance is a figure (it is `null` only on a
		// currency mismatch).
		expect(again!.amount_variance).toBeCloseTo(poMatch!.amount_variance!, 2);
	});
});
