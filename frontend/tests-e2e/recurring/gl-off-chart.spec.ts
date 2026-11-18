import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * `/recurring` surfaces a template whose GL code the chart no longer accepts.
 *
 * Generation re-judges the template's `gl_account` against its entity's chart
 * (`services/recurring_invoices._gl_code_for_generation`); a retired, unknown
 * or another entity's code is not stamped — the invoice is raised uncoded with
 * `gl_codes_not_in_chart`. The template keeps the code, so the list has to say
 * which templates need re-coding: a per-row badge (`gl_account_off_chart`) and
 * a whole-set filter (`gl_off_chart`, counted by `GET /api/recurring/summary`).
 *
 * The "Not generating" badge for a template the sweep skipped shares the
 * same shape (`ui/ExplainedBadge`), so it is pinned here too.
 *
 * The backend verdict is pinned by `backend/tests/test_gl_code_entity_chart.py`;
 * responses are stubbed here so the assertion doesn't depend on the shard
 * tenant's chart.
 */

type LastSkip = {
	reason: string;
	period_key: string | null;
	consecutive: number;
	last_skipped_at: string;
};

function template(n: number, glOffChart: boolean, lastSkip: LastSkip | null = null) {
	return {
		id: `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
		name: `E2E GL Template ${n}`,
		entity_id: null,
		vendor_id: null,
		vendor_name: 'Acme',
		description: null,
		amount: '1000.00',
		currency: 'USD',
		gl_account: glOffChart ? '6800' : '6000',
		cost_center: null,
		department: null,
		project: null,
		po_number: null,
		payment_terms: null,
		cadence: 'monthly',
		day_of_period: 1,
		start_date: '2026-01-01',
		end_date: null,
		next_run_on: '2026-12-01',
		last_period_key: null,
		last_generated_at: null,
		generated_count: 0,
		status: 'active',
		variance_tolerance_pct: null,
		notes: null,
		last_skip: lastSkip,
		gl_account_off_chart: glOffChart,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z'
	};
}

/** Stub the list + summary, honouring `gl_off_chart`; returns the list queries seen. */
async function stubRecurring(
	page: Page,
	templates: ReturnType<typeof template>[] = [template(1, true), template(2, false)]
): Promise<URLSearchParams[]> {
	const listQueries: URLSearchParams[] = [];
	await page.route('**/api/recurring**', async (route) => {
		const url = new URL(route.request().url());
		const offOnly = url.searchParams.get('gl_off_chart') === 'true';
		if (url.pathname === '/api/recurring/summary') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					total: offOnly ? 1 : 2,
					by_status: { active: offOnly ? 1 : 2 },
					monthly_equivalent: [],
					soonest_next_run: '2026-12-01',
					gl_off_chart: 1
				})
			});
		}
		if (url.pathname === '/api/recurring') {
			listQueries.push(url.searchParams);
			const items = offOnly ? templates.filter((t) => t.gl_account_off_chart) : templates;
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items,
					total: items.length,
					page: 1,
					page_size: 20
				})
			});
		}
		return route.continue();
	});
	return listQueries;
}

test('a template coded to an account the chart dropped is badged and filterable', async ({
	page
}) => {
	const listQueries = await stubRecurring(page);

	await page.goto('/recurring');

	const stale = page.locator('tr').filter({ hasText: 'E2E GL Template 1' });
	const live = page.locator('tr').filter({ hasText: 'E2E GL Template 2' });
	// The dropped code is in the badge's own text, and the why in a
	// visually-hidden span — not only in a `title` tooltip, which a keyboard,
	// a touch screen and a screen reader never reach.
	const badge = stale.locator('.badge.gl-off-chart');
	await expect(badge).toContainText('GL 6800 needs re-coding');
	await expect(badge.locator('.visually-hidden')).toHaveText(
		/GL 6800 is not an active account in this template’s chart/
	);
	// The pill text and the explanation are separate words when read as text.
	await expect(badge).toHaveText(/^GL 6800 needs re-coding GL 6800 is not/);
	await expect(badge).toHaveAttribute('title', /6800/);
	await expect(live).toBeVisible();
	await expect(live.locator('.badge.gl-off-chart')).toHaveCount(0);

	// The filter chip carries the whole-set count and asks the server for the
	// set, rather than hiding rows of the loaded page.
	const chip = page.getByRole('button', { name: /Needs GL re-coding/ });
	await expect(chip.locator('.count')).toHaveText('1');
	await chip.click();
	await expect(chip).toHaveAttribute('aria-pressed', 'true');
	await expect(live).toHaveCount(0);
	await expect(stale).toBeVisible();
	await expect(page).toHaveURL(/[?&]gl=off_chart/);
	expect(listQueries.at(-1)?.get('gl_off_chart')).toBe('true');
});

test('a deep link to /recurring?gl=off_chart arrives filtered', async ({ page }) => {
	const listQueries = await stubRecurring(page);

	// A fresh load — no click — must read the filter from the URL: the chip
	// pressed, the server asked for the narrowed set, only that set listed.
	await page.goto('/recurring?gl=off_chart');

	const stale = page.locator('tr').filter({ hasText: 'E2E GL Template 1' });
	await expect(stale).toBeVisible();
	await expect(page.locator('tr').filter({ hasText: 'E2E GL Template 2' })).toHaveCount(0);
	const chip = page.getByRole('button', { name: /Needs GL re-coding/ });
	await expect(chip).toHaveAttribute('aria-pressed', 'true');
	await expect(page).toHaveURL(/[?&]gl=off_chart/);
	expect(listQueries.length).toBeGreaterThan(0);
	expect(listQueries.every((q) => q.get('gl_off_chart') === 'true')).toBe(true);
});

test('a template that is not generating says why in text, not only a tooltip', async ({
	page
}) => {
	await stubRecurring(page, [
		template(3, false, {
			reason: 'missing_amount',
			period_key: '2026-11',
			consecutive: 2,
			last_skipped_at: '2026-11-01T00:00:00Z'
		}),
		template(4, false)
	]);

	await page.goto('/recurring');

	const skipped = page.locator('tr').filter({ hasText: 'E2E GL Template 3' });
	const generating = page.locator('tr').filter({ hasText: 'E2E GL Template 4' });
	// The skipped-period count, the period and the reason are in the badge's
	// text (a visually-hidden span), which a keyboard, a touch screen and a
	// screen reader reach — a `title` alone reaches none of them. The tooltip
	// stays for the pointer user. (decisions §NEW-L4)
	const badge = skipped.locator('.badge.skipped');
	await expect(badge).toHaveText(
		'Not generating 2 periods skipped — last 2026-11: no amount set'
	);
	await expect(badge.locator('.visually-hidden')).toHaveText(
		'2 periods skipped — last 2026-11: no amount set'
	);
	await expect(badge).toHaveAttribute('title', '2 periods skipped — last 2026-11: no amount set');
	await expect(generating).toBeVisible();
	await expect(generating.locator('.badge.skipped')).toHaveCount(0);
});
