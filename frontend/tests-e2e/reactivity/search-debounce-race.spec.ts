import { expect, test } from '../fixtures/helpers';

/**
 * Regression guard for GitHub issue #168.
 *
 * /invoices, /payments (history tab), and /vendors each have a search box
 * that's supposed to debounce at 300ms. The bug: the filter-change `$effect`
 * (status chips / advanced filters) called a params-builder that also read
 * `search` — and Svelte tracks reads transitively through called functions,
 * so that effect ended up depending on `search` too. Every keystroke re-fired
 * it immediately, bypassing the dedicated debounce timer entirely. On top of
 * that, nothing sequenced responses: a slow request for an earlier search
 * term could resolve after a faster later one and clobber the list with
 * stale results.
 *
 * The fix: the params-builder reads `search` via `untrack()` so only the
 * dedicated debounce effect (which reads `search` directly) triggers a
 * fetch, plus a monotonic request-token guard (`$lib/utils/requestSequence.ts`)
 * that discards a response superseded by a later request.
 *
 * Each route below proves both halves of the fix:
 *   (a) rapid typing coalesces into a single debounced request, not one
 *       immediate un-debounced request per keystroke.
 *   (b) a slow response for an earlier search term that resolves AFTER a
 *       faster later one is discarded — the final list stays correct.
 *
 * `/requisitions` and `/expenses` are here because their guards were written
 * locally in `tests-e2e/{requisitions,expenses}/search-scope.spec.ts` — as
 * hand-rolled copies of (a) — purely to avoid a merge conflict on this shared
 * file during a parallel round. Two copies of a technique drift; one table does
 * not. Those files keep the tests that are genuinely their own (server-side
 * search reaching a later page, the empty state, the CSV term, and the
 * requisitions sequencing test, which waits on a real issued-request signal and
 * is stronger than (b) rather than a copy of it).
 *
 * Those two routes also carry a failure mode the other four do not, and it is
 * the reason (a) has to TYPE rather than `fill()`: they hold an `appliedSearch`
 * state that cancels a pending debounce once the term it names has already been
 * requested. So an untracked-read slip there does not produce a duplicate
 * request — it produces one un-debounced request per keystroke and NO debounced
 * one, which a single-write `fill()` cannot distinguish from correct behaviour.
 */

interface RouteCase {
	name: string;
	route: string;
	apiPathname: string;
	searchPlaceholder: string;
	/** Extra setup after `page.goto(route)` before the search box is usable
	 *  (e.g. switching to the History tab on /payments). */
	beforeSearch?: (page: import('@playwright/test').Page) => Promise<void>;
	/** Why this route's response-sequencing half is NOT generated here — a
	 *  reason string, never a boolean. Set it only when a stronger test for the
	 *  same failure already exists elsewhere; generating a weaker copy beside it
	 *  is the duplication this table was consolidated to remove. */
	sequencingCoveredElsewhere?: string;
	/** A real signal that the mount fetch has RENDERED, awaited before the
	 *  debounce test's measurement window opens. Prefer this to relying on the
	 *  400ms settle wait alone: the search box is visible before the list
	 *  arrives, so without it the wait is a guess about when the mount request
	 *  lands, and a late one would show up as a phantom keystroke request. New
	 *  cases should supply it. Used by the debounce test ONLY — the sequencing
	 *  test passes unmatched terms through to the real backend, where the row
	 *  count is the tenant's, not the case's. */
	settle?: (page: import('@playwright/test').Page) => Promise<void>;
	/** Build a fulfill body for a given search term + a unique marker string
	 *  embedded in the one row returned, keyed so the test can assert on it. */
	buildBody: (searchTerm: string, marker: string) => string;
}

const CASES: RouteCase[] = [
	{
		name: 'invoices',
		route: '/invoices',
		apiPathname: '/api/invoices',
		searchPlaceholder: 'Search invoices...',
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `${marker}-id`,
						invoice_number: marker,
						vendor: 'Marker Vendor',
						description: `row for "${searchTerm}"`,
						po_number: 'PO-1',
						amount: '100.00',
						currency: 'USD',
						due_date: '2026-08-01',
						status: 'new',
						warnings: []
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		name: 'vendors',
		route: '/vendors',
		apiPathname: '/api/vendors',
		searchPlaceholder: 'Search vendors...',
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `${marker}-id`,
						name: marker,
						code: null,
						email: null,
						status: 'active',
						source: 'manual',
						invoice_count: 0,
						erp_vendor_id: null,
						bank_details: null,
						screening_status: 'unscreened',
						risk_level: 'unknown',
						payments_blocked: false
					}
				],
				total: 1
			})
	},
	{
		// Search arrived with issue #443 and carries the same `appliedSearch`
		// shape as /requisitions and /expenses (see the header), which is why
		// (a) types rather than fills here too.
		name: 'credit-memos',
		route: '/credit-memos',
		apiPathname: '/api/credit-memos',
		searchPlaceholder: 'Search memo # or vendor',
		settle: async (page) => {
			await expect(page.locator('table tbody tr')).toHaveCount(1);
		},
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: '00000000-0000-4000-b000-00000000c0de',
						memo_number: marker,
						vendor_id: '00000000-0000-4000-b001-000000000001',
						vendor_name: `vendor for "${searchTerm}"`,
						invoice_id: null,
						invoice_number: null,
						amount: 25,
						currency: 'USD',
						issued_date: '2026-01-01',
						reason: null,
						status: 'open',
						applied_at: null,
						applied_by: null,
						created_at: '2026-01-01T00:00:00Z'
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		// The #168 fix was originally applied to /invoices, /payments and
		// /vendors only. /recurring (and its siblings /contracts, /budgets,
		// /intake) carried the identical bug — their filter `$effect` called
		// `buildParams()` / `syncUrl()`, both of which read `search` — until the
		// sequencer sweep. This case is the guard that it stays fixed.
		name: 'recurring',
		route: '/recurring',
		apiPathname: '/api/recurring',
		searchPlaceholder: 'Search templates...',
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `${marker}-id`,
						name: marker,
						vendor_id: null,
						vendor_name: `vendor for "${searchTerm}"`,
						description: null,
						amount: 100,
						currency: 'USD',
						gl_account: null,
						cost_center: null,
						department: null,
						project: null,
						po_number: null,
						payment_terms: null,
						cadence: 'monthly',
						day_of_period: 1,
						start_date: '2026-01-01',
						end_date: null,
						next_run_on: '2026-09-01',
						last_period_key: null,
						last_generated_at: null,
						generated_count: 0,
						status: 'active',
						variance_tolerance_pct: null,
						notes: null,
						created_at: '2026-01-01T00:00:00Z',
						updated_at: null
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		// Folded in from `tests-e2e/requisitions/search-scope.spec.ts`, which
		// hand-rolled this same measurement locally. See the header for the
		// `appliedSearch` failure mode this route and /expenses share.
		name: 'requisitions',
		route: '/requisitions',
		apiPathname: '/api/requisitions',
		searchPlaceholder: 'Search requisitions...',
		// `tests-e2e/requisitions/search-scope.spec.ts` already guards the
		// sequencing half, and does it better: it waits on a real
		// "the held request was issued" signal rather than guessing when the
		// debounce fired, and asserts row counts rather than the absence of a
		// marker string. Generating the generic copy alongside it would put two
		// tests for one failure back into the tree.
		sequencingCoveredElsewhere:
			'tests-e2e/requisitions/search-scope.spec.ts — signal-driven, stronger',
		settle: async (page) => {
			await expect(page.locator('table tbody tr.clickable')).toHaveCount(1);
		},
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `00000000-0000-4000-9000-${marker}`,
						requisition_number: marker,
						title: `row for "${searchTerm}"`,
						requester_user_id: '00000000-0000-4000-9999-000000000001',
						department: 'Engineering',
						status: 'draft',
						needed_by: null,
						justification: null,
						vendor_id: null,
						contract_id: null,
						budget_id: null,
						total: 100,
						currency: 'USD',
						notes: null,
						submitted_at: null,
						approved_at: null,
						approved_by: null,
						rejection_reason: null,
						converted_po_id: null,
						line_items: [],
						created_at: '2026-03-01T00:00:00Z',
						updated_at: '2026-03-01T00:00:00Z'
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		// Folded in from `tests-e2e/expenses/search-scope.spec.ts`. No
		// `beforeSearch`: /expenses is a five-tab page and the search box lives
		// inside the Expenses panel, which is the DEFAULT tab — so this case
		// depends on that default rather than on a click, unlike /payments.
		name: 'expenses',
		route: '/expenses',
		apiPathname: '/api/expenses',
		searchPlaceholder: 'Search expenses...',
		settle: async (page) => {
			await expect(page.locator('table tbody tr.clickable')).toHaveCount(1);
		},
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `00000000-0000-4000-8000-${marker}`,
						report_id: null,
						expense_date: '2026-03-01',
						merchant: marker,
						category: 'Travel',
						description: `row for "${searchTerm}"`,
						amount: 11,
						currency: 'USD',
						converted_amount: null,
						converted_currency: null,
						converted_fx_rate: null,
						converted_fx_locked_at: null,
						gl_account_id: null,
						receipt_file_key: null,
						receipt_url: null,
						payment_method: 'out_of_pocket',
						card_transaction_id: null,
						policy_violations: null,
						status: 'draft',
						reimbursable: true,
						mileage_miles: null,
						created_at: '2026-03-01T00:00:00Z',
						updated_at: '2026-03-01T00:00:00Z'
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		// The exceptions queue gained its search box in GitHub #443. Its chip
		// effect calls `reload()`, which reaches `loadExceptions` AND
		// `loadSummary` synchronously — two functions deep, where a tracked
		// `search` read is easiest to reintroduce — and it keeps the
		// `appliedSearch` guard, so this has to type rather than `fill()`.
		name: 'exceptions',
		route: '/exceptions',
		apiPathname: '/api/exceptions',
		searchPlaceholder: 'Search invoice # or vendor...',
		settle: async (page) => {
			await expect(page.getByRole('cell', { name: 'ROW', exact: true })).toBeVisible();
		},
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `${marker}-id`,
						invoice_id: null,
						invoice_number: marker,
						vendor_name: `vendor for "${searchTerm}"`,
						amount: 100,
						currency: 'USD',
						exception_type: 'duplicate',
						type_label: 'Duplicate Invoice',
						severity: 'warning',
						description: null,
						status: 'open',
						resolution: null,
						resolved_by: null,
						resolved_at: null,
						assigned_to: null,
						assigned_to_user_id: null,
						due_at: null,
						is_overdue: false,
						time_to_resolution_hours: null,
						created_at: '2026-03-01T00:00:00Z'
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	},
	{
		name: 'payments (history tab)',
		route: '/payments',
		apiPathname: '/api/payments',
		searchPlaceholder: 'Search payments...',
		beforeSearch: async (page) => {
			await page.getByRole('button', { name: /History/ }).click();
		},
		buildBody: (searchTerm, marker) =>
			JSON.stringify({
				items: [
					{
						id: `${marker}-id`,
						invoice_number: marker,
						vendor_name: 'Marker Vendor',
						method: 'ach',
						amount: '50.00',
						currency: 'USD',
						status: 'completed',
						reference: `ref-${searchTerm}`,
						created_at: '2026-07-01T00:00:00Z'
					}
				],
				total: 1,
				page: 1,
				page_size: 20
			})
	}
];

for (const c of CASES) {
	test.describe(`${c.name} search — debounce + response sequencing (#168)`, () => {
		test(`${c.name}: rapid typing does not fire one un-debounced request per keystroke`, async ({
			page
		}) => {
			const searchTerms: string[] = [];
			await page.route(`**${c.apiPathname}?*`, async (route) => {
				const url = new URL(route.request().url());
				if (url.pathname !== c.apiPathname) {
					await route.continue();
					return;
				}
				const term = url.searchParams.get('search') ?? '';
				searchTerms.push(term);
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: c.buildBody(term, 'ROW')
				});
			});

			await page.goto(c.route);
			if (c.beforeSearch) await c.beforeSearch(page);

			const search = page.getByPlaceholder(c.searchPlaceholder);
			await expect(search).toBeVisible();
			// A real "the mount fetch rendered" signal where the case offers
			// one — the box is visible well before the list arrives.
			if (c.settle) await c.settle(page);

			// Let whatever mount-time fetch(es) fire and settle before measuring.
			await page.waitForTimeout(400);
			searchTerms.length = 0;

			// Type "acme" one keystroke at a time, well inside the 300ms debounce
			// window — a correctly debounced box fires nothing until typing stops.
			await search.pressSequentially('acme', { delay: 30 });

			// Comfortably under 300ms since the last keystroke: no request yet.
			await page.waitForTimeout(150);
			expect(
				searchTerms,
				'no immediate un-debounced request fired per keystroke'
			).toEqual([]);

			// Past the 300ms debounce: exactly one coalesced request, for the
			// FINAL value — not one per keystroke ("a", "ac", "acm", "acme").
			await page.waitForTimeout(300);
			expect(searchTerms, 'exactly one debounced request for the final term').toEqual([
				'acme'
			]);
		});

		// Not `test.skip` — this is not a masked failure but a test we decline
		// to GENERATE, because a stronger one for the same failure already
		// exists (see `sequencingCoveredElsewhere`).
		if (c.sequencingCoveredElsewhere) return;

		test(`${c.name}: a stale slow response cannot clobber a fresher one that resolves first`, async ({
			page
		}) => {
			let releaseStale: () => void = () => {};
			const staleGate = new Promise<void>((resolve) => (releaseStale = resolve));

			await page.route(`**${c.apiPathname}?*`, async (route) => {
				const url = new URL(route.request().url());
				if (url.pathname !== c.apiPathname) {
					await route.continue();
					return;
				}
				const term = url.searchParams.get('search') ?? '';
				if (term === 'acm') {
					// Hold this response open — simulates a slow query for the
					// earlier, superseded search term.
					await staleGate;
					await route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: c.buildBody(term, 'STALE-ACM')
					});
					return;
				}
				if (term === 'acme') {
					await route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: c.buildBody(term, 'FRESH-ACME')
					});
					return;
				}
				await route.continue();
			});

			await page.goto(c.route);
			if (c.beforeSearch) await c.beforeSearch(page);

			const search = page.getByPlaceholder(c.searchPlaceholder);
			await expect(search).toBeVisible();
			// No `c.settle` here: this test's handler deliberately passes every
			// term but 'acm'/'acme' through to the real backend, so the mount
			// response is the shard tenant's own data and its row count is not
			// something a case can state. `settle` describes the fully-stubbed
			// world of the test above.
			await page.waitForTimeout(400); // let the mount-time fetch(es) settle

			// Type "acm" and let its debounced request fire (and hang, gated).
			await search.fill('acm');
			await page.waitForTimeout(350);

			// Now type the rest — "acme" — and let ITS debounced request fire
			// and resolve immediately (faster than the still-pending "acm" one).
			await search.fill('acme');
			await page.waitForTimeout(350);

			await expect(page.getByText('FRESH-ACME')).toBeVisible();

			// Release the slow, now-stale "acm" response. If sequencing is
			// broken, this overwrites the list with the stale row.
			releaseStale();
			await page.waitForTimeout(300);

			await expect(page.getByText('FRESH-ACME')).toBeVisible();
			await expect(page.getByText('STALE-ACM')).toHaveCount(0);
		});
	});
}
