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
