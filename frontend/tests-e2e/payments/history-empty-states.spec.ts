import { expect, test } from '../fixtures/helpers';

/**
 * /payments History — the table's empty line names the state it is in.
 *
 * It used to read "No payments match your filters." whenever the list was
 * empty: on an unfiltered first visit (blaming filters nobody had set), and
 * while the first fetch was still in flight. A failed load rendered the same
 * table line with "Try again." and nothing to press. Now an unfiltered empty
 * list says there are no payments yet, a filtered one keeps the "match your
 * filters" copy, and a failure is `ui/LoadError` with a working Retry.
 *
 * Stubbed: a real backend answers instantly, never fails on demand, and the
 * seed tenant is not empty.
 */

const EMPTY_PAGE = JSON.stringify({ items: [], total: 0, page: 1, page_size: 50 });

async function stubPayments(
	page: import('@playwright/test').Page,
	respond: (attempt: number) => { status: number; body: string }
) {
	let attempts = 0;
	await page.route('**/api/payments*', async (route) => {
		if (new URL(route.request().url()).pathname !== '/api/payments') {
			await route.continue();
			return;
		}
		attempts += 1;
		const { status, body } = respond(attempts);
		await route.fulfill({ status, contentType: 'application/json', body });
	});
	await page.route('**/api/payments/counts*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ total: 0, by_status: {} })
		})
	);
	return () => attempts;
}

test.describe('/payments History empty states', () => {
	test('an unfiltered empty list says there are no payments yet', async ({ page }) => {
		await stubPayments(page, () => ({ status: 200, body: EMPTY_PAGE }));
		await page.goto('/payments?tab=history');

		const empty = page.getByTestId('table-empty');
		await expect(empty).toHaveText(/^No payments yet\./);
		await expect(empty).not.toContainText('match your filters');
	});

	test('a search that matches nothing keeps the filtered copy', async ({ page }) => {
		await stubPayments(page, () => ({ status: 200, body: EMPTY_PAGE }));
		await page.goto('/payments?tab=history&search=nothing-matches-this');

		await expect(page.getByTestId('table-empty')).toHaveText('No payments match your filters.');
	});

	test('a failed load offers a Retry that reloads the list', async ({ page }) => {
		const attempts = await stubPayments(page, (n) =>
			n === 1
				? { status: 500, body: JSON.stringify({ detail: 'boom' }) }
				: { status: 200, body: EMPTY_PAGE }
		);
		await page.goto('/payments?tab=history');

		const error = page.getByTestId('history-error');
		await expect(error).toContainText('Could not load payments.');
		await expect(page.getByTestId('table-empty')).toHaveCount(0);

		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('table-empty')).toHaveText(/^No payments yet\./);
		expect(attempts()).toBeGreaterThan(1);
	});
});
