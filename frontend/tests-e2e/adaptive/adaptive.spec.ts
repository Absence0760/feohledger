import { expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';
import type { Page, Request, Route } from '@playwright/test';

/**
 * `/adaptive` — the UI for the nine `/api/adaptive` endpoints, which had none.
 *
 * Every backend response is stubbed so the assertions are about the PAGE, not
 * about whatever approval history the seeded tenant happens to hold. Two of the
 * states covered here can't be produced on demand against a live tenant at all:
 * the threshold apply's stale-value 409 (it needs the recommendation to move
 * between a read and a write) and the feedback loop's insufficient-data state
 * (it needs FEWER than five auto-approved invoices).
 *
 * Route globs are dispatched on the exact PATHNAME. `**\/api/adaptive*` also
 * matches Vite's dev-server module URL for `/src/lib/api/adaptive.ts`, and
 * fulfilling that with JSON blanks the whole page — the same guard
 * `tests-e2e/experiments/load-states.spec.ts` documents.
 */

const PATH = {
	suggestions: '/api/adaptive/suggestions',
	threshold: '/api/adaptive/threshold-recommendation',
	thresholdApply: '/api/adaptive/threshold-recommendation/apply',
	feedback: '/api/adaptive/feedback'
} as const;

function pathOf(request: Request): string {
	return new URL(request.url()).pathname;
}

function json(route: Route, body: unknown, status = 200) {
	return route.fulfill({
		status,
		contentType: 'application/json',
		body: JSON.stringify(body)
	});
}

const EMPTY_SUGGESTIONS = { suggestions: [] };

function suggestion(overrides: Record<string, unknown> = {}) {
	return {
		id: '11111111-1111-1111-1111-111111111111',
		kind: 'auto_approve_threshold',
		vendor_id: '22222222-2222-2222-2222-222222222222',
		vendor_name: 'Northwind Supplies',
		title: 'Auto-approve Northwind Supplies below 1,200.00',
		rationale: '18 of 18 invoices approved unedited over the last year.',
		payload: {},
		confidence_pct: '96.0',
		status: 'open',
		created_at: '2026-08-01T10:00:00+00:00',
		dismissed_at: null,
		// The server's verdict on Dismiss (`dismiss_refusal`); the page offers
		// the control by it and never restates the status.
		actions: { dismiss: null },
		...overrides
	};
}

function recommendation(overrides: Record<string, unknown> = {}) {
	return {
		should_raise: true,
		current_threshold: '1000.00',
		recommended_threshold: '2500.00',
		cap_threshold: '5000.00',
		qualifying_vendor_count: 3,
		total_clean_invoices: 42,
		reason_code: 'ok',
		rationale: '3 vendors with clean approval history support a higher threshold.',
		evidence: [
			{
				vendor_id: '22222222-2222-2222-2222-222222222222',
				vendor_name: 'Northwind Supplies',
				based_on_n: 18,
				max_approved_amount: '2450.00',
				median_approved_amount: '900.00'
			}
		],
		workflow_id: '33333333-3333-3333-3333-333333333333',
		lookback_days: 365,
		...overrides
	};
}

/**
 * Stub the two calls the page makes on mount, so no test is at the mercy of the
 * tenant's real approval history. Extra per-test handlers registered AFTER this
 * take precedence (Playwright matches most-recently-added first).
 */
async function stubPageLoad(page: Page) {
	await page.route('**/api/adaptive/**', async (route) => {
		const p = pathOf(route.request());
		if (p === PATH.suggestions) return json(route, EMPTY_SUGGESTIONS);
		if (p === PATH.threshold) return json(route, recommendation({ should_raise: false }));
		return route.continue();
	});
}

test.describe('/adaptive — role gate', () => {
	test('an admin gets the page', async ({ page }) => {
		await stubPageLoad(page);
		await page.goto('/adaptive');
		await expect(page.getByRole('heading', { name: 'Adaptive Workflows', level: 1 })).toBeVisible({
			timeout: 15_000
		});
		// The standing advisory line is part of the contract, not decoration: the
		// whole surface recommends, it never reports a change that happened.
		await expect(page.getByTestId('adaptive-advisory')).toBeVisible();
	});

	test('the page is reachable from the Settings section bar', async ({ page }) => {
		await stubPageLoad(page);
		await page.goto('/experiments');
		const link = page.getByRole('link', { name: 'Adaptive Workflows' });
		await expect(link).toBeVisible({ timeout: 15_000 });
		await link.click();
		await expect(page).toHaveURL(/\/adaptive$/);
	});

	test.describe('a clerk is refused', () => {
		test.use({ storageState: { cookies: [], origins: [] } });

		test('an ap_clerk is bounced off the page', async ({ page, tenantClerk }) => {
			await stubPageLoad(page);
			await signInAndWait(page, tenantClerk);
			await page.goto('/adaptive');
			// The backend 403s an ap_clerk on every /api/adaptive route, so the page
			// redirects rather than rendering panels that can only fail.
			await expect(page).toHaveURL(TENANT_ROOT_URL, { timeout: 15_000 });
			await expect(
				page.getByRole('heading', { name: 'Adaptive Workflows', level: 1 })
			).toHaveCount(0);
		});
	});
});

test.describe('/adaptive — accessibility (WCAG 2.2 AA)', () => {
	// The page ships a tablist, six panels, four tables and two money KPI rows.
	// Scanning it here rather than adding a row to `a11y/axe.spec.ts` keeps the
	// new surface guarded without editing a spec several other surfaces share.
	test('the default panel has no axe violations', async ({ page }) => {
		await stubPageLoad(page);
		await page.goto('/adaptive');
		await expect(page.locator('aside.sidebar').first()).toBeVisible({ timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Adaptive Workflows', exact: true })).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('the feedback panel has no axe violations', async ({ page }) => {
		await stubPageLoad(page);
		await page.route('**/api/adaptive/feedback**', async (route) => {
			if (pathOf(route.request()) !== PATH.feedback) return route.continue();
			return json(route, {
				lookback_days: 365,
				entity_id: null,
				outcomes: {
					auto_approved_count: 2,
					voided_count: 0,
					corrected_count: 0,
					rejected_count: 0,
					overturned_count: 0,
					overturn_rate_pct: '0.0',
					insufficient_data: true
				},
				metrics: [
					{
						name: 'auto_approval_overturn_rate',
						value_pct: null,
						sample_size: 2,
						insufficient_data: true,
						label: 'Not yet measurable — only 2 auto-approved invoice(s) so far (need 5).'
					}
				],
				base_recommendation: recommendation(),
				adjusted_recommendation: recommendation({ should_raise: false })
			});
		});
		await page.goto('/adaptive');
		await page.getByRole('tab', { name: 'Feedback loop' }).click();
		await expect(page.getByTestId('adaptive-metric-insufficient')).toBeVisible({
			timeout: 15_000
		});
		await expectNoA11yViolations(page);
	});
});

test.describe('/adaptive — advisory suggestions', () => {
	test('a suggestion can be dismissed', async ({ page }) => {
		await stubPageLoad(page);

		let dismissed = false;
		let dismissPosts = 0;
		await page.route('**/api/adaptive/suggestions**', async (route) => {
			const req = route.request();
			const p = pathOf(req);
			if (p === PATH.suggestions && req.method() === 'GET') {
				return json(route, dismissed ? EMPTY_SUGGESTIONS : { suggestions: [suggestion()] });
			}
			if (p === `${PATH.suggestions}/${suggestion().id}/dismiss` && req.method() === 'POST') {
				dismissPosts += 1;
				dismissed = true;
				return json(route, {
					suggestions: [
						suggestion({
							status: 'dismissed',
							actions: {
								dismiss: {
									code: 'workflow_suggestion_not_dismissable',
									message: 'A suggestion that is dismissed cannot be dismissed.',
									params: { status: 'dismissed' }
								}
							}
						})
					]
				});
			}
			return route.continue();
		});

		await page.goto('/adaptive');

		const row = page.getByTestId('adaptive-suggestion-row');
		await expect(row).toHaveCount(1, { timeout: 15_000 });
		await expect(row).toContainText('Northwind Supplies');

		// Armed two-click, the same shape every destructive row action uses.
		const dismiss = row.getByRole('button', { name: 'Dismiss' });
		await dismiss.click();
		await row.getByRole('button', { name: 'Confirm' }).click();

		await expect(page.getByTestId('table-empty')).toBeVisible({ timeout: 10_000 });
		expect(dismissPosts).toBe(1);
	});

	test('Dismiss follows the server verdict, not the status', async ({ page }) => {
		// An `open` row the reader may not dismiss shows no Dismiss; a `stale`
		// row the endpoint accepts a dismiss for shows one. A restated
		// `status === 'open'` check gets both wrong.
		await stubPageLoad(page);
		await page.route('**/api/adaptive/suggestions**', async (route) => {
			const req = route.request();
			if (pathOf(req) === PATH.suggestions && req.method() === 'GET') {
				return json(route, {
					suggestions: [
						suggestion({
							title: 'Refused open suggestion',
							actions: {
								dismiss: {
									code: 'role_not_permitted',
									message: 'Your role does not permit this action.',
									params: {}
								}
							}
						}),
						suggestion({
							id: '33333333-3333-3333-3333-333333333333',
							title: 'Stale suggestion',
							status: 'stale'
						})
					]
				});
			}
			return route.continue();
		});

		await page.goto('/adaptive');
		const rows = page.getByTestId('adaptive-suggestion-row');
		await expect(rows).toHaveCount(2, { timeout: 15_000 });
		await expect(
			rows.filter({ hasText: 'Refused open suggestion' }).getByRole('button', { name: 'Dismiss' })
		).toHaveCount(0);
		await expect(
			rows.filter({ hasText: 'Stale suggestion' }).getByRole('button', { name: 'Dismiss' })
		).toHaveCount(1);
	});
});

test.describe('/adaptive — auto-approve threshold', () => {
	test('applying the recommendation sends the rendered figure and reports the result', async ({
		page
	}) => {
		await stubPageLoad(page);

		let applied = false;
		let sentExpected: string | null = null;
		await page.route('**/api/adaptive/threshold-recommendation**', async (route) => {
			const req = route.request();
			const p = pathOf(req);
			if (p === PATH.thresholdApply && req.method() === 'POST') {
				sentExpected =
					(req.postDataJSON() as { expected_recommended_threshold?: string })
						.expected_recommended_threshold ?? null;
				applied = true;
				return json(route, {
					applied: true,
					workflow_id: '33333333-3333-3333-3333-333333333333',
					previous_threshold: '1000.00',
					new_threshold: '2500.00',
					reason_code: 'ok',
					rationale: 'Raised.',
					version_number: 4
				});
			}
			if (p === PATH.threshold && req.method() === 'GET') {
				return json(
					route,
					applied
						? recommendation({
								should_raise: false,
								current_threshold: '2500.00',
								recommended_threshold: '2500.00',
								reason_code: 'no_increase',
								rationale: 'The threshold already matches the evidence.'
							})
						: recommendation()
				);
			}
			return route.continue();
		});

		await page.goto('/adaptive');
		await page.getByRole('tab', { name: 'Auto-approve threshold' }).click();

		const apply = page.getByTestId('adaptive-threshold-apply');
		await expect(apply).toBeVisible({ timeout: 15_000 });
		await apply.click();

		// After a successful apply the recommendation is re-read and there is
		// nothing left to raise — the button is replaced by the no-raise state.
		await expect(page.getByTestId('adaptive-threshold-no-raise')).toBeVisible({
			timeout: 10_000
		});
		// The stale-value guard is only a guard if the UI actually sends the
		// number it rendered. That is the whole reason the backend accepts it.
		expect(sentExpected).toBe('2500.00');
		await expect(page.getByTestId('adaptive-threshold-stale')).toHaveCount(0);
	});

	test('a STALE recommendation surfaces the recommendation-changed state, not an error', async ({
		page
	}) => {
		await stubPageLoad(page);

		// The recommendation moves between the read the page rendered and the
		// write it attempted — exactly what `expected_recommended_threshold`
		// exists to catch.
		let moved = false;
		await page.route('**/api/adaptive/threshold-recommendation**', async (route) => {
			const req = route.request();
			const p = pathOf(req);
			if (p === PATH.thresholdApply && req.method() === 'POST') {
				moved = true;
				return json(
					route,
					{
						detail:
							'Recommendation changed since it was read (now 3200.00); re-read before applying'
					},
					409
				);
			}
			if (p === PATH.threshold && req.method() === 'GET') {
				return json(
					route,
					moved ? recommendation({ recommended_threshold: '3200.00' }) : recommendation()
				);
			}
			return route.continue();
		});

		await page.goto('/adaptive');
		await page.getByRole('tab', { name: 'Auto-approve threshold' }).click();

		const apply = page.getByTestId('adaptive-threshold-apply');
		await expect(apply).toBeVisible({ timeout: 15_000 });
		await expect(apply).toContainText(/2[.,]500/);
		await apply.click();

		const stale = page.getByTestId('adaptive-threshold-stale');
		await expect(stale).toBeVisible({ timeout: 10_000 });
		await expect(stale).toContainText('The recommendation changed');
		// It names BOTH figures: what was on screen, and what it became. A bare
		// "conflict" would leave the reader with nothing to decide on.
		await expect(stale).toContainText(/2[.,]500/);
		await expect(stale).toContainText(/3[.,]200/);
		// It is a changed state, not a load failure — the panel is still usable
		// and the refreshed recommendation is the one now offered.
		await expect(page.getByTestId('adaptive-threshold-error')).toHaveCount(0);
		await expect(page.getByTestId('adaptive-threshold-apply')).toContainText(/3[.,]200/);
	});
});

test.describe('/adaptive — feedback loop', () => {
	function feedbackPayload(overrides: { insufficient: boolean }) {
		const insufficient = overrides.insufficient;
		return {
			lookback_days: 365,
			entity_id: null,
			outcomes: {
				auto_approved_count: insufficient ? 2 : 40,
				voided_count: 0,
				corrected_count: insufficient ? 0 : 3,
				rejected_count: 0,
				overturned_count: insufficient ? 0 : 3,
				overturn_rate_pct: insufficient ? '0.0' : '7.5',
				insufficient_data: insufficient
			},
			metrics: [
				{
					name: 'auto_approval_overturn_rate',
					value_pct: insufficient ? null : '7.5',
					sample_size: insufficient ? 2 : 40,
					insufficient_data: insufficient,
					label: insufficient
						? 'Not yet measurable — only 2 auto-approved invoice(s) so far (need 5).'
						: '3 of 40 auto-approved invoices were later voided, corrected, or rejected (7.5%).'
				},
				{
					name: 'recommendation_acceptance_rate',
					value_pct: null,
					sample_size: 0,
					insufficient_data: true,
					label:
						'Not yet measurable — no workflow suggestions have been surfaced yet, so there is nothing to have accepted.'
				}
			],
			base_recommendation: recommendation(),
			adjusted_recommendation: recommendation()
		};
	}

	test('the audited read does not happen until the reader asks for it', async ({ page }) => {
		await stubPageLoad(page);

		let feedbackGets = 0;
		await page.route('**/api/adaptive/feedback**', async (route) => {
			if (pathOf(route.request()) !== PATH.feedback) return route.continue();
			feedbackGets += 1;
			return json(route, feedbackPayload({ insufficient: true }));
		});

		await page.goto('/adaptive');
		await expect(page.getByRole('heading', { name: 'Adaptive Workflows', level: 1 })).toBeVisible({
			timeout: 15_000
		});
		// GET /feedback writes an `adaptive_feedback.viewed` access-audit row, so
		// landing on the page must not fetch it.
		expect(feedbackGets).toBe(0);

		await page.getByRole('tab', { name: 'Feedback loop' }).click();
		await expect
			.poll(() => feedbackGets, { timeout: 10_000 })
			.toBe(1);
	});

	test('the insufficient-data state renders as itself, never as a computed rate', async ({
		page
	}) => {
		await stubPageLoad(page);
		await page.route('**/api/adaptive/feedback**', async (route) => {
			if (pathOf(route.request()) !== PATH.feedback) return route.continue();
			return json(route, feedbackPayload({ insufficient: true }));
		});

		await page.goto('/adaptive');
		await page.getByRole('tab', { name: 'Feedback loop' }).click();

		const overturn = page.getByTestId('adaptive-metric-auto_approval_overturn_rate');
		await expect(overturn).toBeVisible({ timeout: 15_000 });
		await expect(overturn).toContainText('Not yet measurable');
		// The honest state is the ABSENCE of a figure: a 0% here would read as
		// "the automation is never overruled", which two invoices cannot support.
		await expect(overturn).not.toContainText('%');
		await expect(page.getByTestId('adaptive-metric-insufficient')).toHaveCount(2);
	});

	test('a measurable metric renders its rate', async ({ page }) => {
		await stubPageLoad(page);
		await page.route('**/api/adaptive/feedback**', async (route) => {
			if (pathOf(route.request()) !== PATH.feedback) return route.continue();
			return json(route, feedbackPayload({ insufficient: false }));
		});

		await page.goto('/adaptive');
		await page.getByRole('tab', { name: 'Feedback loop' }).click();

		const overturn = page.getByTestId('adaptive-metric-auto_approval_overturn_rate');
		await expect(overturn).toBeVisible({ timeout: 15_000 });
		await expect(overturn).toContainText('7.5%');
		// The second metric has no sample at all and stays honest beside it.
		await expect(page.getByTestId('adaptive-metric-insufficient')).toHaveCount(1);
	});
});
