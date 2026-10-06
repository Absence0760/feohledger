import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * An ACCEPTED early-payment discount changes what a run pays
 * (`backend/docs/payments.md` § An accepted early-payment discount is paid, not
 * just reported). The run total is therefore below the invoices it pays, and
 * the two screens a payer reads before money moves must say why:
 *
 *  - the Queue row (and the review step before a run is staged) names the
 *    figure the run would move and the pay-by date — `accepted_discount_*` /
 *    `payable_amount` on `GET /api/payments/queue`;
 *  - the run dialog shows each payment's deduction and the run's
 *    `discount_total` beside a total that already excludes it.
 *
 * Both payloads are injected/stubbed for the same reason as
 * `run-detail-currency.spec.ts`: the subject is what the page does with the
 * contract, which `backend/tests/test_discounted_payment.py` pins end to end.
 */

const RUNS_LIST_PATH = '/api/payments/runs/';
const RUN_ID = 'd15c0000-0000-4000-8000-0000000000d1';

function runDetail() {
	return {
		id: RUN_ID,
		status: 'draft',
		total_amount: '1016.26',
		discount_total: '20.74',
		currency: 'USD',
		initiated_by: null,
		executed_at: null,
		created_at: '2026-10-01T10:00:00Z',
		requires_cfo_approval: false,
		cfo_approved_by: null,
		cfo_approved_at: null,
		payment_count: 1,
		payments: [
			{
				id: `${RUN_ID}-leg-1`,
				invoice_id: `${RUN_ID}-inv-1`,
				invoice_number: 'DISC-RUN-1',
				vendor_name: 'Discount Fixture Ltd',
				amount: '1016.26',
				discount_amount: '20.74' as string | null,
				discount_offer_id: `${RUN_ID}-offer` as string | null,
				invoice_amount: '1037.00',
				currency: 'USD',
				method: 'ach',
				status: 'pending',
				reference: null
			}
		]
	};
}

// One stub per test: Playwright runs the most recently registered matching
// handler first, so a test that layered its own detail stub under this one
// would silently get the discounted fixture back.
async function openRun(page: Page, detail: ReturnType<typeof runDetail> = runDetail()) {
	await page.route(
		(url) => url.pathname === RUNS_LIST_PATH,
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [
						{
							id: RUN_ID,
							status: 'draft',
							total_amount: '1016.26',
							currency: 'USD',
							executed_at: null,
							created_at: '2026-10-01T10:00:00Z',
							payment_count: 1
						}
					],
					total: 1,
					page: 1,
					page_size: 100
				})
			})
	);
	await page.route(
		(url) => url.pathname === `${RUNS_LIST_PATH}${RUN_ID}`,
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(detail)
			})
	);
	await page.goto('/payments?tab=runs');
	await page.getByRole('button', { name: `View payment run ${RUN_ID.slice(0, 8)}` }).click();
	const dialog = page.getByRole('dialog', { name: 'Payment run' });
	await expect(dialog).toBeVisible({ timeout: 10_000 });
	return dialog;
}

test.describe('Accepted early-payment discount — what the payer sees', () => {
	test('the run dialog shows the discount taken beside a total that excludes it', async ({
		page
	}) => {
		const dialog = await openRun(page);
		await expect(dialog.getByTestId('run-total')).toContainText('1,016.26');
		await expect(dialog.getByTestId('run-discount-total')).toContainText('20.74');
		await expect(dialog.getByTestId('run-payment-discount')).toContainText('20.74');
	});

	test('a run with no discount shows no discount line', async ({ page }) => {
		const detail = { ...runDetail(), discount_total: '0' };
		detail.payments = detail.payments.map((p) => ({
			...p,
			amount: '1037.00',
			discount_amount: null,
			discount_offer_id: null
		}));
		const dialog = await openRun(page, detail);
		await expect(dialog.getByTestId('run-payment-amount')).toBeVisible();
		await expect(dialog.getByTestId('run-discount-total')).toHaveCount(0);
		await expect(dialog.getByTestId('run-payment-discount')).toHaveCount(0);
	});

	test('a queue row with an accepted offer names what the run would pay', async ({ page }) => {
		let injected: string | null = null;
		await page.route((url) => url.pathname === '/api/payments/queue', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			const response = await route.fetch();
			const body = (await response.json()) as {
				items?: Record<string, unknown>[];
			};
			const row = (body.items ?? []).find((i) => !i.blocked);
			if (row) {
				row.accepted_discount_amount = '20.74';
				row.accepted_discount_pay_by = '2099-12-31';
				row.payable_amount = '1016.26';
				injected = String(row.invoice_number);
			}
			await route.fulfill({ response, json: body });
		});
		await page.goto('/payments');
		const chip = page.getByTestId('queue-accepted-discount').first();
		await expect(chip).toBeVisible({ timeout: 10_000 });
		expect(injected).not.toBeNull();
		await expect(chip).toContainText('1,016.26');
		await expect(chip).toContainText('2099');
	});
});
