import { expect, test } from '../fixtures/helpers';

/**
 * /payments — the Cards table distinguishes loading / failed / genuinely empty.
 *
 * Every other list on this page already did: the Queue and History tabs, the
 * Runs tab (`tests-e2e/payments/runs-load-states.spec.ts`), and the Rebates
 * table stacked directly ABOVE this one on the same tab. The Cards table was
 * the last one here with no error flag at all, so a failed read rendered
 * "No virtual cards issued yet." — answering "we could not look" with "there is
 * nothing", permanently, with the toast that explained it already faded.
 *
 * `/exceptions` states the rule verbatim: never reintroduce an empty message
 * that outranks "we could not look".
 *
 * Stubbed so both states are deterministic: a real backend answers instantly
 * and never fails on demand. The assertions are scoped to
 * `[data-testid="cards-table"]` for the same reason `cards-pagination.spec.ts`
 * is — this tab stacks two tables, and a page-wide lookup reads the rebates
 * one.
 */

async function openCards(page: import('@playwright/test').Page) {
	await page.goto('/payments');
	await page.getByRole('tab', { name: 'Cards', exact: true }).click();
}

/** Keep the sibling rebates list out of the way — it has its own error state,
 *  and a second failing request would make it ambiguous which table is being
 *  asserted on. */
async function stubRebatesEmpty(page: import('@playwright/test').Page) {
	await page.route('**/api/cards/rebates**', (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items: [],
				total: 0,
				page: 1,
				page_size: 20,
				total_amount: null,
				currency: 'USD'
			})
		})
	);
}

test.describe('/payments Cards table load states', () => {
	test('a FAILED load says so — never "No virtual cards issued yet."', async ({ page }) => {
		await stubRebatesEmpty(page);
		await page.route('**/api/cards?**', (r) =>
			r.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			})
		);

		await openCards(page);

		const empty = page.getByTestId('cards-table').getByTestId('table-empty');
		await expect(empty).toBeVisible({ timeout: 10_000 });
		await expect(empty).toHaveText('Could not load virtual cards.');
		// The critical negative: an outage must not read as "you have none".
		await expect(empty).not.toContainText('No virtual cards issued yet.');
	});

	test('a SLOW load shows the loading state, not the empty claim', async ({ page }) => {
		await stubRebatesEmpty(page);
		// A real readiness gate, not a sleep: the response is released by
		// resolving this promise, so nothing is timing-dependent.
		let release!: () => void;
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		await page.route('**/api/cards?**', async (route) => {
			await held;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 })
			});
		});

		await openCards(page);

		const empty = page.getByTestId('cards-table').getByTestId('table-empty');
		await expect(empty).toBeVisible({ timeout: 10_000 });
		await expect(empty).toHaveText('Loading cards…');
		await expect(empty).not.toContainText('No virtual cards issued yet.');

		release();

		// Once the genuinely-empty response lands, the empty claim is earned.
		await expect(empty).toHaveText('No virtual cards issued yet.', { timeout: 10_000 });
	});
});
