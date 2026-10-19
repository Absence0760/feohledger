import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /payments Queue tab — the multi-route corridor quote comparison.
 *
 * `POST /api/payments/corridor-quotes` prices ONE payable invoice across every
 * processor the org has configured and ranks them
 * (`services/corridor_quotes.compare_quotes`). It had **no caller anywhere in
 * `frontend/src`**, so the optimizer was a module nothing reached — see
 * `docs/followups.md` and `backend/docs/international-payments.md`
 * § Multi-route quote optimization.
 *
 * Three properties the UI has to get right, and each is asserted here:
 *
 *  1. **It is advisory.** The endpoint books no `Payment`, claims no run and
 *     picks no rail — `payment_corridor.pick_corridor` plus the org's
 *     configured provider still decide that at execute time (`decisions.md`
 *     §42). The dialog says so BEFORE any figure; a comparison a user reads as
 *     a choice is worse than no comparison.
 *
 *  2. **A non-quoting processor is shown, not omitted.** An adapter with no
 *     published fee schedule fails closed (`PaymentAdapter.quote_payment` →
 *     `available: false`, `unavailable_reason: "no_quote_endpoint"`) and drops
 *     out of the RANKING. `modern_treasury` is exactly that case today. A
 *     tenant paying on such a rail would otherwise watch an auction its own
 *     rail never entered and read the winner as the best route open to it — a
 *     ranking that hides its own gaps is worse than no ranking.
 *
 *  3. **The role gate is the endpoint's own** (`require_roles(admin,
 *     ap_manager, cfo)`), so an `ap_clerk` never sees a control that can only
 *     403. That half is asserted against the REAL backend at the bottom of the
 *     file, where a mock would prove nothing.
 *
 * Quote payloads are driven through `page.route()` — a real response the page
 * parses, not a stub of the page's own state — because which processors a
 * TENANT has configured is not this file's to control, and `mock` is the only
 * adapter a fresh clone has (so a live call yields a one-horse auction with no
 * unavailable row to assert on).
 */

const QUEUE_INVOICE_ID = '44444444-4444-4444-4444-444444444444';
const QUEUE_INVOICE_NUMBER = 'E2E-QUOTE-001';

function queueRow() {
	return {
		id: QUEUE_INVOICE_ID,
		invoice_number: QUEUE_INVOICE_NUMBER,
		vendor_name: 'E2E Corridor Vendor',
		amount: '5000.00',
		currency: 'USD',
		due_date: '2026-07-01',
		payment_terms: 'Net 30',
		status: 'approved',
		is_overdue: false,
		discount_eligible: false,
		discount_date: null,
		discount_percent: null,
		discount_amount: null
	};
}

/** Pin the Queue tab to exactly one payable invoice. */
async function mockQueue(page: import('@playwright/test').Page) {
	await page.route(
		(url) => url.pathname === '/api/payments/queue',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [queueRow()],
					total: 1,
					page: 1,
					page_size: 20,
					selectable_total: 1,
					blocked_total: 0,
					currency: 'USD',
					unconverted_count: 0,
					by_currency: [{ currency: 'USD', total: '5000.00', count: 1 }]
				})
			})
	);
}

/**
 * A ranking with two real quotes and one processor that publishes no fee
 * schedule — the `modern_treasury` shape the backend produces today.
 */
function comparison(mode: 'cheapest' | 'fastest' = 'cheapest') {
	return {
		invoice_id: QUEUE_INVOICE_ID,
		mode,
		currency: 'USD',
		amount: '5000.00',
		winner: {
			provider: 'increase',
			method: 'ach',
			available: true,
			unavailable_reason: null,
			total_cost: '12.50',
			flat_fee: '2.50',
			pct_fee: '0.002',
			eta_business_days: 2,
			fx_rate: null
		},
		runners_up: [
			{
				provider: 'column',
				method: 'ach',
				available: true,
				unavailable_reason: null,
				total_cost: '27.50',
				flat_fee: '2.50',
				pct_fee: '0.005',
				eta_business_days: 1,
				fx_rate: null
			},
			{
				// The whole point of test (2): listed, never ranked, never hidden.
				provider: 'modern_treasury',
				method: 'ach',
				available: false,
				unavailable_reason: 'no_quote_endpoint',
				total_cost: null,
				flat_fee: '0',
				pct_fee: '0',
				eta_business_days: 0,
				fx_rate: null
			}
		],
		savings_vs_runner_up: '15.00',
		advisory: true
	};
}

async function mockQuotes(
	page: import('@playwright/test').Page,
	body: unknown = comparison(),
	status = 200
) {
	await page.route('**/api/payments/corridor-quotes', (route) =>
		route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
	);
}

async function openQueue(page: import('@playwright/test').Page) {
	// Queue is the default tab, so there is nothing to click. (Its button's
	// accessible name carries the pending count — "Queue 8" — so an exact-name
	// click would be pinned to whatever this worker's tenant happens to hold.)
	await page.goto('/payments');
	await expect(page.getByText(QUEUE_INVOICE_NUMBER)).toBeVisible({ timeout: 10_000 });
}

// Located by accessible name — the row action's per-row `aria-label` names the
// invoice, so a screen reader hears which one it would price (WCAG 2.5.3 is why
// the visible label is a prefix of it).
const compareAction = (page: import('@playwright/test').Page) =>
	page.getByRole('button', { name: `Compare routes for invoice ${QUEUE_INVOICE_NUMBER}` });

test.describe('/payments — corridor quote comparison (advisory)', () => {
	test('a permitted role gets the ranking, and it leads with the advisory notice', async ({
		page
	}) => {
		await mockQueue(page);
		await mockQuotes(page);
		await openQueue(page);

		await compareAction(page).click();

		// The advisory sentence renders before any figure, and it says the two
		// things the endpoint's own docstring says: it prices, and it decides
		// nothing.
		const advisory = page.getByTestId('quotes-advisory');
		await expect(advisory).toBeVisible();
		await expect(advisory).toContainText(/advisory/i);
		await expect(advisory).toContainText(/does not choose a route/i);

		// The ranked routes, cheapest first, with the winner marked.
		const ranking = page.getByTestId('quotes-ranking');
		await expect(ranking).toBeVisible();
		await expect(ranking).toContainText('increase');
		await expect(ranking).toContainText('column');
		const rows = page.getByTestId('quote-row');
		await expect(rows).toHaveCount(2);
		await expect(rows.first()).toContainText('increase');
		await expect(rows.first()).toContainText('Best');

		// Money renders through the shared formatter, and the SAVING is the
		// server's own figure — the client never subtracts two amounts.
		await expect(page.getByTestId('quote-total').first()).toContainText('12.50');
		await expect(page.getByTestId('quotes-savings')).toContainText('15.00');
	});

	test('a processor with no fee schedule is shown as unranked, never omitted', async ({
		page
	}) => {
		await mockQueue(page);
		await mockQuotes(page);
		await openQueue(page);
		await compareAction(page).click();

		// It is NOT in the ranking...
		const rows = page.getByTestId('quote-row');
		await expect(rows).toHaveCount(2);
		await expect(page.getByTestId('quotes-ranking')).not.toContainText('modern_treasury');

		// ...and it is on screen anyway, with the reason in words and the
		// standing caveat that the ranking may not be the whole picture.
		const unranked = page.getByTestId('quotes-unranked');
		await expect(unranked).toBeVisible();
		await expect(unranked).toContainText('modern_treasury');
		await expect(unranked).toContainText(/publishes no fee schedule/i);
		await expect(unranked).toContainText(/not the whole picture/i);
		await expect(page.getByTestId('quote-unranked-row')).toHaveCount(1);
	});

	test('an adapter-authored reason renders verbatim rather than a made-up bucket', async ({
		page
	}) => {
		const body = comparison();
		body.runners_up[1].unavailable_reason = "method 'sepa' not supported by column";
		await mockQueue(page);
		await mockQuotes(page, body);
		await openQueue(page);
		await compareAction(page).click();

		// The adapter's own sentence says more than any bucket we could invent
		// for an unmapped code, so it is passed through untouched.
		await expect(page.getByTestId('quotes-unranked')).toContainText(
			"method 'sepa' not supported by column"
		);
	});

	test('switching to fastest re-asks the server rather than re-sorting client-side', async ({
		page
	}) => {
		await mockQueue(page);
		let lastMode: string | undefined;
		await page.route('**/api/payments/corridor-quotes', async (route) => {
			const body = route.request().postDataJSON() as { mode?: string };
			lastMode = body.mode;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(comparison((body.mode as 'cheapest' | 'fastest') ?? 'cheapest'))
			});
		});

		await openQueue(page);
		await compareAction(page).click();
		await expect.poll(() => lastMode).toBe('cheapest');

		await page.getByTestId('quotes-mode-fastest').click();
		// Ranking by ETA is the SERVER's ordering rule (`_rank`); re-sorting the
		// loaded rows here would silently disagree with it.
		await expect.poll(() => lastMode).toBe('fastest');
	});

	test('a 409 (nobody can quote this corridor) stays on screen instead of fading', async ({
		page
	}) => {
		await mockQueue(page);
		await mockQuotes(
			page,
			{
				detail:
					'no provider can quote method=sepa for USD/?: no_quote_endpoint; provider_not_supported'
			},
			409
		);
		await openQueue(page);
		await compareAction(page).click();

		// The 409 body names each provider's own machine reason — that is the
		// actionable half of the refusal, and a toast would take it away.
		const err = page.getByTestId('quotes-error');
		await expect(err).toBeVisible();
		await expect(err).toContainText('no provider can quote');
		await expect(page.getByTestId('quotes-ranking')).toHaveCount(0);
	});

	test('a clerk sees no compare control — the endpoint refuses that role', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await mockQueue(page);
		await mockQuotes(page);
		await openQueue(page);

		// Mirrors `require_roles(admin, ap_manager, cfo)`. The row still renders;
		// only the control it could never use is absent.
		await expect(compareAction(page)).toHaveCount(0);
	});
});

test.describe('/payments — corridor quotes against the real backend', () => {
	test('the endpoint refuses a clerk and answers a manager', async ({
		page,
		tenantClerk,
		tenantManager
	}) => {
		// A mocked route proves nothing about the gate, so both halves are asked
		// of the live API with a real invoice id from the caller's own queue.
		await signInAndWait(page, tenantManager);
		const queueResp = await page.request.get(`${API_BASE}/api/payments/queue?page=1&page_size=1`, {
			headers: await authedTenantHeaders(page)
		});
		expect(queueResp.status()).toBe(200);
		const queue = (await queueResp.json()) as { items: { id: string }[] };
		test.skip(queue.items.length === 0, 'this worker’s tenant has no payable invoice queued');
		const invoiceId = queue.items[0].id;

		const managerResp = await page.request.post(`${API_BASE}/api/payments/corridor-quotes`, {
			headers: await authedTenantHeaders(page),
			data: { invoice_id: invoiceId, mode: 'cheapest' }
		});
		// 200 with a ranking, or 409 when this tenant has no provider that can
		// quote the corridor — both are the endpoint answering. A 403 would not be.
		expect([200, 409]).toContain(managerResp.status());
		if (managerResp.status() === 200) {
			const body = (await managerResp.json()) as { advisory: boolean; winner: unknown };
			// The server states its own advisory nature in the payload, not just
			// its docstring — the UI's notice is not the only place it is said.
			expect(body.advisory).toBe(true);
			expect(body.winner).toBeTruthy();
		}

		await signInAndWait(page, tenantClerk);
		const clerkResp = await page.request.post(`${API_BASE}/api/payments/corridor-quotes`, {
			headers: await authedTenantHeaders(page),
			data: { invoice_id: invoiceId, mode: 'cheapest' }
		});
		expect(clerkResp.status()).toBe(403);
	});
});
