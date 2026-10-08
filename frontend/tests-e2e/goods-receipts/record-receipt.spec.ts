import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	signOut,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { cleanup, createGr, createPo } from '../matching/setup';
import type { Page } from '@playwright/test';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * Recording a goods receipt from /goods-receipts (`POST /api/goods-receipts`).
 *
 * Before this form nothing in the app could write a receipt — the 3-way leg
 * read only seeded rows. These specs drive the real form against the real API:
 * pick a PO, see ordered / received / outstanding per line, record a partial
 * delivery, open what was booked, and cancel it. The matching consequence (a
 * hold lifting or not, by who recorded the receipt) is pinned in the backend's
 * `tests/test_goods_receipt_entry.py`, where the exceptions table is in reach.
 */

function receiptsOn(poId: string): string[] {
	const out = tenantPsql(`select string_agg(id::text, ',') from goods_receipts where po_id = '${poId}';`).trim();
	return out ? out.split(',') : [];
}

async function openFormFor(page: Page, poNumber: string, poId: string) {
	await page.getByTestId('record-receipt').click();
	const form = page.getByTestId('record-receipt-form');
	await form.getByTestId('receipt-po-search').fill(poNumber);
	await expect(form.getByTestId('receipt-po')).toHaveValue(poId);
	return form;
}

test.describe('record a goods receipt', () => {
	const created: { grIds: string[]; poIds: string[] } = { grIds: [], poIds: [] };
	test.afterAll(() => {
		for (const poId of created.poIds) created.grIds.push(...receiptsOn(poId));
		cleanup(created);
	});

	test('a partial delivery is booked line by line, then cancelled', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 1200,
			lines: [
				{ description: 'Wilson Pro Staff racket', quantity: 10, unitPrice: 100, total: 1000 },
				{ description: 'Overgrip pack', quantity: 20, unitPrice: 10, total: 200 }
			]
		});
		created.poIds.push(poId);

		await page.goto('/goods-receipts');
		await page.getByTestId('record-receipt').click();
		const form = page.getByTestId('record-receipt-form');
		await form.getByTestId('receipt-po-search').fill(poNumber);
		await expect(form.getByTestId('receipt-po')).toHaveValue(poId);

		// Outstanding pre-fills "this delivery" — the common case is "it all came".
		const qty = form.getByTestId('receipt-line-quantity');
		await expect(qty).toHaveCount(2);
		await expect(qty.nth(0)).toHaveValue('10');
		await expect(qty.nth(1)).toHaveValue('20');

		// Nine rackets and none of the grips.
		await qty.nth(0).fill('9');
		await qty.nth(1).fill('');
		await form.getByTestId('receipt-number').fill(`DN-${poNumber}`);
		await form.getByTestId('receipt-submit').click();

		// The form closes onto the receipt it booked.
		await expect(form).toBeHidden();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Goods receipt"]');
		await expect(modal.locator('.num-badge')).toHaveText(`DN-${poNumber}`);
		await expect(modal.getByTestId('receipt-source')).toHaveText('Recorded in FeohLedger');
		const lines = modal.locator('.line-table').first().locator('tbody tr');
		await expect(lines).toHaveCount(1);
		await expect(lines.first()).toContainText('Wilson Pro Staff racket');
		await expect(lines.first()).toContainText('9');

		// The PO now shows nine received on that line.
		const po = await page.request.get(`${API_BASE}/api/purchase-orders/${poId}`, {
			headers: await authedTenantHeaders(page)
		});
		const body = (await po.json()) as { quantity_received_total: number };
		expect(body.quantity_received_total).toBe(9);

		// Two clicks: arm, then confirm.
		const cancel = modal.getByTestId('cancel-receipt');
		await cancel.click();
		await expect(cancel).toHaveText('Confirm cancel');
		await cancel.click();
		await expect(modal.locator('.title-block')).toContainText('cancelled');
		await expect(modal.getByTestId('cancel-receipt')).toHaveCount(0);
	});

	test('the form says when a line is over-received, and refuses a cancelled PO', async ({
		page
	}) => {
		const open = createPo({
			total: 100,
			lines: [{ description: 'Ball hopper', quantity: 2, unitPrice: 50, total: 100 }]
		});
		const cancelled = createPo({
			total: 100,
			lines: [{ description: 'Net', quantity: 1, unitPrice: 100, total: 100 }]
		});
		tenantPsql(`update purchase_orders set status = 'cancelled' where id = '${cancelled.poId}';`);
		created.poIds.push(open.poId, cancelled.poId);

		await page.goto('/goods-receipts');
		await page.getByTestId('record-receipt').click();
		const form = page.getByTestId('record-receipt-form');

		await form.getByTestId('receipt-po-search').fill(open.poNumber);
		await expect(form.getByTestId('receipt-po')).toHaveValue(open.poId);
		await form.getByTestId('receipt-line-quantity').fill('3');
		await expect(form.getByTestId('receipt-over')).toBeVisible();
		// Over-receipt is allowed — the matcher flags it — so submit stays live.
		await expect(form.getByTestId('receipt-submit')).toBeEnabled();
		await form.getByTestId('receipt-line-quantity').fill('0');
		await expect(form.getByTestId('receipt-submit')).toBeDisabled();

		await form.getByTestId('receipt-po-search').fill(cancelled.poNumber);
		await expect(form.getByTestId('receipt-po')).toHaveValue(cancelled.poId);
		await expect(form.getByTestId('receipt-po-cancelled')).toBeVisible();
		await expect(form.getByTestId('receipt-submit')).toBeDisabled();
	});

	test('a CFO sees receipts but no record control, and the API refuses them', async ({
		page,
		tenantCfo
	}) => {
		await page.goto('/');
		await signOut(page);
		await signInAndWait(page, tenantCfo);
		await page.goto('/goods-receipts');
		await expect(page.getByRole('heading', { name: 'Goods Receipts' })).toBeVisible();
		await expect(page.locator('table tbody tr.clickable').first()).toBeVisible();
		await expect(page.getByTestId('record-receipt')).toHaveCount(0);

		const resp = await page.request.post(`${API_BASE}/api/goods-receipts`, {
			headers: await authedTenantHeaders(page),
			data: {
				po_id: '00000000-0000-0000-0000-000000000001',
				received_date: '2026-01-01',
				lines: [{ quantity_received: '1' }]
			}
		});
		expect(resp.status()).toBe(403);
	});

	// Scanned here, not in `a11y/axe.spec.ts`'s route table: the controls are
	// inside a DIALOG — a lines table with per-row labelled inputs, the
	// over-receipt note, the persistent role="alert" refusal region — which a
	// list-page scan never opens.
	test('the record dialog has no axe violations', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 50,
			lines: [{ description: 'Tennis balls (can)', quantity: 5, unitPrice: 10, total: 50 }]
		});
		created.poIds.push(poId);
		await page.goto('/goods-receipts');
		await page.getByTestId('record-receipt').click();
		const form = page.getByTestId('record-receipt-form');
		await form.getByTestId('receipt-po-search').fill(poNumber);
		await expect(form.getByTestId('receipt-po')).toHaveValue(poId);
		// The over-receipt note is the widest state of the line table.
		await form.getByTestId('receipt-line-quantity').fill('6');
		await expect(form.getByTestId('receipt-over')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('a refusal stays inline in the form, in words', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 40,
			lines: [{ description: 'Grip tape', quantity: 4, unitPrice: 10, total: 40 }]
		});
		created.poIds.push(poId);
		await page.goto('/goods-receipts');
		let form = await openFormFor(page, poNumber, poId);
		await form.getByTestId('receipt-line-quantity').fill('1');
		await form.getByTestId('receipt-number').fill(`DN-${poNumber}`);
		await form.getByTestId('receipt-submit').click();
		await expect(form).toBeHidden();
		await page.keyboard.press('Escape');

		// The same delivery-note number again: refused, and the form stays open
		// with the reason in it rather than a toast that fades.
		form = await openFormFor(page, poNumber, poId);
		await form.getByTestId('receipt-line-quantity').fill('1');
		await form.getByTestId('receipt-number').fill(`DN-${poNumber}`);
		await form.getByTestId('receipt-submit').click();
		await expect(form.getByTestId('receipt-error')).toContainText(`DN-${poNumber}`);
		await expect(form).toBeVisible();
	});

	test('a quantity in the wrong notation is explained, not just refused', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 40,
			lines: [{ description: 'Grip tape', quantity: 4, unitPrice: 10, total: 40 }]
		});
		created.poIds.push(poId);
		await page.goto('/goods-receipts');
		const form = await openFormFor(page, poNumber, poId);
		const qty = form.getByTestId('receipt-line-quantity');
		// An English reader's decimal separator is a dot; a comma is refused
		// rather than read as a thousands separator or a decimal.
		await qty.fill('9,5');
		const error = form.getByTestId('receipt-line-quantity-error');
		await expect(error).toBeVisible();
		await expect(qty).toHaveAttribute('aria-invalid', 'true');
		const errorId = await error.getAttribute('id');
		await expect(qty).toHaveAttribute('aria-describedby', new RegExp(errorId!));
		await expect(form.getByTestId('receipt-submit')).toBeDisabled();
		await qty.fill('2.5');
		await expect(error).toHaveCount(0);
		await expect(form.getByTestId('receipt-submit')).toBeEnabled();
	});

	test('a fully received line starts blank and is not posted', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 300,
			lines: [
				{ description: 'Rackets', quantity: 2, unitPrice: 100, total: 200 },
				{ description: 'Strings', quantity: 10, unitPrice: 10, total: 100 }
			]
		});
		created.poIds.push(poId);
		const headers = await authedTenantHeaders(page);
		const po = (await (
			await page.request.get(`${API_BASE}/api/purchase-orders/${poId}`, { headers })
		).json()) as { line_items: { id: string; description: string }[] };
		const rackets = po.line_items.find((l) => l.description === 'Rackets')!;
		const first = await page.request.post(`${API_BASE}/api/goods-receipts`, {
			headers,
			data: {
				po_id: poId,
				received_date: new Date().toISOString().slice(0, 10),
				lines: [{ po_line_item_id: rackets.id, quantity_received: '2' }]
			}
		});
		expect(first.status()).toBe(201);

		await page.goto('/goods-receipts');
		const form = await openFormFor(page, poNumber, poId);
		const qty = form.getByTestId('receipt-line-quantity');
		await expect(qty.nth(0)).toHaveValue('');
		await expect(qty.nth(1)).toHaveValue('10');
		await form.getByTestId('receipt-submit').click();

		const modal = page.locator('div.modal[role="dialog"][aria-label="Goods receipt"]');
		const lines = modal.locator('.line-table').first().locator('tbody tr');
		await expect(lines).toHaveCount(1);
		await expect(lines.first()).toContainText('Strings');
	});

	test('a PO with no lines takes described lines', async ({ page }) => {
		const { poId, poNumber } = createPo({ total: 90 });
		created.poIds.push(poId);
		await page.goto('/goods-receipts');
		const form = await openFormFor(page, poNumber, poId);
		await form.getByTestId('receipt-free-description').fill('Ball machine');
		await form.getByTestId('receipt-free-quantity').fill('1');
		await form.getByTestId('receipt-add-line').click();
		await form.getByTestId('receipt-free-description').nth(1).fill('Spare balls');
		await form.getByTestId('receipt-free-quantity').nth(1).fill('48');
		await form.getByRole('button', { name: 'Remove line 1' }).click();
		// Removing the first row leaves the second row's own values in place.
		await expect(form.getByTestId('receipt-free-description')).toHaveValue('Spare balls');
		await form.getByTestId('receipt-submit').click();

		const modal = page.locator('div.modal[role="dialog"][aria-label="Goods receipt"]');
		const lines = modal.locator('.line-table').first().locator('tbody tr');
		await expect(lines).toHaveCount(1);
		await expect(lines.first()).toContainText('Spare balls');
	});

	test('a search that matches nothing says so', async ({ page }) => {
		await page.goto('/goods-receipts');
		await page.getByTestId('record-receipt').click();
		const form = page.getByTestId('record-receipt-form');
		await form.getByTestId('receipt-po-search').fill('NO-SUCH-PO-zzzz');
		await expect(form.getByTestId('receipt-no-pos')).toBeVisible();
		await expect(form.getByTestId('receipt-submit')).toBeDisabled();
	});

	test('Escape closes the dialog and returns focus to the trigger', async ({ page }) => {
		await page.goto('/goods-receipts');
		const trigger = page.getByTestId('record-receipt');
		await trigger.click();
		await expect(page.getByTestId('record-receipt-form')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(page.getByTestId('record-receipt-form')).toBeHidden();
		await expect(trigger).toBeFocused();
	});

	test('a receipt from elsewhere says so and offers no cancel', async ({ page }) => {
		const { poId } = createPo({ total: 10 });
		created.poIds.push(poId);
		const { grNumber } = createGr({ poId, lines: [{ description: 'thing', quantityReceived: 1 }] });
		await page.goto('/goods-receipts');
		await page.getByRole('button', { name: new RegExp(grNumber) }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Goods receipt"]');
		await expect(modal.getByTestId('receipt-source')).toHaveText('Recorded elsewhere');
		await expect(modal.getByTestId('cancel-receipt')).toHaveCount(0);
	});

	test('the record dialog has no axe violations at phone width', async ({ page }) => {
		const { poId, poNumber } = createPo({
			total: 50,
			lines: [{ description: 'Tennis balls (can)', quantity: 5, unitPrice: 10, total: 50 }]
		});
		created.poIds.push(poId);
		await page.setViewportSize({ width: 375, height: 812 });
		await page.goto('/goods-receipts');
		const form = await openFormFor(page, poNumber, poId);
		await form.getByTestId('receipt-line-quantity').fill('6');
		await expect(form.getByTestId('receipt-over')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('a clerk can record a receipt', async ({ page, tenantClerk }) => {
		await page.goto('/');
		await signOut(page);
		await signInAndWait(page, tenantClerk);
		await page.goto('/goods-receipts');
		await expect(page.getByTestId('record-receipt')).toBeVisible();
	});
});
