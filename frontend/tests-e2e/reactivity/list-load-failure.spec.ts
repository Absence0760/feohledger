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
		route: '/contracts',
		apiPathname: '/api/contracts',
		notShown: /No contracts match your filters/,
		shown: /Could not load contracts/
	},
	{
		name: 'expenses',
		route: '/expenses',
		apiPathname: '/api/expenses',
		notShown: /No expenses match your filters/,
		shown: /Could not load expenses/
	},
	{
		name: 'payments (history tab)',
		route: '/payments',
		apiPathname: '/api/payments',
		notShown: /No payments match your filters/,
		shown: /Could not load payments/,
		before: async (page) => {
			await page.getByRole('button', { name: /History/ }).click();
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

	// One good response, then failures.
	//
	// Deliberately NOT `satisfies VendorChangeRequestPage`, which is the house
	// rule for a stub whose shape has a `$lib/types` counterpart: importing it
	// pulls `$lib/types/vendor.ts` into the `tsconfig.e2e.json` program, and
	// that module does `import type { BadgeTone } from '…/ui/Badge.svelte'`.
	// Plain `tsc` resolves a `.svelte` path through the ambient `*.svelte`
	// module shim, which declares no named exports, so `pnpm check:e2e` fails
	// with TS2614 on a file this spec never edits. (Durable fix: `BadgeTone`
	// belongs in a `.ts` module — 28 files import it, so that is its own change.)
	//
	// The omission is affordable HERE in a way it was not for the dashboard
	// stub that motivated the rule. That one's missing field made an assertion
	// pass vacuously; this one's assertions are `toHaveCount(1)` then
	// `toHaveCount(0)`, so a payload the page can no longer render fails the
	// first one loudly rather than quietly weakening the second.
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
	};

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
