import { expect, test } from '../fixtures/helpers';

/**
 * `/payments` summary + queue must name the currency and admit the exclusions.
 *
 * `Payment.amount` is INVOICE currency — the home-currency debit lives on
 * `source_amount` — so the backend's `total_paid` / `total_pending` used to be
 * face-value sums across currencies. They now convert into the org's reporting
 * currency and declare what they could not convert (`currency`,
 * `unconverted_payment_count`; `unconverted_count` on the queue).
 *
 * The frontend half is the part this pins, and it is two separate failures:
 *
 *   1. The figures rendered through `formatCurrency` with NO currency argument,
 *      so they fell back to the org default. That is right until the day the
 *      backend's reporting currency and the org default disagree, at which
 *      point the page confidently labels a figure with the wrong symbol —
 *      worse than not labelling it, because it looks authoritative.
 *   2. A payment with no rate is silently absent from the total. The number is
 *      honest but incomplete, and nothing said so. Same class of bug as
 *      `/discounts`' excluded foreign offers and `/cfo`'s unconverted outflows,
 *      and deliberately the same notice.
 *
 * Both responses are stubbed: the assertion is about what the page does with
 * the contract, and seeding a genuinely unconvertible payment would need an FX
 * adapter with a deliberate hole in it.
 */

const SUMMARY_PATH = '/api/payments/summary';
const QUEUE_PATH = '/api/payments/queue';

test.describe('/payments — reporting currency and FX exclusions', () => {
	test('the summary figures carry the currency the backend converted into', async ({ page }) => {
		await page.route(
			(url) => url.pathname === SUMMARY_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						total_paid: '1234.50',
						total_pending: '99.00',
						payment_count: 3,
						total_rebates: '0',
						queue_count: 0,
						// Deliberately NOT the seeded org default, so a page that
						// ignores the field renders a different symbol.
						currency: 'EUR',
						unconverted_payment_count: 0
					})
				})
		);

		await page.goto('/payments');

		const paid = page.locator('.scard').filter({ hasText: 'Total Paid' }).locator('.scard-value');
		await expect(paid).toBeVisible();
		// Shape, not an exact glyph: Intl may render EUR as "€" or "EUR"
		// depending on the runtime's locale data, but it can never render it as
		// a dollar sign.
		await expect(paid).toContainText(/€|EUR/);
		await expect(paid).not.toContainText('$');

		// No exclusions reported → no notice. A caveat that shows when there is
		// nothing to caveat trains people to ignore it.
		await expect(page.getByTestId('unconverted-payments')).toHaveCount(0);
	});

	test('a summary that names no currency renders bare, not in the org default', async ({
		page
	}) => {
		// The page's `formatCurrency` used to write `currency ?? orgCurrency.currency`,
		// so a response that did not state its denomination still wore the
		// seeded org's `$` — the one symbol nothing in the payload established.
		// `formatMoney` renders an unstated code bare now (decisions §196).
		await page.route(
			(url) => url.pathname === SUMMARY_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						total_paid: '1234.50',
						total_pending: '99.00',
						payment_count: 3,
						total_rebates: '0',
						queue_count: 0,
						currency: null,
						unconverted_payment_count: 0
					})
				})
		);

		await page.goto('/payments');

		const paid = page.locator('.scard').filter({ hasText: 'Total Paid' }).locator('.scard-value');
		await expect(paid).toBeVisible();
		await expect(paid).toContainText(/1[,.]?234[.,]50/);
		await expect(paid).not.toContainText(/[$€£¥]|USD|EUR/);
	});

	test('an unconvertible payment is admitted rather than silently dropped', async ({ page }) => {
		await page.route(
			(url) => url.pathname === SUMMARY_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						total_paid: '1000.00',
						total_pending: '0',
						payment_count: 4,
						total_rebates: '0',
						queue_count: 0,
						currency: 'USD',
						unconverted_payment_count: 2
					})
				})
		);

		await page.goto('/payments');

		const notice = page.getByTestId('unconverted-payments');
		await expect(notice).toBeVisible();
		// It must name the count and the currency, or it is a shrug rather than
		// an explanation.
		await expect(notice).toContainText('2');
		await expect(notice).toContainText('USD');
		// Screen-reader parity with the /discounts + /cfo notices.
		await expect(notice).toHaveAttribute('role', 'alert');
	});

	test('the queue says when its own totals leave rows out', async ({ page }) => {
		await page.route(
			(url) => url.pathname === QUEUE_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ items: [], unconverted_count: 3 })
				})
		);

		await page.goto('/payments');

		const notice = page.getByTestId('unconverted-queue');
		await expect(notice).toBeVisible();
		await expect(notice).toContainText('3');
		await expect(notice).toHaveAttribute('role', 'alert');
	});

	test('a backend without the new fields renders exactly as before', async ({ page }) => {
		// The fields are optional on the wire, so an older backend must not
		// produce an empty notice or an "undefined" currency label.
		await page.route(
			(url) => url.pathname === SUMMARY_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						total_paid: '10.00',
						total_pending: '0',
						payment_count: 1,
						total_rebates: '0',
						queue_count: 0
					})
				})
		);
		await page.route(
			(url) => url.pathname === QUEUE_PATH,
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ items: [] })
				})
		);

		await page.goto('/payments');

		const paid = page.locator('.scard').filter({ hasText: 'Total Paid' }).locator('.scard-value');
		await expect(paid).toBeVisible();
		await expect(paid).not.toContainText('undefined');
		await expect(page.getByTestId('unconverted-payments')).toHaveCount(0);
		await expect(page.getByTestId('unconverted-queue')).toHaveCount(0);
	});

	test('the run-detail modal renders each cell in the run currency, not USD', async ({ page }) => {
		const runId = '00000000-0000-4000-8000-000000000abc';
		await page.route(
			(url) => url.pathname === '/api/payments/runs/' || url.pathname.startsWith('/api/payments/runs'),
			(route) => {
				const p = new URL(route.request().url()).pathname;
				if (p === `/api/payments/runs/${runId}`) {
					return route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({
							id: runId,
							status: 'draft',
							total_amount: '250.00',
							currency: 'EUR', // deliberately not the seeded org default
							initiated_by: null,
							executed_at: null,
							created_at: new Date().toISOString(),
							requires_cfo_approval: false,
							cfo_approved_by: null,
							cfo_approved_at: null,
							payments: [
								{
									id: 'p1',
									invoice_id: 'i1',
									invoice_number: 'INV-EUR-1',
									vendor_name: 'Euro Vendor',
									amount: '250.00',
									currency: 'EUR',
									method: 'sepa',
									status: 'pending',
									reference: null
								}
							]
						})
					});
				}
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						items: [
							{
								id: runId,
								status: 'draft',
								total_amount: '250.00',
								payment_count: 1,
								created_at: new Date().toISOString()
							}
						],
						total: 1,
						page: 1,
						page_size: 100
					})
				});
			}
		);

		await page.goto('/payments?tab=runs');
		await page.getByRole('button', { name: /View payment run/ }).first().click();

		const modal = page.locator('.modal[role="dialog"][aria-label="Payment run"]');
		await expect(modal).toBeVisible();
		// Shape, not the exact glyph — but never a dollar sign.
		await expect(modal.locator('.total')).toContainText(/€|EUR/);
		await expect(modal.locator('.total')).not.toContainText('$');
		await expect(modal.locator('td.mono.right, td.right.mono')).toContainText(/€|EUR/);
	});
});
