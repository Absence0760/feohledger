import type { Page, Request } from '@playwright/test';
import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — search, column sort and the severity chip row (GitHub #443).
 *
 * The queue shipped with neither a `SearchBox` nor a `SortableHeader` because
 * the backend had no parameter for either, and a client-side `.filter()` over
 * the loaded page is the anti-pattern `frontend/docs/ui-patterns.md` § Search
 * forbids. These specs pin the three things that make the new controls honest:
 *
 *  - every filter — search included — reaches the list, the chip tallies AND
 *    the "select all N matching" resolver, so no count on the page describes a
 *    different set from the table;
 *  - each control is URL-backed, so a reload or a pasted link reproduces it;
 *  - sort sends only allowlisted keys, and each header's `aria-sort` states the
 *    order of the values it SHOWS (the Age column is `created_at` read backwards).
 *
 * The `stubQueue` tests stub both endpoints so they assert the page's wiring
 * exactly; the last two run against the real backend to prove the search and
 * sort are server-side end to end.
 */

function exceptionRow(n: number, severity = 'warning') {
	return {
		id: `00000000-0000-4000-9200-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-SRCH-${n}`,
		vendor_name: 'Search Vendor',
		amount: 100,
		currency: 'USD',
		exception_type: 'duplicate',
		type_label: 'Duplicate Invoice',
		severity,
		description: 'stubbed',
		status: 'open',
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

const SUMMARY = exceptionSummary({
	open: 7,
	by_type: { duplicate: 7 },
	by_severity: { error: 2, warning: 5 }
});

/**
 * Stub the list + summary, recording every request each receives. `total` lets
 * a test claim a set larger than the one row served, which is what makes the
 * "select all N matching" affordance appear.
 */
async function stubQueue(page: Page, opts: { total?: number } = {}) {
	const lists: URLSearchParams[] = [];
	const summaries: URLSearchParams[] = [];
	await page.route('**/api/exceptions/summary*', async (route) => {
		summaries.push(new URL(route.request().url()).searchParams);
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(SUMMARY)
		});
	});
	await page.route('**/api/exceptions?*', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname !== '/api/exceptions') {
			// `fallback`: lets the summary stub above answer its own URL.
			await route.fallback();
			return;
		}
		lists.push(url.searchParams);
		const severity = url.searchParams.get('severity');
		const items = severity === 'error' ? [exceptionRow(1, 'error')] : [exceptionRow(2)];
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({
				items,
				total: opts.total ?? items.length,
				page: 1,
				page_size: 20
			})
		});
	});
	return { lists, summaries };
}

function isQueueList(req: Request): boolean {
	return req.method() === 'GET' && new URL(req.url()).pathname === '/api/exceptions';
}

function isSummary(req: Request): boolean {
	return new URL(req.url()).pathname === '/api/exceptions/summary';
}

test.describe('/exceptions — search, sort and severity', () => {
	test('a search reaches the list, the chip tallies and select-all, and survives a reload', async ({
		page
	}) => {
		const { lists } = await stubQueue(page, { total: 25 });
		await page.route('**/api/exceptions/ids*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ids: [exceptionRow(2).id], total: 1, truncated: false })
			})
		);

		await page.goto('/exceptions');
		await expect(page.getByText('E2E-SRCH-2')).toBeVisible();
		expect(lists.at(-1)?.get('search')).toBeNull();

		const listReq = page.waitForRequest(
			(r) => isQueueList(r) && new URL(r.url()).searchParams.get('search') === 'acme'
		);
		const summaryReq = page.waitForRequest(
			(r) => isSummary(r) && new URL(r.url()).searchParams.get('search') === 'acme'
		);
		await page.getByRole('textbox', { name: 'Search exceptions by invoice number or vendor' }).fill(
			'  acme  '
		);
		// Trimmed on the wire, and the chip tallies carry the same term as the
		// table — a summary counting the whole tenant above a searched table is
		// the defect the faceted endpoint exists to prevent.
		await listReq;
		await summaryReq;
		await expect(page).toHaveURL(/[?&]search=acme(&|$)/);

		// "Select all N matching" resolves the SEARCHED set — the one the table
		// shows — not the whole open queue.
		await page.getByLabel('Select all selectable exceptions').check();
		const idsReq = page.waitForRequest(
			(r) => new URL(r.url()).pathname === '/api/exceptions/ids'
		);
		await page.getByRole('button', { name: 'Select all 25 matching' }).click();
		const ids = new URL((await idsReq).url()).searchParams;
		expect(ids.get('search')).toBe('acme');
		expect(ids.get('status')).toBe('open');

		// A reload reproduces the searched view from the URL alone.
		const reloadedList = page.waitForRequest(
			(r) => isQueueList(r) && new URL(r.url()).searchParams.get('search') === 'acme'
		);
		await page.reload();
		await reloadedList;
		await expect(
			page.getByRole('textbox', { name: 'Search exceptions by invoice number or vendor' })
		).toHaveValue('acme');
	});

	test('the severity row shows its tallies, narrows via ?severity=, and is URL-backed', async ({
		page
	}) => {
		await stubQueue(page);
		await page.goto('/exceptions');

		const allSeverities = page.locator('.filter-chip', { hasText: /^All severities/ });
		await expect(allSeverities).toContainText('7');
		const error = page.locator('.filter-chip', { hasText: /^Error/ });
		await expect(error).toContainText('2');
		await expect(page.locator('.filter-chip', { hasText: /^Warning/ })).toContainText('5');
		// A severity with no rows still renders, at 0 — a chip that vanished at
		// zero would take its pressed state with it.
		await expect(page.locator('.filter-chip', { hasText: /^Info/ })).toContainText('0');
		await expect(allSeverities).toHaveAttribute('aria-pressed', 'true');

		const listReq = page.waitForRequest(
			(r) => isQueueList(r) && new URL(r.url()).searchParams.get('severity') === 'error'
		);
		const summaryReq = page.waitForRequest(
			(r) => isSummary(r) && new URL(r.url()).searchParams.get('severity') === 'error'
		);
		await error.click();
		await listReq;
		await summaryReq;
		await expect(error).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByText('E2E-SRCH-1')).toBeVisible();
		await expect(page).toHaveURL(/[?&]severity=error(&|$)/);

		await page.reload();
		await expect(page.locator('.filter-chip', { hasText: /^Error/ })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
		await expect(page.getByText('E2E-SRCH-1')).toBeVisible();
	});

	test('a pressed type chip stays on screen when a search empties its tally', async ({ page }) => {
		// The type tallies are faceted over the search, so a term can drop the
		// selected type out of `by_type`. The chip must survive at 0 — otherwise
		// the table is narrowed by a filter nothing on screen shows or can undo.
		await stubQueue(page);
		await page.goto('/exceptions?type=fraud_flag');

		const fraud = page.locator('.type-chip', { hasText: 'Fraud Flag' });
		await expect(fraud).toHaveAttribute('aria-pressed', 'true');
		await expect(fraud.locator('.count')).toHaveText('0');

		await fraud.click();
		await expect(page).not.toHaveURL(/type=/);
		await expect(page.locator('.type-chip', { hasText: 'All types' })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
	});

	test('sort headers send allowlisted keys, are URL-backed, and never refetch the tallies', async ({
		page
	}) => {
		const { summaries } = await stubQueue(page);
		await page.goto('/exceptions');
		await expect(page.getByText('E2E-SRCH-2')).toBeVisible();
		const summariesBefore = summaries.length;

		async function clickSort(label: string, sort: string, order: string) {
			const req = page.waitForResponse(
				(r) =>
					isQueueList(r.request()) &&
					new URL(r.url()).searchParams.get('sort') === sort &&
					new URL(r.url()).searchParams.get('order') === order
			);
			await page.getByRole('columnheader', { name: label, exact: true }).getByRole('button').click();
			await req;
		}

		const sev = page.getByRole('columnheader', { name: 'Sev', exact: true });
		await expect(sev).toHaveAttribute('aria-sort', 'none');
		await clickSort('Sev', 'severity', 'asc');
		await expect(sev).toHaveAttribute('aria-sort', 'ascending');
		await expect(page).toHaveURL(/sort=severity&order=asc/);
		await clickSort('Sev', 'severity', 'desc');
		await expect(sev).toHaveAttribute('aria-sort', 'descending');

		// Age is created_at read backwards: its first click is YOUNGEST first —
		// ascending age — which the wire spells `created_at desc`.
		const age = page.getByRole('columnheader', { name: 'Age', exact: true });
		await clickSort('Age', 'created_at', 'desc');
		await expect(age).toHaveAttribute('aria-sort', 'ascending');
		await expect(sev).toHaveAttribute('aria-sort', 'none');
		await clickSort('Age', 'created_at', 'asc');
		await expect(age).toHaveAttribute('aria-sort', 'descending');

		await clickSort('Due', 'due_at', 'asc');
		await expect(page.getByRole('columnheader', { name: 'Due', exact: true })).toHaveAttribute(
			'aria-sort',
			'ascending'
		);
		await expect(page).toHaveURL(/sort=due_at&order=asc/);

		// Sort reorders the set and cannot change a single tally. `reload()`
		// issues the summary in the same tick as the list, so had sorting
		// refetched it the request would already be recorded here.
		expect(summaries.length).toBe(summariesBefore);

		// A reload keeps the column and direction.
		const reloaded = page.waitForRequest(
			(r) => isQueueList(r) && new URL(r.url()).searchParams.get('sort') === 'due_at'
		);
		await page.reload();
		await reloaded;
		await expect(page.getByRole('columnheader', { name: 'Due', exact: true })).toHaveAttribute(
			'aria-sort',
			'ascending'
		);
	});

	test('search runs server-side on the real queue, and the Open chip counts the searched set', async ({
		page
	}) => {
		const resp = await page.request.get(`${API_BASE}/api/exceptions?status=open&page_size=100`, {
			headers: await authedTenantHeaders(page)
		});
		const open = ((await resp.json()) as { items: { invoice_number: string | null }[] }).items;
		const target = open.find((row) => row.invoice_number)?.invoice_number;
		expect(target, 'the tenant seed must carry an open exception on an invoice').toBeTruthy();

		await page.goto('/exceptions');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const searched = page.waitForResponse(
			(r) =>
				isQueueList(r.request()) && new URL(r.url()).searchParams.get('search') === target
		);
		const tallied = page.waitForResponse(
			(r) => isSummary(r.request()) && new URL(r.url()).searchParams.get('search') === target
		);
		await page
			.getByRole('textbox', { name: 'Search exceptions by invoice number or vendor' })
			.fill(target!);
		const body = (await (await searched).json()) as {
			items: { invoice_number: string | null; vendor_name: string | null }[];
			total: number;
		};
		await tallied;

		expect(body.total).toBeGreaterThan(0);
		for (const item of body.items) {
			const haystack = `${item.invoice_number ?? ''} ${item.vendor_name ?? ''}`.toLowerCase();
			expect(haystack).toContain(target!.toLowerCase());
		}
		await expect(page.locator('table tbody tr')).toHaveCount(body.items.length);
		// The Open chip is faceted over the search — it counts the rows on screen,
		// not the tenant's open queue.
		await expect(
			page.locator('.filter-chip', { hasText: /^Open/ }).locator('.count')
		).toHaveText(String(body.total));
	});

	test('severity sort ranks worst-first on the real queue', async ({ page }) => {
		await page.goto('/exceptions?status=all');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const sorted = page.waitForResponse(
			(r) =>
				isQueueList(r.request()) &&
				new URL(r.url()).searchParams.get('sort') === 'severity' &&
				new URL(r.url()).searchParams.get('order') === 'desc'
		);
		const sev = page.getByRole('columnheader', { name: 'Sev', exact: true }).getByRole('button');
		await sev.click();
		await sev.click();
		const body = (await (await sorted).json()) as { items: { severity: string }[] };

		// Worst-first by RANK: alphabetical descending would be warning, info,
		// error — the informational rows between the two that need attention.
		const rank: Record<string, number> = { error: 3, warning: 2, info: 1 };
		const ranks = body.items.map((i) => rank[i.severity] ?? 0);
		expect(ranks).toEqual([...ranks].sort((a, b) => b - a));
	});
});
