import type { Locator, Page } from '@playwright/test';

import { API_BASE, authedTenantHeaders, deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * While a modal dialog is open the page outside the topmost one is `inert`
 * (`lib/actions/modalInert.ts`, decisions §384; WCAG 1.3.2, 2.4.3, 2.5.8).
 *
 * `aria-modal` alone left the page behind reachable by a screen reader's
 * cursor and by pointer, and axe's `target-size` rule reported the `/invoices`
 * sort buttons behind the invoice dialog as obscured targets. Driven here over
 * the two dialogs that stack: the invoice dialog (a hand-rolled shell) and the
 * "Recall this approval?" dialog (`ui/Modal`) opened from it. The rules are
 * unit-tested in `src/lib/actions/modalInert.test.ts`; axe over the stacked
 * pair is `recall-import-verify.spec.ts`.
 */

const PREFIX = 'E2E-MODAL-INERT';

/** Whether `el` is inside an inert subtree (itself included). */
const isInert = (el: Locator) => el.evaluate((n) => n.closest('[inert]') !== null);

async function createApproved(page: Page, number: string): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E Modal Inert Vendor', invoice_number: number, amount: '12.30', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	// The stage is the fixture, not the path that leads there.
	tenantPsql(
		`UPDATE invoices SET status='approved', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}'`
	);
	return id;
}

test.describe('modal dialogs make the page behind them inert', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	test('the page, then the dialog under a stacked one, go inert; closing restores each', async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		const number = `${PREFIX}-${Date.now()}`;
		const id = await createApproved(page, number);
		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		await page.goto(`/invoices?id=${id}`);
		await verdict;

		const sortBtn = page.locator('main .sort-btn').first();
		const sidebar = page.locator('aside.sidebar');
		const toasts = page.locator('.toast-region');
		const invoice = page.getByRole('dialog', { name: `Edit invoice ${number}` });
		const recallBtn = invoice.getByTestId('recall-approval');
		await expect(recallBtn).toBeEnabled();

		// One dialog: the page behind it is inert, the dialog and the toast
		// region are not, and the layout's own `inert` (the sidebar overlay's)
		// is not what did it — the attribute sits on content we marked.
		expect(await isInert(sortBtn)).toBe(true);
		expect(await isInert(sidebar)).toBe(true);
		expect(await isInert(recallBtn)).toBe(false);
		expect(await isInert(toasts)).toBe(false);
		await expect(page.locator('main#main-content')).not.toHaveAttribute('inert', /.*/);
		// Inert content takes no pointer: a click on a sort button lands on the
		// backdrop, which closes the dialog — so assert the hit test instead.
		const box = await sortBtn.boundingBox();
		expect(box).not.toBeNull();
		const hit = await page.evaluate(
			({ x, y }) => document.elementFromPoint(x, y)?.closest('.sort-btn') ?? null,
			{ x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 }
		);
		expect(hit).toBeNull();

		// A second dialog over the first: the first goes inert too.
		await recallBtn.click();
		const recall = page.getByRole('dialog', { name: 'Recall this approval?' });
		await expect(recall).toBeVisible();
		await expect(recall.getByTestId('recall-approval-reason')).toBeFocused();
		expect(await isInert(recallBtn)).toBe(true);
		expect(await isInert(recall.getByTestId('recall-approval-reason'))).toBe(false);
		expect(await isInert(sortBtn)).toBe(true);
		// The toast region follows the top dialog and stays live.
		await expect.poll(() => toasts.evaluate((n) => n.closest('[role="dialog"]')?.getAttribute('aria-label'))).toBe(
			'Recall this approval?'
		);
		expect(await isInert(toasts)).toBe(false);

		// Closing it gives back exactly the one-dialog state, and focus returns
		// to the control that opened it.
		await page.keyboard.press('Escape');
		await expect(recall).toHaveCount(0);
		await expect(recallBtn).toBeFocused();
		expect(await isInert(recallBtn)).toBe(false);
		expect(await isInert(sortBtn)).toBe(true);

		// Closing the last dialog leaves nothing inert.
		await page.keyboard.press('Escape');
		await expect(invoice).toHaveCount(0);
		await expect(page.locator('[inert]')).toHaveCount(0);
		expect(await isInert(sortBtn)).toBe(false);
	});
});
