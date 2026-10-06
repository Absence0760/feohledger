import type { Locator, Page, Route } from '@playwright/test';

import { emptyTenantDashboardResponse } from '../dashboard/fixture';
import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * Accessibility regression guard for the **KPI "no figure yet" affordance**
 * (`#lib/components/ui/KpiCard.svelte` + `#lib/utils/kpiValue.ts`).
 *
 * The convention this guards, in the order the component applies it:
 *
 *   - A figure that does not exist renders `KPI_NO_FIGURE` (an em dash), never
 *     a coerced `0` — `docs/decisions.md` §34 in a display layer.
 *   - **Whether it is missing because it is still arriving is ANNOUNCED, not
 *     drawn.** A pending card is pixel-identical to an unavailable one; it
 *     differs only in `aria-busy="true"`, an `aria-hidden` dash, and a
 *     screen-reader-only "Loading…" in the dash's place. A placeholder must
 *     never be read out as a value.
 *   - A pending or unavailable card drops its highlight tint, because the tint
 *     is a *verdict* and there is no figure to have a verdict about.
 *   - A genuine computed `0` is a figure: it renders as one, keeps its verdict
 *     tint, and is never announced as pending. The classification is nullish,
 *     not falsy.
 *
 * **Why this spec exists separately from `axe.spec.ts`.** That suite scans
 * routes; this scans *states*. Every assertion below needs the KPI response
 * held or shaped, and none of the routes below reaches the pending state for
 * long enough to be scanned incidentally — a route list would report the page
 * clean while the busy affordance went entirely unchecked. It is the same
 * argument `deemphasised-rows.spec.ts` makes for de-emphasised rows.
 *
 * **Why axe alone would not be enough here.** axe cannot see the interesting
 * part: a dash left inside the accessibility tree while the card claims to be
 * busy is valid ARIA and violates no rule — it just reads "em dash" to a
 * screen-reader user where a number belongs. So each test asserts the
 * *announced* content directly, via Playwright's accessibility-tree snapshot
 * (`locator.ariaSnapshot()`), and runs axe on top of that.
 *
 * The pending state is reached by holding the route's own KPI response on a
 * promise released by hand — the pattern
 * `tests-e2e/admin/api-keys.spec.ts`'s usage-identity test establishes. No
 * sleep, no timeout, no race: the readiness gate is the rendered
 * `data-kpi-state`, and the response is released only once the pending
 * assertions have run.
 */

/** `kpiValue.ts::KPI_NO_FIGURE` — U+2014, the glyph a card shows for no figure. */
const NO_FIGURE = '—';

/** `common.loading` — the screen-reader-only text substituted for the dash. */
const LOADING = 'Loading…';

/**
 * Hold every request matching `pathname` until `release()` is called, then let
 * the REAL backend answer it.
 *
 * Passing the real response through (rather than a fixture) is deliberate for
 * the pending→settled tests: the settled half is then the page's genuine data,
 * so the same test covers "the pending state is clean" and "the state the user
 * actually lands on is clean" without inventing a payload.
 *
 * `body` overrides that for the one case where the SETTLED state is the thing
 * being pinned and the tenant's own data cannot produce it — the dashboard's
 * zero-invoice hand-off, where the seeded tenant has invoices and the state
 * under test is the one it will never be in. Held and stubbed has to be the
 * same handler: two routes on one path means the first installed wins, so
 * `stubEndpoint` beside `holdEndpoint` would silently answer immediately and
 * the pending half of the assertion would never be reached.
 *
 * The glob is broader than the path, so the handler re-checks the pathname and
 * lets anything else through — the guard `tests-e2e/requisitions/search-scope`
 * documents.
 */
function holdEndpoint(
	page: Page,
	pathname: string,
	body?: unknown
): { release: () => void; install: () => Promise<void> } {
	let release!: () => void;
	const released = new Promise<void>((resolve) => {
		release = resolve;
	});
	const install = async () => {
		await page.route(`**${pathname}*`, async (route: Route) => {
			if (new URL(route.request().url()).pathname !== pathname) {
				await route.continue();
				return;
			}
			await released;
			if (body !== undefined) {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(body)
				});
				return;
			}
			const response = await route.fetch();
			await route.fulfill({ response });
		});
	};
	return { release, install };
}

/** Fulfil `pathname` with a fixed body; everything else passes through. */
async function stubEndpoint(
	page: Page,
	pathname: string,
	body: unknown,
	status = 200
): Promise<void> {
	await page.route(`**${pathname}*`, async (route: Route) => {
		if (new URL(route.request().url()).pathname !== pathname) {
			await route.continue();
			return;
		}
		await route.fulfill({
			status,
			contentType: 'application/json',
			body: JSON.stringify(body)
		});
	});
}

/**
 * Every card in `row` is announcing itself as busy, and none of them is
 * announcing its placeholder as a value.
 */
async function expectRowPending(row: Locator, count: number): Promise<void> {
	const cards = row.locator('.kpi');
	await expect(cards).toHaveCount(count);
	await expect(row.locator('.kpi[data-kpi-state="pending"]')).toHaveCount(count);
	await expect(row.locator('.kpi[aria-busy="true"]')).toHaveCount(count);

	for (let i = 0; i < count; i++) {
		const value = cards.nth(i).locator('.kpi-value');
		// Drawn (the row keeps its shape) but hidden from assistive tech.
		await expect(value).toHaveText(NO_FIGURE);
		await expect(value).toHaveAttribute('aria-hidden', 'true');
		// …and the loading text stands in its place.
		await expect(cards.nth(i).locator('.kpi-loading')).toHaveText(LOADING);
	}

	// The tint is a verdict. There is no figure to have a verdict about, so a
	// pending row is uniformly neutral — this is the assertion that would have
	// caught `/bank-reconciliation` reading "nothing unmatched, no
	// discrepancies" on a fraud control while it was still asking.
	await expect(row.locator('.kpi.highlight-green, .kpi.highlight-red')).toHaveCount(0);

	// The accessibility tree itself — the thing axe cannot judge. The dash is
	// out of it entirely; "Loading…" is in it once per card.
	//
	// Asserted on the VALUE nodes rather than by searching the row's whole aria
	// snapshot for the dash character. Two of `/audit`'s five cards carry a
	// `sub` line with a legitimate em dash ("Digest no longer re-derives —
	// investigate"), so a substring search flags prose that has nothing to do
	// with a placeholder. What the row is actually promising is narrower and
	// exactly checkable: while pending, no card's value node is in the
	// accessibility tree at all.
	await expect(
		row.locator('.kpi-value:not([aria-hidden="true"])'),
		'a pending card must not announce its placeholder dash'
	).toHaveCount(0);
	const announced = await row.ariaSnapshot();
	expect(announced.match(new RegExp(LOADING, 'g')) ?? []).toHaveLength(count);
}

/**
 * No card in `row` is claiming to be busy any more.
 *
 * Deliberately does NOT require every card to hold a figure. A settled row can
 * still carry an `unavailable` card — `/discounts`' capture rate is exactly
 * that on a tenant where no offer has been captured or missed, since a ratio
 * over an empty decided population is not `0%`. What must be true of every
 * card once the response has landed is that it has stopped announcing a load,
 * which is what this asserts; the stubbed-figure tests below pin the
 * `value` state where the data makes it determinate.
 */
async function expectRowSettled(row: Locator, count: number): Promise<void> {
	await expect(row.locator('.kpi')).toHaveCount(count);
	await expect(row.locator('.kpi[data-kpi-state="pending"]')).toHaveCount(0);
	// `aria-busy` is ABSENT once settled, not `"false"` — a card that keeps the
	// attribute keeps telling assistive tech the page is still working.
	await expect(row.locator('.kpi[aria-busy]')).toHaveCount(0);
	await expect(row.locator('.kpi-value[aria-hidden]')).toHaveCount(0);
	await expect(row.locator('.kpi-loading')).toHaveCount(0);

	const announced = await row.ariaSnapshot();
	expect(announced, 'a settled card must not still announce a loading state').not.toContain(
		LOADING
	);
}

/** Every card in `row` holds a computed figure — no dash, pending or not. */
async function expectEveryCardIsAFigure(row: Locator, count: number): Promise<void> {
	await expect(row.locator('.kpi[data-kpi-state="value"]')).toHaveCount(count);
	await expect(row.locator('.kpi-value', { hasText: NO_FIGURE })).toHaveCount(0);
}

test.describe('accessibility — KPI pending affordance (WCAG 4.1.2 / 1.3.1)', () => {
	/**
	 * Retire the route handlers before Playwright tears the test down.
	 *
	 * `holdEndpoint` parks a handler inside `await released` and then inside
	 * `route.fetch()`. A page still settling can leave one there when the last
	 * assertion resolves, and the teardown surfaces it as `route.fetch: Test
	 * ended.` against whichever test owned it — the failure mode
	 * `a11y/target-size.spec.ts` and `a11y/deemphasised-rows.spec.ts` both
	 * document. Every test also releases its own hold in a `finally`, so the
	 * handler is never still blocked when this runs.
	 */
	test.afterEach(async ({ page }) => {
		await page.unrouteAll({ behavior: 'ignoreErrors' });
	});

	// --- /discounts -------------------------------------------------------

	test('/discounts KPI row: pending announces a load, settled announces figures', async ({
		page
	}) => {
		const held = holdEndpoint(page, '/api/discounts/dashboard');
		await held.install();

		try {
			await page.goto('/discounts');
			const row = page.locator('.kpi-row').first();
			await expect(row.locator('.kpi').first()).toBeVisible();

			// --- pending ---
			await expectRowPending(row, 5);
			await expectNoA11yViolations(page);

			// --- settled (the page's real dashboard response) ---
			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			await expectRowSettled(row, 5);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test('/discounts: a computed zero is announced as a zero, never as pending', async ({ page }) => {
		// Every figure genuinely zero — the case the nullish (not falsy) rule in
		// `kpiFigureState` exists for. A tenant with nothing captured and nothing
		// open has five real answers, and each must read as one.
		await stubEndpoint(page, '/api/discounts/dashboard', {
			captured_count: 0,
			captured_amount: 0,
			missed_count: 0,
			missed_amount: 0,
			capture_rate_pct: 0,
			insufficient_data: false,
			open_offer_count: 0,
			projected_savings: 0,
			currency: 'USD',
			unconvertible_offer_count: 0,
			excluded_captured_count: 0,
			excluded_missed_count: 0
		});

		await page.goto('/discounts');
		const row = page.locator('.kpi-row').first();
		await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');

		// Not pending, not unavailable — a figure.
		await expectRowSettled(row, 5);
		await expectEveryCardIsAFigure(row, 5);

		// The count card is currency-independent, so it can be pinned exactly.
		const openOffers = row.locator('.kpi', { hasText: 'Open offers' });
		await expect(openOffers.locator('.kpi-value')).toHaveText('0');
		await expect(row.locator('.kpi', { hasText: 'Capture rate' }).locator('.kpi-value')).toHaveText(
			'0%'
		);

		// A zero is a figure, so it KEEPS its verdict tint — the difference from
		// a pending card, which drops it. Without this the two states could be
		// collapsed back together by making the tint conditional on truthiness.
		await expect(row.locator('.kpi.highlight-green, .kpi.highlight-red')).toHaveCount(3);

		// And the zero reaches the accessibility tree as a zero.
		const announced = await row.ariaSnapshot();
		expect(announced).toContain('0 Open offers');

		await expectNoA11yViolations(page);
	});

	test('/discounts: an unavailable figure announces its dash, and is not busy', async ({
		page
	}) => {
		// The third state, and the reason `aria-hidden` on the dash has to be
		// CONDITIONAL: "still arriving" and "did not arrive" draw the same glyph,
		// so hiding it unconditionally would leave a failed load announcing an
		// empty card with no explanation at all.
		await stubEndpoint(page, '/api/discounts/dashboard', { detail: 'boom' }, 500);

		await page.goto('/discounts');
		const row = page.locator('.kpi-row').first();
		await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'unavailable');

		await expect(row.locator('.kpi[data-kpi-state="unavailable"]')).toHaveCount(5);
		await expect(row.locator('.kpi[aria-busy]')).toHaveCount(0);
		await expect(row.locator('.kpi-value[aria-hidden]')).toHaveCount(0);
		await expect(row.locator('.kpi-loading')).toHaveCount(0);
		// No verdict on a figure that never arrived, either.
		await expect(row.locator('.kpi.highlight-green, .kpi.highlight-red')).toHaveCount(0);

		const announced = await row.ariaSnapshot();
		expect(announced, 'an unavailable figure must still announce its dash').toContain(NO_FIGURE);
		expect(announced).not.toContain(LOADING);

		await expectNoA11yViolations(page);
	});

	// --- /bank-reconciliation --------------------------------------------

	test('/bank-reconciliation KPI row: pending announces a load, settled announces figures', async ({
		page
	}) => {
		const held = holdEndpoint(page, '/api/bank-reconciliation/outstanding');
		await held.install();

		try {
			await page.goto('/bank-reconciliation');
			const row = page.locator('.kpi-row').first();
			await expect(row.locator('.kpi').first()).toBeVisible();

			await expectRowPending(row, 3);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			await expectRowSettled(row, 3);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test('/bank-reconciliation: a zero count is a figure, and the tint returns with it', async ({
		page
	}) => {
		// Nothing uncleared, three debits the bank shows that we cannot account
		// for, no amount discrepancies. Two genuine zeros beside one genuine
		// alarm, so this pins BOTH halves of the tint rule in one render: the
		// verdict is withheld only for a missing figure, never for a zero one.
		await stubEndpoint(page, '/api/bank-reconciliation/outstanding', {
			as_of: '2026-01-15T09:00:00Z',
			older_than_days: 0,
			uncleared_payments: [],
			uncleared_count: 0,
			uncleared_totals: [],
			unmatched_debits: [],
			unmatched_debit_count: 3,
			unmatched_debit_totals: [],
			discrepancies: [],
			discrepancy_count: 0,
			amount_mismatch_net_variances: []
		});

		await page.goto('/bank-reconciliation');
		const row = page.locator('.kpi-row').first();
		await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');

		await expectRowSettled(row, 3);
		await expectEveryCardIsAFigure(row, 3);

		const uncleared = row.locator('.kpi', { hasText: 'Uncleared payments' });
		await expect(uncleared.locator('.kpi-value')).toHaveText('0');
		const discrepancies = row.locator('.kpi', { hasText: 'Discrepancies' });
		await expect(discrepancies.locator('.kpi-value')).toHaveText('0');

		// The one card with something to be alarmed about, and only that one.
		const unmatched = row.locator('.kpi', { hasText: 'Unmatched bank debits' });
		await expect(unmatched.locator('.kpi-value')).toHaveText('3');
		await expect(row.locator('.kpi.highlight-red')).toHaveCount(1);

		const announced = await row.ariaSnapshot();
		expect(announced).toContain('0 Uncleared payments');
		expect(announced).toContain('3 Unmatched bank debits');

		await expectNoA11yViolations(page);
	});

	// --- /cfo -------------------------------------------------------------

	test('/cfo KPI row: pending announces a load, settled announces figures', async ({ page }) => {
		// `/cfo` used to apply the rule by a DIFFERENT mechanism — it rendered no
		// KPI row at all until the response landed. That satisfied §34 (no figure
		// nobody computed was displayed) but meant the one convention had two
		// shapes, so a reader could not learn it once. It now takes the same
		// `pending` treatment as the two rows above, and this asserts the
		// mechanism as well as the invariant.
		const forecast = holdEndpoint(page, '/api/analytics/cashflow_forecast');
		const whatif = holdEndpoint(page, '/api/analytics/cashflow_whatif');
		const position = holdEndpoint(page, '/api/analytics/cash_position');
		await forecast.install();
		await whatif.install();
		await position.install();

		try {
			await page.goto('/cfo');
			await expect(page.getByRole('heading', { name: 'Cash Flow', exact: true })).toBeVisible();
			// The page's own in-flight state, not a guess at timing.
			await expect(page.locator('.workspace > .loading')).toBeVisible();

			const row = page.getByTestId('forecast-kpi-row');
			await expect(row).toBeVisible();
			await expectRowPending(row, 4);
			await expectNoA11yViolations(page);

			// --- settled (the page's real analytics responses) ---
			forecast.release();
			whatif.release();
			position.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			await expectRowSettled(row, 4);
			await expectNoA11yViolations(page);
		} finally {
			forecast.release();
			whatif.release();
			position.release();
		}
	});

	test('/cfo: a zeroed forecast is announced as a zero and keeps its verdict tint', async ({
		page
	}) => {
		// Rewrite the REAL payload rather than inventing one — the forecast shape
		// is wide and the assertion is about the KPI cards, not the fixture. Same
		// approach `deemphasised-rows.spec.ts` uses for the credit-memo row.
		await page.route('**/api/analytics/cashflow_forecast*', async (route: Route) => {
			const response = await route.fetch();
			const body = (await response.json()) as {
				totals?: Record<string, unknown>;
			};
			if (body.totals) {
				body.totals.scheduled_amount = '0';
				body.totals.committed_amount = '0';
				body.totals.pending_amount = '0';
			}
			await route.fulfill({ response, json: body });
		});
		await page.route('**/api/analytics/cashflow_whatif*', async (route: Route) => {
			const response = await route.fetch();
			const body = (await response.json()) as {
				scenarios?: { early?: Record<string, unknown> };
			};
			if (body.scenarios?.early) body.scenarios.early.total_discount_captured = '0';
			await route.fulfill({ response, json: body });
		});

		await page.goto('/cfo');
		const row = page.getByTestId('forecast-kpi-row');
		// The row is now rendered from first paint (pending), so its mere
		// presence is no longer a readiness signal — wait on the card state the
		// landed response produces, exactly as the two rows above do.
		await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
			timeout: 15_000
		});

		await expectRowSettled(row, 4);
		await expectEveryCardIsAFigure(row, 4);

		// Every card carries a real zero — money, so the currency symbol is the
		// tenant's and only the digit can be pinned.
		const values = row.locator('.kpi-value');
		for (let i = 0; i < 4; i++) {
			await expect(values.nth(i)).toHaveText(/0/);
		}

		// "$0 available if you pay early" is a computed answer, so it keeps the
		// green verdict a pending card would have dropped.
		await expect(row.locator('.kpi.highlight-green')).toHaveCount(1);

		await expectNoA11yViolations(page);
	});

	// --- / (the dashboard) ------------------------------------------------

	test('the dashboard KPI row: pending announces a load, settled announces figures', async ({
		page
	}) => {
		// The app's highest-traffic KPI row, and the last page-level one still
		// gated on its response: it sat inside `{:else if data}` and collapsed to
		// nothing on every load of every tenant (`docs/decisions.md` §154).
		const held = holdEndpoint(page, '/api/dashboard');
		await held.install();

		try {
			await page.goto('/');
			const row = page.locator('.kpi-row');
			await expect(row.locator('.kpi').first()).toBeVisible();

			// FIVE, not however many the tenant's data produces: the row's spine
			// is the five figures every tenant has. Exceptions, stale approvals,
			// rebates and captured discounts appear only when there is something
			// to report, so they stay gated on the response — a dash promising a
			// figure for a card that may never render is the same lie pointed the
			// other way.
			await expectRowPending(row, 5);
			await expectNoA11yViolations(page);

			// --- settled (the page's real dashboard response) ---
			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			// The settled count is read rather than written, as on `/billing`: the
			// four conditional cards depend on seed state this test does not own.
			// What it asserts is that nothing is still announcing a load.
			await expectRowSettled(row, await row.locator('.kpi').count());
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test('the dashboard pending row gives way to the zero-invoice empty state', async ({ page }) => {
		// The design collision §154 resolves, asserted as the transition it chose.
		// While the answer is out, whether the tenant has any invoices is unknown,
		// so the row renders pending; once the answer says "none", the onboarding
		// EmptyState REPLACES it. Gating the row on `total_invoices > 0` instead
		// would spare this one tenant a hand-off by collapsing the row for every
		// other tenant on every load.
		const held = holdEndpoint(page, '/api/dashboard', emptyTenantDashboardResponse());
		await held.install();

		try {
			await page.goto('/');
			const row = page.locator('.kpi-row');
			await expect(row.locator('.kpi').first()).toBeVisible();
			await expectRowPending(row, 5);

			// The empty state is not on screen YET — so the assertion below is
			// about the hand-off, not about a page that simply never had a row.
			await expect(page.getByTestId('dashboard-empty-state')).toHaveCount(0);

			held.release();
			await expect(page.getByTestId('dashboard-empty-state')).toBeVisible();
			// And the row is GONE, not left sitting above the empty state with
			// five zeros — the two are alternatives, not a stack.
			await expect(page.locator('.kpi-row')).toHaveCount(0);
			await expect(page.locator('.kpi')).toHaveCount(0);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	// --- panel-scoped rows ------------------------------------------------
	//
	// The rows above are PAGE-level rows: one row, one headline fetch, the
	// page's own loading flag. The rows below sit inside a PANEL's own
	// error/loading/data chain — a different loading chain, which is exactly why
	// they were missed when the convention landed and why they need their own
	// cases rather than being assumed covered by a route scan.
	//
	// Three shapes, and each is represented once rather than exhaustively:
	//
	//   (a) an on-mount panel fetch                → /admin/health, /adaptive,
	//                                                 /admin/access-review, /cfo's
	//                                                 CfoMetrics
	//   (b) a row hoisted OUT of a branch chain    → /billing
	//   (c) a row the user TRIGGERS                → /audit
	//
	// `/expenses`' Reports summary, `ForecastVariancePanel`,
	// `AgentDashboard` and `BudgetModal`'s spend rollup are (a) and (c) again
	// with no new mechanism — each needs a report opened, a form submitted or a
	// modal driven first, so a case for each would add setup rather than
	// signal. If one of those grows a mechanism of its own, add it here.

	test('/admin/health panel row: pending announces a load, and withholds its verdict', async ({
		page
	}) => {
		const held = holdEndpoint(page, '/api/health/sweeps');
		await held.install();

		try {
			await page.goto('/admin/health');
			const row = page.getByTestId('sweep-health-summary');
			await expect(row.locator('.kpi').first()).toBeVisible();

			// This is the assertion the panel rows exist for. "Overall" is tinted
			// green when the sweeps are healthy and red otherwise — an
			// unconditional verdict, so before the fix the row's only protection
			// from painting "everything is fine" over an unanswered question was
			// not existing yet.
			await expectRowPending(row, 4);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			await expectRowSettled(row, 4);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test('/adaptive threshold panel row: pending announces a load, settled announces figures', async ({
		page
	}) => {
		const held = holdEndpoint(page, '/api/adaptive/threshold-recommendation');
		await held.install();

		try {
			await page.goto('/adaptive');
			// The threshold row lives behind its own tab — `/adaptive` opens on
			// Suggestions, so without this the row is never in the DOM and the
			// assertion below fails as "element(s) not found" rather than as a
			// collapsed row, which is the opposite of what this case is for.
			await page.getByRole('tab', { name: 'Auto-approve threshold' }).click();
			const row = page.getByTestId('adaptive-threshold-card');
			await expect(row.locator('.kpi').first()).toBeVisible();

			await expectRowPending(row, 4);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
				timeout: 15_000
			});
			await expectRowSettled(row, 4);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test('/admin/access-review panel row: pending announces a load, settled announces figures', async ({
		page
	}) => {
		const held = holdEndpoint(page, '/api/access-reviews');
		await held.install();

		try {
			await page.goto('/admin/access-review');
			const row = page.locator('.kpi-row').first();
			await expect(row.locator('.kpi').first()).toBeVisible();

			// Dormant carries a red verdict on a SOX access control: "0 dormant
			// privileged users" is the reassuring answer, and it must not be drawn
			// before anyone has counted.
			await expectRowPending(row, 4);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value');
			await expectRowSettled(row, 4);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	test("/cfo's CFO-metrics panel row: pending announces a load, settled announces figures", async ({
		page
	}) => {
		// A SECOND panel on a route this spec already covers at page level, and
		// the two are independent: the forecast row settles off
		// `/api/analytics/cashflow_*` while this one waits on `/api/analytics/cfo`.
		// Holding only the latter is what proves the panel row has its own
		// loading chain rather than riding the page's.
		const held = holdEndpoint(page, '/api/analytics/cfo');
		await held.install();

		try {
			await page.goto('/cfo');
			const section = page.getByTestId('cfo-metrics-section');
			const row = section.locator('.kpi-row');
			await expect(row.locator('.kpi').first()).toBeVisible({ timeout: 15_000 });

			await expectRowPending(row, 4);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
				timeout: 15_000
			});
			await expectRowSettled(row, 4);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	// --- /billing (shape b: hoisted out of a branch chain) ----------------

	test('/billing usage row survives the branch chain it used to live inside', async ({ page }) => {
		// The row used to be TWO copies, one per branch of a chain gated on the
		// subscription response — so it could not simply take `pending`: which
		// copy owns the row is decided by `hasSubscription`, and while the
		// response is out there is no answer. This asserts the single hoisted
		// row is on screen and busy while that question is unanswered, which is
		// the whole of the structural fix.
		const held = holdEndpoint(page, '/api/billing/subscription');
		await held.install();

		try {
			await page.goto('/billing');
			const row = page.locator('.kpi-row').first();
			await expect(row.locator('.kpi').first()).toBeVisible();

			// Two cards, not the empty-state's rebate-augmented row: `rebateGroups`
			// is empty while the response is out, so the count is the shared pair.
			await expectRowPending(row, 2);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
				timeout: 15_000
			});
			// The SETTLED count is not a literal, unlike every other case in this
			// spec: an org with no subscription gets one extra card per rebate
			// currency, and whether the worker's tenant has accrued any is seed
			// state this test does not own (`billing.spec.ts` avoids counting here
			// for the same reason). Reading it after the row has provably settled
			// is deterministic, and the count was never what this asserts — the
			// absence of `aria-busy`, the hidden dash and the loading text is.
			await expectRowSettled(row, await row.locator('.kpi').count());
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});

	// --- /audit (shape c: the user triggers the fetch) --------------------

	test('/audit verification row appears on the CLICK, not on the answer', async ({ page }) => {
		// The one panel row that stays gated, deliberately. The read is audited
		// (`audit.viewed`), so nothing is fetched on mount and five dashes before
		// anyone pressed Run would claim a figure is coming for a question nobody
		// asked. `report || verifyLoading` is the gate: absent, then pending on
		// the click, then settled.
		const held = holdEndpoint(page, '/api/audit/verify-signatures');
		await held.install();

		try {
			await page.goto('/audit');
			await expect(
				page.getByRole('heading', { name: 'Approval-signature verification' })
			).toBeVisible();

			// Nothing asked yet — and this absence assertion is non-vacuous
			// because the heading above already proved the page rendered.
			await expect(page.getByTestId('verify-counts')).toHaveCount(0);

			await page.getByTestId('run-verification').click();

			const row = page.getByTestId('verify-counts');
			await expect(row.locator('.kpi').first()).toBeVisible();
			await expectRowPending(row, 5);
			await expectNoA11yViolations(page);

			held.release();
			await expect(row.locator('.kpi').first()).toHaveAttribute('data-kpi-state', 'value', {
				timeout: 15_000
			});
			await expectRowSettled(row, 5);
			await expectNoA11yViolations(page);
		} finally {
			held.release();
		}
	});
});
