import { expect, test } from '../fixtures/helpers';
import type { Page, Request } from '@playwright/test';

/**
 * `/credit-memos` — server-side search, column sort, and the chip counts.
 *
 * All three were missing for the same reason (issue #443): the shared
 * `SearchBox` / `SortableHeader` / chip-count primitives existed, the backend
 * parameters did not. Approximating search with a `.filter()` over the one
 * loaded page would have searched a PAGE rather than the set, so the router
 * grew `search`, a `sort` allowlist and `GET /api/credit-memos/summary`, and
 * this page wires them the way its siblings do: URL-backed, sequenced, and
 * with the chips counting exactly the population the table is showing.
 *
 * The list and summary are stubbed so the assertions are about the page's
 * contract with the API, not about what the worker's tenant holds. Every stub
 * matches on the EXACT pathname via a URL predicate: under `vite dev` a glob
 * such as `**\/api/credit-memos*` would also be offered the dev server's own
 * module requests (`/src/lib/api/*.ts`), and answering one of those with JSON
 * blanks the route — see `tests-e2e/README.md` § Stubbing an API route.
 */

const LIST = '/api/credit-memos';
const SUMMARY = '/api/credit-memos/summary';

function memo(n: number, vendor = 'Globex Corporation') {
	return {
		id: `00000000-0000-4000-b000-${String(n).padStart(12, '0')}`,
		memo_number: `E2E-SRCH-${n}`,
		vendor_id: '00000000-0000-4000-b001-000000000001',
		vendor_name: vendor,
		invoice_id: null,
		invoice_number: null,
		amount: 10 * n,
		currency: 'USD',
		issued_date: '2026-01-01',
		reason: null,
		status: 'open',
		applied_at: null,
		applied_by: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

const isGet = (pathname: string) => (url: URL) => url.pathname === pathname;

function json(body: unknown, status = 200) {
	return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/** The list request the page issues, as a query object. */
function listQuery(request: Request): Record<string, string> {
	return Object.fromEntries(new URL(request.url()).searchParams);
}

async function stubInvoices(page: Page) {
	// The page loads every invoice for its two invoice selects; empty is fine.
	await page.route(isGet('/api/invoices'), (route) =>
		route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }))
	);
}

test.describe('/credit-memos — search, sort and chip counts', () => {
	test('search is a server filter: URL-backed, sent to the list AND the chip summary', async ({
		page
	}) => {
		await stubInvoices(page);
		const summarySearches: string[] = [];
		await page.route(isGet(LIST), (route) => {
			const term = listQuery(route.request()).search ?? '';
			return route.fulfill(
				json(
					term === 'globex'
						? { items: [memo(1)], total: 1 }
						: { items: [memo(1), memo(2, 'Initech')], total: 2 }
				)
			);
		});
		await page.route(isGet(SUMMARY), (route) => {
			const term = new URL(route.request().url()).searchParams.get('search') ?? '';
			summarySearches.push(term);
			return route.fulfill(
				json(
					term === 'globex'
						? { total: 1, by_status: { open: 1, applied: 0, void: 0 } }
						: { total: 5, by_status: { open: 3, applied: 1, void: 1 } }
				)
			);
		});

		await page.goto('/credit-memos');
		// The chips count the WHOLE set, not the two rows on screen.
		await expect(page.getByRole('button', { name: 'All 5', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Open 3', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Applied 1', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Void 1', exact: true })).toBeVisible();

		const searched = page.waitForRequest(
			(r) => new URL(r.url()).pathname === LIST && listQuery(r).search === 'globex'
		);
		await page.getByRole('textbox', { name: 'Search credit memos' }).fill('globex');
		await searched;
		await expect(page).toHaveURL(/[?&]search=globex(&|$)/);
		await expect(page.getByText('E2E-SRCH-2')).toHaveCount(0);
		await expect(page.getByText('E2E-SRCH-1')).toBeVisible();
		// The chips narrowed with the table — the same term reached the summary.
		await expect(page.getByRole('button', { name: 'All 1', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Applied 0', exact: true })).toBeVisible();
		expect(summarySearches.at(-1)).toBe('globex');

		// A reload (or a pasted link) reproduces the searched view.
		const remount = page.waitForRequest((r) => new URL(r.url()).pathname === LIST);
		await page.reload();
		expect(listQuery(await remount).search).toBe('globex');
		await expect(page.getByRole('textbox', { name: 'Search credit memos' })).toHaveValue('globex');

		// Clearing the box re-fires WITHOUT the term — not just a visual clear.
		const cleared = page.waitForRequest(
			(r) => new URL(r.url()).pathname === LIST && !('search' in listQuery(r))
		);
		await page.getByRole('textbox', { name: 'Search credit memos' }).fill('');
		await cleared;
		await expect(page.getByText('E2E-SRCH-2')).toBeVisible();
		await expect(page).not.toHaveURL(/search=/);
	});

	test('a failed summary leaves bare chip labels, never a page-local tally', async ({ page }) => {
		await stubInvoices(page);
		await page.route(isGet(LIST), (route) =>
			route.fulfill(json({ items: [memo(1), memo(2)], total: 2 }))
		);
		await page.route(isGet(SUMMARY), (route) => route.fulfill(json({ detail: 'boom' }, 500)));

		await page.goto('/credit-memos');
		await expect(page.getByText('E2E-SRCH-2')).toBeVisible();
		// Two rows are loaded; "Open 2" would be a claim about the whole set
		// that nothing established.
		await expect(page.getByRole('button', { name: 'All', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Open', exact: true })).toBeVisible();
	});

	test('a filter that matches nothing says so, rather than claiming there are no memos', async ({
		page
	}) => {
		await stubInvoices(page);
		await page.route(isGet(LIST), (route) => {
			const term = listQuery(route.request()).search ?? '';
			return route.fulfill(json(term ? { items: [], total: 0 } : { items: [memo(1)], total: 1 }));
		});
		await page.route(isGet(SUMMARY), (route) =>
			route.fulfill(json({ total: 1, by_status: { open: 1, applied: 0, void: 0 } }))
		);

		await page.goto('/credit-memos?search=nothing-matches');
		await expect(page.getByTestId('table-empty')).toHaveText('No credit memos match this filter.');
		// …and not the zero-data onboarding block either.
		await expect(page.getByTestId('credit-memos-empty-state')).toHaveCount(0);
	});

	test('column sort sends an allowlisted key and survives a reload', async ({ page }) => {
		await stubInvoices(page);
		await page.route(isGet(SUMMARY), (route) =>
			route.fulfill(json({ total: 2, by_status: { open: 2, applied: 0, void: 0 } }))
		);
		await page.route(isGet(LIST), (route) =>
			route.fulfill(json({ items: [memo(1), memo(2)], total: 2 }))
		);

		const listRequest = (expected: Record<string, string>) =>
			page.waitForRequest((r) => {
				if (new URL(r.url()).pathname !== LIST) return false;
				const q = listQuery(r);
				return Object.entries(expected).every(([k, v]) => q[k] === v);
			});

		await page.goto('/credit-memos');
		await expect(page.getByText('E2E-SRCH-2')).toBeVisible();

		const amountHeader = page.getByRole('columnheader', { name: /Amount/ });
		await expect(amountHeader).toHaveAttribute('aria-sort', 'none');

		let sent = listRequest({ sort: 'amount', order: 'asc' });
		await amountHeader.getByRole('button').click();
		await sent;
		await expect(amountHeader).toHaveAttribute('aria-sort', 'ascending');
		await expect(page).toHaveURL(/sort=amount&order=asc/);

		// The active column flips; the others stay unsorted.
		sent = listRequest({ sort: 'amount', order: 'desc' });
		await amountHeader.getByRole('button').click();
		await sent;
		await expect(amountHeader).toHaveAttribute('aria-sort', 'descending');

		// URL-backed: a reload issues the same sort on mount.
		sent = listRequest({ sort: 'amount', order: 'desc' });
		await page.reload();
		await sent;
		await expect(page.getByRole('columnheader', { name: /Amount/ })).toHaveAttribute(
			'aria-sort',
			'descending'
		);

		// The other two sortable columns send their own keys.
		sent = listRequest({ sort: 'issued_date', order: 'asc' });
		await page.getByRole('columnheader', { name: /Issued/ }).getByRole('button').click();
		await sent;
		sent = listRequest({ sort: 'memo_number', order: 'asc' });
		await page.getByRole('columnheader', { name: /Memo #/ }).getByRole('button').click();
		await sent;
	});

	test('a bookmarked sort the API does not allow is dropped, not sent to a 422', async ({
		page
	}) => {
		await stubInvoices(page);
		await page.route(isGet(SUMMARY), (route) =>
			route.fulfill(json({ total: 1, by_status: { open: 1, applied: 0, void: 0 } }))
		);
		const mount = page.waitForRequest((r) => new URL(r.url()).pathname === LIST);
		await page.route(isGet(LIST), (route) => route.fulfill(json({ items: [memo(1)], total: 1 })));

		await page.goto('/credit-memos?sort=vendor_id&order=desc');
		expect(listQuery(await mount)).not.toHaveProperty('sort');
		await expect(page.getByText('E2E-SRCH-1')).toBeVisible();
	});
});
