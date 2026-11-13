import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { routeVerify, verifyAnswer, withExportedRun } from '../fixtures/nachaRun';
import { expectNoA11yViolations } from './axe-helper';

/**
 * axe at WCAG 2.2 AA, at a desktop width and at the 320px phone width WCAG
 * 1.4.10 names, over three surfaces that state a server's answer in place:
 *
 *  - the "Recall this approval?" dialog (decisions §374), open and after the
 *    door refused (its `role="alert"` error);
 *  - the Cards tab's refused-rows panel after a corporate-card CSV import
 *    (`ui/CsvImportErrors.svelte`, decisions §377). It is a scroll region, so
 *    the import refuses enough rows to make it overflow first: axe's
 *    `scrollable-region-focusable` only judges a region that actually
 *    scrolls, and a short list would pass it whatever the markup;
 *  - the run dialog's "Verify before upload" result (decisions §362), with
 *    both a taken-out list and a held list on screen.
 *
 * The verify answer is routed for the reason `payments/nacha-verify.spec.ts`
 * gives: no local ERP pays a bill on cue.
 */

const WIDTHS = [
	{ name: 'desktop', width: 1280, height: 800 },
	{ name: 'phone (320px)', width: 320, height: 720 }
] as const;

const PREFIX = 'E2E-A11Y-RECALL';
const CARD_CSV_HEADER =
	'external_txn_id,date,posted_date,merchant,amount,currency,card_last_four,card_ref';
/** Enough refused rows to overflow the panel's 260px scroll region. */
const REFUSED_ROWS = 30;

async function createApproved(page: Page, number: string): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E A11y Recall Vendor', invoice_number: number, amount: '41.20', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	// The stage is the fixture, not the path that leads there.
	tenantPsql(
		`UPDATE invoices SET status='approved', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}'`
	);
	return id;
}

test.describe('accessibility — recall, card-import refusals, bank-file verify (WCAG 2.2 AA)', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	for (const { name, width, height } of WIDTHS) {
		test(`recall-approval dialog, open and refused, has no axe violations at ${name}`, async ({
			page
		}) => {
			await page.setViewportSize({ width, height });
			const number = `${PREFIX}-${width}-${Date.now()}`;
			const id = await createApproved(page, number);
			const verdict = page.waitForResponse(
				(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
			);
			await page.goto(`/invoices?id=${id}`);
			await verdict;
			const modal = page.getByRole('dialog', { name: `Edit invoice ${number}` });
			await modal.getByTestId('recall-approval').click();
			const dialog = page.getByRole('dialog', { name: 'Recall this approval?' });
			await expect(dialog).toBeVisible();

			await expectNoA11yViolations(page);

			// The door refuses (a run was staged in between): its sentence is
			// stated in the dialog and nothing moves.
			await page.route(`**/api/invoices/${id}/recall-approval`, (route) =>
				route.fulfill({
					status: 409,
					json: {
						detail: {
							code: 'invoice_not_recallable',
							message: "Only an approved invoice's approval can be recalled"
						}
					}
				})
			);
			await dialog.getByTestId('recall-approval-reason').fill('Amount keyed wrong');
			await dialog.getByTestId('recall-approval-confirm').click();
			await expect(dialog.getByTestId('recall-approval-error')).toBeVisible();

			await expectNoA11yViolations(page);
		});

		test(`card CSV refused-rows panel (overflowing) has no axe violations at ${name}`, async ({
			page
		}) => {
			await page.setViewportSize({ width, height });
			const stamp = Date.now();
			// A blank currency is refused per row (decisions §377), so nothing
			// lands and there is nothing to clean up.
			const csv = [
				CARD_CSV_HEADER,
				...Array.from(
					{ length: REFUSED_ROWS },
					(_, i) => `e2e-a11y-ccy-${stamp}-${i},2026-02-07,,E2E A11y Merchant ${i},8.00,,4242,corp`
				)
			].join('\n');
			await page.goto('/expenses?tab=cards');
			const imported = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === '/api/corporate-card-transactions/import-csv' &&
					r.request().method() === 'POST'
			);
			await page.locator('input[type="file"][accept=".csv"]').setInputFiles({
				name: 'cards.csv',
				mimeType: 'text/csv',
				buffer: Buffer.from(csv)
			});
			const body = (await (await imported).json()) as { imported: number; errors: unknown[] };
			expect(body.imported).toBe(0);
			expect(body.errors).toHaveLength(REFUSED_ROWS);

			// The scroll region itself, not the panel `<section>` around it,
			// which carries the same name from its heading.
			const region = page.locator('[role="region"][tabindex="0"]', {
				has: page.getByTestId('csv-import-errors')
			});
			await expect(region).toHaveAccessibleName('Rows not imported');
			await expect(region.locator('li')).toHaveCount(REFUSED_ROWS);
			// The precondition that makes `scrollable-region-focusable` bite.
			const overflow = await region.evaluate((el) => el.scrollHeight - el.clientHeight);
			expect(overflow, 'the refused-rows list must overflow its region').toBeGreaterThan(0);

			await expectNoA11yViolations(page);
		});

		test(`bank-file verify result has no axe violations at ${name}`, async ({ page }) => {
			await withExportedRun(page, async (run) => {
				// Resized once the run is exported: the export path is the
				// fixture here, and this scan is about the result panel.
				await page.setViewportSize({ width, height });
				await routeVerify(page, run.runId, {
					status: 200,
					json: {
						...verifyAnswer(run, 'superseded', { remaining: 1 }),
						held: [
							{
								payment_id: '00000000-0000-4000-8000-000000000002',
								invoice_id: run.invoiceId,
								invoice_number: `${run.invoiceNumber}-B`,
								reason: 'erp_bill_unverified'
							}
						]
					}
				});
				await run.modal.getByTestId('nacha-verify').click();
				const result = run.modal.getByTestId('nacha-verify-result');
				await expect(result.getByRole('alert')).toBeVisible();
				await expect(result.getByRole('status')).toBeVisible();

				await expectNoA11yViolations(page);
			});
		});
	}
});
