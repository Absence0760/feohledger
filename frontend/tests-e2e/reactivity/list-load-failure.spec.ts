import type { Payment } from '#lib/types/payment.ts';
import type { VendorChangeRequestPage } from '#lib/types/vendor.ts';

import { expect, test } from '../fixtures/helpers';

/**
 * A failed list load must not read as an empty result set.
 *
 * The invoices / payments / contracts / expenses stores had no `catch`: a 500
 * or an offline backend left the list empty and the table rendered
 * "No … match your filters." — an outage indistinguishable from a filter that
 * matched nothing, on four of the app's busiest surfaces. Each store now
 * records `errored` (re-throwing, so callers that await a refresh keep their
 * own handling) and each page picks its empty message off it, the way
 * /notifications and /exceptions already did.
 */

interface Case {
	name: string;
	route: string;
	apiPathname: string;
	/** Text that must NOT appear (the "nothing matched" lie). */
	notShown: RegExp;
	/** Text that must appear instead. */
	shown: RegExp;
	/** Extra setup after landing on the route (e.g. switching tab). */
	before?: (page: import('@playwright/test').Page) => Promise<void>;
	/** The page renders `ui/LoadError`, so the error carries a Retry. */
	retry?: true;
}

const CASES: Case[] = [
	{
		name: 'invoices',
		route: '/invoices',
		apiPathname: '/api/invoices',
		notShown: /No invoices match your filters/,
		shown: /Could not load invoices/
	},
	{
		name: 'contracts',
		retry: true,
		route: '/contracts',
		apiPathname: '/api/contracts',
		notShown: /No contracts match your filters/,
		shown: /Could not load contracts/
	},
	{
		name: 'expenses',
		retry: true,
		route: '/expenses',
		apiPathname: '/api/expenses',
		notShown: /No expenses match your filters/,
		shown: /Could not load expenses/
	},
	{
		name: 'payments (history tab)',
		retry: true,
		route: '/payments',
		apiPathname: '/api/payments',
		notShown: /No payments match your filters/,
		shown: /Could not load payments/,
		before: async (page) => {
			await page.getByRole('tab', { name: /History/ }).click();
		}
	},
	{
		// The queue was the last payments list still conflating the three states.
		// It matters most here: "No invoices ready for payment." is a claim about
		// MONEY OWED, and it was rendered both while the fetch was in flight and
		// permanently after a failed one. Queue is the default tab, so no `before`.
		name: 'payments (queue tab)',
		route: '/payments',
		apiPathname: '/api/payments/queue',
		notShown: /No invoices ready for payment/,
		shown: /Could not load the payment queue/
	},
	{
		// The chart of accounts joins the sweep with its page rather than after
		// it: "No accounts in this chart." is a claim a clerk acts on by asking
		// someone to sync the ERP, and it must not be what an outage looks like.
		name: 'gl accounts',
		route: '/gl-accounts',
		apiPathname: '/api/gl-accounts',
		notShown: /No accounts in this chart/,
		shown: /Couldn.t load the chart of accounts/
	},
	{
		// The dual-control bank-change queue. It is the highest-stakes list in
		// the sweep: "Nothing is waiting for approval." is a claim that no
		// supplier's payment details are pending a second signature, and an
		// approver who believes it stops looking.
		name: 'vendor change requests',
		route: '/vendors/change-requests',
		apiPathname: '/api/vendors/change-requests',
		notShown: /Nothing is waiting for approval/,
		shown: /Could not load the change-request queue/
	},
	// The procure-to-pay lists. Each toasted its failure and then let the
	// empty line stand — "No purchase orders." is what a clerk reads before
	// keying an invoice as non-PO spend.
	{
		name: 'purchase orders',
		retry: true,
		route: '/purchase-orders',
		apiPathname: '/api/purchase-orders',
		notShown: /No purchase orders\./,
		shown: /Could not load purchase orders/
	},
	{
		name: 'goods receipts',
		retry: true,
		route: '/goods-receipts',
		apiPathname: '/api/goods-receipts',
		notShown: /No goods receipts\./,
		shown: /Could not load goods receipts/
	},
	{
		name: 'requisitions',
		retry: true,
		route: '/requisitions',
		apiPathname: '/api/requisitions',
		notShown: /No requisitions match your filters/,
		shown: /Could not load requisitions/
	},
	{
		name: 'intake',
		retry: true,
		route: '/intake',
		apiPathname: '/api/intake',
		notShown: /No intake requests match your filters/,
		shown: /Could not load intake requests/
	},
	{
		name: 'budgets',
		retry: true,
		route: '/budgets',
		apiPathname: '/api/budgets',
		notShown: /No budgets match your filters/,
		shown: /Could not load budgets/
	},
	{
		retry: true,
		name: 'vendor statements',
		route: '/vendor-statements',
		apiPathname: '/api/vendor-statements',
		notShown: /No reconciliations/,
		shown: /Failed to load reconciliations/
	},
	{
		retry: true,
		name: 'positive pay',
		route: '/positive-pay',
		apiPathname: '/api/positive-pay',
		notShown: /No Positive Pay files/,
		shown: /Failed to load Positive Pay files/
	},
	{
		retry: true,
		name: 'discount offers',
		route: '/discounts',
		apiPathname: '/api/discounts/offers',
		notShown: /No discount offers match this filter/,
		shown: /Failed to load discount offers/
	},
	// The four /expenses sub-tabs only toasted a failed load and left their
	// "No … yet." empty line standing; each now renders `ui/LoadError`.
	{
		retry: true,
		name: 'expense reports (reports tab)',
		route: '/expenses?tab=reports',
		apiPathname: '/api/expense-reports',
		notShown: /No expense reports yet/,
		shown: /Failed to load expense reports/
	},
	{
		retry: true,
		name: 'expense policies (policies tab)',
		route: '/expenses?tab=policies',
		apiPathname: '/api/expense-policies',
		notShown: /No expense policies yet/,
		shown: /Failed to load policies/
	},
	{
		retry: true,
		name: 'expense pre-approvals (pre-approvals tab)',
		route: '/expenses?tab=preapprovals',
		apiPathname: '/api/expense-preapprovals',
		notShown: /No pre-approval requests/,
		shown: /Failed to load pre-approvals/
	},
	{
		retry: true,
		name: 'card transactions (cards tab)',
		route: '/expenses?tab=cards',
		apiPathname: '/api/corporate-card-transactions',
		notShown: /No card transactions/,
		shown: /Failed to load card transactions/
	}
];

for (const c of CASES) {
	test(`${c.name}: a failed load renders an error, not "nothing matched"`, async ({ page }) => {
		// `*` not `?*`: `/api/payments/queue` is fetched with no query string at
		// all, so a glob requiring one never intercepts it. Widening is safe —
		// the handler below continues anything whose pathname isn't an exact
		// match, which is what already kept `/api/payments` off `/api/payments/queue`.
		await page.route(`**${c.apiPathname}*`, async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== c.apiPathname) {
				await route.continue();
				return;
			}
			await route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		});

		await page.goto(c.route);
		if (c.before) await c.before(page);

		await expect(page.getByText(c.shown)).toBeVisible();
		await expect(page.getByText(c.notShown)).toHaveCount(0);
		// A way back, not just a message: `ui/LoadError`'s Retry is the only
		// route off the dead end once the toast is dismissed.
		if (c.retry) await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
	});
}

/**
 * The other half of the same bug: a load that fails AFTER a good one.
 *
 * The sweep above only exercises the first load, where an empty list and a
 * cleared list look identical. The failure that actually ships is the second
 * one — a chip click whose fetch 500s — because `errored` reaches the reader
 * only through `DataTable`'s `empty` message, which renders on `isEmpty`
 * alone. A route that sets `errored` without clearing its rows therefore shows
 * the PREVIOUS filter's rows as the answer to a filter it never ran, with
 * nothing but a toast that fades.
 *
 * `/vendors/change-requests` is the dual-control bank-change queue, so the
 * stale rows are proposed changes to where a supplier's money goes, labelled
 * with a status they do not have. That is why this one gets its own test
 * rather than only a sweep row.
 */
test('vendor change requests: a failed RE-load clears the previous filter\'s rows', async ({
	page
}) => {
	const PATHNAME = '/api/vendors/change-requests';
	const STALE_VENDOR = 'Stale Queue Vendor';

	// One good response, then failures. Pinned to the type the page reads
	// (`satisfies`, the house rule for a stub with a `#lib/types` counterpart),
	// so a new field on the queue payload is a compile error here rather than a
	// stub the page quietly stops rendering.
	const pendingPage = {
		items: [
			{
				id: '00000000-0000-4000-8000-0000000000aa',
				vendor_id: '00000000-0000-4000-8000-0000000000bb',
				vendor_name: STALE_VENDOR,
				change_type: 'bank_details',
				status: 'pending',
				proposed_value: { bank_account_last4: '4321' },
				requested_by_vendor_user_id: null,
				requested_by_user_id: null,
				reviewed_by_user_id: null,
				reviewed_at: null,
				review_note: null,
				created_at: new Date().toISOString()
			}
		],
		total: 1,
		page: 1,
		page_size: 25
	} satisfies VendorChangeRequestPage;

	let served = 0;
	await page.route(`**${PATHNAME}*`, async (route) => {
		// Exact pathname only — `/counts` hangs off the same prefix and the
		// page's chip tallies must keep loading normally.
		if (new URL(route.request().url()).pathname !== PATHNAME) {
			await route.continue();
			return;
		}
		served += 1;
		if (served === 1) {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(pendingPage)
			});
			return;
		}
		await route.fulfill({
			status: 500,
			contentType: 'application/json',
			body: JSON.stringify({ detail: 'boom' })
		});
	});

	await page.goto('/vendors/change-requests');

	// The good load landed — this is the row that must not survive the next one.
	await expect(page.getByTestId('change-request-row')).toHaveCount(1);
	await expect(page.getByText(STALE_VENDOR)).toBeVisible();

	// A chip click re-queries the server, and that query fails.
	// Non-exact: the chip's accessible name carries its tally once
	// `/counts` lands ("Rejected 3").
	await page.getByRole('button', { name: /^Rejected/ }).click();

	await expect(page.getByText(/Could not load the change-request queue/)).toBeVisible();
	await expect(page.getByTestId('change-request-row')).toHaveCount(0);
	await expect(page.getByText(STALE_VENDOR)).toHaveCount(0);
	// ...and the footer must not keep reporting the stale tally as this
	// filter's answer either.
	await expect(page.getByText(/Showing all 1/)).toHaveCount(0);
});

/**
 * Retry has to be a real retry, every time — not only the first.
 *
 * `ui/LoadError` expects the caller's error flag to clear when the request
 * STARTS: that unmounts the block (the loading line shows while the request is
 * out), and a second failure mounts a fresh `role="alert"`, which is what a
 * screen reader announces. The contracts / expenses / payments stores cleared
 * `errored` only on success, so Retry left the block standing — a silent no-op
 * to look at — and a second failure changed nothing, so it was never announced.
 *
 * The second response is HELD until the test has seen the block go: that is
 * the observable difference, since the block reads the same before and after.
 * Requests are answered by a `mode` the test sets, not by a count, so an extra
 * mount-time fetch cannot shift which response is which.
 */
const EMPTY_LIST = JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 });

interface RetryCase {
	name: string;
	route: string;
	apiPathname: string;
	errorText: RegExp;
}

const RETRY_CASES: RetryCase[] = [
	{
		name: 'contracts',
		route: '/contracts',
		apiPathname: '/api/contracts',
		errorText: /Could not load contracts/
	},
	{
		name: 'payments (history tab)',
		route: '/payments?tab=history',
		apiPathname: '/api/payments',
		errorText: /Could not load payments/
	}
];

for (const c of RETRY_CASES) {
	test(`${c.name}: a Retry that fails a second time still shows the error (and recovers on the third)`, async ({
		page
	}) => {
		let mode: 'fail' | 'hold-then-fail' | 'ok' = 'fail';
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});

		await page.route(`**${c.apiPathname}*`, async (route) => {
			// Exact pathname only — `/api/payments/counts`, `/queue`, `/summary`
			// hang off the same prefix and keep loading normally.
			if (new URL(route.request().url()).pathname !== c.apiPathname) {
				await route.continue();
				return;
			}
			if (mode === 'ok') {
				await route.fulfill({ status: 200, contentType: 'application/json', body: EMPTY_LIST });
				return;
			}
			if (mode === 'hold-then-fail') await held;
			await route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		});

		await page.goto(c.route);

		const alert = page.getByRole('alert').filter({ hasText: c.errorText });
		await expect(alert).toBeVisible();

		// Retry #1: the block must come down while the request is out.
		mode = 'hold-then-fail';
		await alert.getByRole('button', { name: 'Retry' }).click();
		await expect(alert).toHaveCount(0);

		// ...and come back when that request fails too.
		release();
		await expect(alert).toBeVisible();

		// Retry #2 succeeds: the error is gone and the (empty) table stands.
		mode = 'ok';
		await alert.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('table-empty')).toBeVisible();
		await expect(alert).toHaveCount(0);
	});
}

/**
 * /payments History: a failed RE-load replaces the previous filter's rows.
 *
 * The History error used to be gated on an empty list
 * (`paymentStore.errored && all.length === 0`), but the store keeps its rows
 * on a failed fetch — so a chip click whose request 500'd left the PREVIOUS
 * filter's payments under the new chip, with nothing saying they were stale.
 * It now gates like /invoices: errored and not loading.
 */
test('payments history: a failed RE-load replaces the previous filter\'s rows', async ({
	page
}) => {
	const PATHNAME = '/api/payments';
	const STALE_VENDOR = 'Stale History Vendor';

	const stalePayment = {
		id: '00000000-0000-4000-8000-0000000000c1',
		correlation_id: null,
		invoice_id: '00000000-0000-4000-8000-0000000000c2',
		payment_run_id: null,
		amount: 125,
		method: 'ach',
		status: 'completed',
		reference: null,
		created_at: new Date().toISOString(),
		updated_at: null,
		currency: 'USD',
		vendor_name: STALE_VENDOR,
		invoice_number: 'INV-STALE-1',
		card_last_four: null,
		card_provider: null,
		card_id: null,
		void_refusal: null
	} satisfies Payment;

	// Answered by mode, not by count, so a second mount-time fetch still gets
	// the good page.
	let failing = false;
	await page.route(`**${PATHNAME}*`, async (route) => {
		if (new URL(route.request().url()).pathname !== PATHNAME) {
			await route.continue();
			return;
		}
		if (!failing) {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [stalePayment], total: 1, page: 1, page_size: 20 })
			});
			return;
		}
		await route.fulfill({
			status: 500,
			contentType: 'application/json',
			body: JSON.stringify({ detail: 'boom' })
		});
	});

	await page.goto('/payments?tab=history');

	// The good load landed — this is the row that must not survive the next one.
	await expect(page.getByText(STALE_VENDOR)).toBeVisible();

	// A chip click re-queries the server, and that query fails. Non-exact: the
	// chip's accessible name carries its tally once `/counts` lands.
	failing = true;
	await page.getByRole('button', { name: /^Failed/ }).click();

	await expect(page.getByTestId('history-error')).toContainText('Could not load payments.');
	await expect(page.getByText(STALE_VENDOR)).toHaveCount(0);
	await expect(page.getByText(/Showing all 1/)).toHaveCount(0);
});
