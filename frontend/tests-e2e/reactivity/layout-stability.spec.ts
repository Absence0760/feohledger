import type { Locator, Page } from '@playwright/test';

import { NAV } from '#lib/nav.ts';

import { expect, test } from '../fixtures/helpers';
import { exceptionSummary } from '../exceptions/summary';

/**
 * A list page must not move under the reader when its data lands.
 *
 * The operator report was "there are always glitches when I click on different
 * pages, especially the invoices page". Measured on a production build with a
 * PerformanceObserver: three sidebar pages drew their table, then pushed it
 * down once a late response arrived —
 *
 *   - /invoices: the status chips sat inline beside the search box. Their
 *     number depends on the active workflow and their width on the tallies,
 *     both of which load after first paint, so the row fitted on one line at
 *     mount and wrapped once they landed: the table dropped ~44px (a
 *     layout-shift score of 0.33 on a cold visit).
 *   - /payments: the four summary cards rendered only once
 *     `/api/payments/summary` answered, inserting a ~94px strip above the tabs
 *     and the table on every visit.
 *   - /exceptions: the type-filter row rendered only once the summary said
 *     which types exist, inserting ~40px above the table on every visit.
 *
 * Each case holds the late response(s) behind a gate, so the page is pinned in
 * its loading state for as long as the test needs, and compares where the
 * table (or tab bar) sits before and after the gate opens. The responses that
 * decide the late layout are stubbed, so the outcome does not depend on what
 * the shard's tenant holds: the stubs describe the layout that used to shift.
 *
 * Navigation is a sidebar click from the dashboard — the client-side route
 * change the operator was making — not a `page.goto`.
 */

type Hold = { release: () => void };

/**
 * Hold every request whose pathname is `pathname` until `release()`, then
 * answer with `body` (or let it through to the backend when `body` is
 * omitted). Matched on the exact pathname — a glob would also catch the
 * app's own `src/lib/api/*.ts` modules under `vite dev`
 * (tests-e2e/README.md § Stubbing an API route).
 */
async function hold(page: Page, pathname: string, body?: unknown): Promise<Hold> {
	let release!: () => void;
	const gate = new Promise<void>((resolve) => (release = resolve));
	await page.route(
		(url) => url.pathname === pathname,
		async (route) => {
			await gate;
			if (body === undefined) await route.fallback();
			else
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(body)
				});
		}
	);
	return { release };
}

async function openFromSidebar(page: Page, href: string) {
	await page.goto('/');
	const link = page.locator(`aside.sidebar a[href="${href}"]`).first();
	await expect(link).toBeVisible();
	await link.click();
	await expect(page).toHaveURL((url) => url.pathname === href);
}

async function top(locator: Locator): Promise<number> {
	const box = await locator.boundingBox();
	expect(box, 'the anchor element must be laid out').not.toBeNull();
	return box!.y;
}

test.describe('list pages hold their layout while data loads', () => {
	test('/invoices: the table does not drop when the workflow and chip tallies land', async ({
		page
	}) => {
		const holds = [
			// An approval step adds the Ready for Review + Approved chips …
			await hold(page, '/api/workflows/active/steps', {
				extraction: true,
				approval: true,
				erp_export: true,
				approval_config: null
			}),
			// … and four-digit tallies widen every chip.
			await hold(page, '/api/invoices/counts', {
				counts: { new: 1234, ready_for_review: 2345, approved: 3456, failed: 4567 },
				total: 11602
			}),
			await hold(page, '/api/invoices')
		];

		await openFromSidebar(page, '/invoices');
		const table = page.locator('table').first();
		await expect(table).toBeVisible();
		await expect(page.locator('nav.filters').getByRole('button', { name: /^New/ })).toBeVisible();
		const before = await top(table);

		for (const h of holds) h.release();
		await expect(
			page.locator('nav.filters').getByRole('button', { name: /Ready for Review\s*2,?345/ })
		).toBeVisible();

		expect(await top(table)).toBe(before);
	});

	test('/payments: the tabs and table do not drop when the summary lands', async ({ page }) => {
		const summary = await hold(page, '/api/payments/summary', {
			total_paid: '123456.78',
			total_pending: '9876.54',
			payment_count: 42,
			total_rebates: '0',
			queue_count: 7,
			currency: 'USD',
			unconverted_payment_count: 0
		});

		await openFromSidebar(page, '/payments');
		const tabs = page.getByRole('tablist', { name: 'Payments' });
		await expect(tabs).toBeVisible();
		const strip = page.locator('.summary-cards');
		// Drawn while pending, and says so.
		await expect(strip).toHaveAttribute('aria-busy', 'true');
		await expect(strip.locator('.scard-value').first()).toHaveText('—');
		const before = await top(tabs);

		summary.release();
		await expect(strip).not.toHaveAttribute('aria-busy', 'true');
		await expect(
			strip.locator('.scard').filter({ hasText: 'Ready to Pay' }).locator('.scard-value')
		).toHaveText('7');

		expect(await top(tabs)).toBe(before);
	});

	test('/exceptions: the table does not drop when the type roster lands', async ({ page }) => {
		const summary = await hold(
			page,
			'/api/exceptions/summary',
			exceptionSummary({
				open: 5,
				by_type: { fraud_flag: 2, duplicate: 3 },
				by_severity: { error: 5 }
			})
		);

		await openFromSidebar(page, '/exceptions');
		const typeRow = page.getByTestId('exception-type-filters');
		// The row holds its place with "All types" alone until the roster lands.
		await expect(typeRow.locator('.filter-chip')).toHaveCount(1);
		const table = page.locator('#exc-panel-queue table').first();
		await expect(table).toBeVisible();
		const before = await top(table);

		summary.release();
		await expect(typeRow.locator('.filter-chip', { hasText: 'Fraud Flag' })).toBeVisible();

		expect(await top(table)).toBe(before);
	});
});

/*
 * The layout-shift budget, table-driven over every main list page.
 *
 * The three cases above pin the specific shifts #644 fixed, with stubbed
 * bodies. This half measures what a reader sees on every list page against
 * the tenant's real data: each page is opened by a client-side route change
 * with every API response held back, then released and answered after
 * `LATENCY_MS` — a production-like round trip, not zero, because a response
 * that lands in the same frame as the first paint hides exactly the shift a
 * customer sees. A `PerformanceObserver` on `layout-shift` sums every shift
 * from the click until the network goes quiet, and the total must stay under
 * 0.1, the Core Web Vitals "good" line. Entries are summed whether or not they
 * follow input: the navigation is a synthetic `click()` from script, so no
 * entry is excused as input-driven, and a stricter sum can only catch more.
 *
 * A page whose table is `fixed` (DataTable's `fixed` prop, frontend/docs/
 * ui-patterns.md § Data tables) also has its header cells' x positions
 * compared before and after the data lands: under `table-layout: auto` the
 * columns are sized against the "Loading…" row and then re-sized against the
 * real rows, which slides every column sideways. A vertical-only shift score
 * can stay small while that happens, so it gets its own exact check.
 *
 * `LIST_PAGES` plus `NOT_LIST_PAGES` must classify every sidebar destination
 * (`NAV`, lib/nav.ts): a new page fails the guard test below until it is put
 * in one or the other, the way lib/help/pages.ts must hold a row for every
 * NAV href (content.test.ts).
 */

/** A production-like API round trip. No per-endpoint timings were recorded
 *  on 2026-10-22, so this is one uniform figure at the slow end of what a
 *  customer's browser sees from the VM — slow enough that no response lands in
 *  the first paint. */
const LATENCY_MS = 400;

/** The Core Web Vitals "good" threshold for cumulative layout shift. */
const CLS_BUDGET = 0.1;

/** A neutral page with no list of its own to start every navigation from. */
const START = '/help';

interface ListPage {
	href: string;
	/** Selector of the `fixed` DataTable whose header columns must not move. */
	fixedTable?: string;
}

const LIST_PAGES: ListPage[] = [
	{ href: '/' },
	{ href: '/invoices', fixedTable: '.workspace table' },
	{ href: '/payments', fixedTable: '.pay-queue table' },
	{ href: '/exceptions', fixedTable: '#exc-panel-queue table' },
	{ href: '/vendors', fixedTable: '.vendor-table table' },
	{ href: '/purchase-orders', fixedTable: '.po-table table' },
	{ href: '/gl-accounts', fixedTable: '.gl-table table' },
	{ href: '/credit-memos' },
	{ href: '/expenses' },
	{ href: '/contracts' },
	{ href: '/bank-reconciliation' },
	{ href: '/vendors/screening', fixedTable: '.screening-table table' },
	{ href: '/vendors/change-requests', fixedTable: '.change-table table' },
	{ href: '/requisitions', fixedTable: '.req-table table' },
	{ href: '/intake', fixedTable: '.intake-table table' },
	{ href: '/catalogs', fixedTable: '.catalog-table table' },
	{ href: '/budgets', fixedTable: '.budget-table table' },
	{ href: '/recurring', fixedTable: '.recurring-table table' },
	{ href: '/vendor-statements', fixedTable: '.recon-table table' },
	{ href: '/positive-pay', fixedTable: '.ppay-table table' },
	{ href: '/experiments', fixedTable: '.experiment-table table' }
];

/**
 * Sidebar destinations deliberately outside the budget, each with its reason.
 * "Unmeasured list page" entries are list pages the budget does not cover yet
 * (docs/followups.md § "The layout-shift budget covers twenty-one pages"): moving
 * one into `LIST_PAGES` is the whole change.
 */
const UNMEASURED = 'Unmeasured list page — see docs/followups.md';
const NOT_LIST_PAGES: Record<string, string> = {
	'/goods-receipts': UNMEASURED,
	'/discounts':
		'Unmeasured: the warning banners and the optimizer panel above the offers table mount once the dashboard lands — see docs/followups.md',
	'/audit': UNMEASURED,
	'/workflows': UNMEASURED,
	'/adaptive': UNMEASURED,
	'/tax':
		'Unmeasured: a loading placeholder stands where the box-allocation panel and the table mount once the report lands — see docs/followups.md',
	'/admin?tab=users': UNMEASURED,
	'/admin?tab=roles': UNMEASURED,
	'/admin/entities': UNMEASURED,
	'/admin/partner': UNMEASURED,
	'/admin/api-keys': UNMEASURED,
	'/admin/webhooks': UNMEASURED,
	'/admin/access-review': UNMEASURED,
	'/assistant': 'A conversation, not a list',
	'/cfo': 'Charts and KPI cards, not a list',
	'/cash-flow': 'A planning workspace, not a list',
	'/reports': 'A report builder; its table is the output of a run',
	'/admin/retention': 'A settings form',
	'/admin/privacy': 'Request forms and a short history, not a list page',
	'/organization': 'A settings form',
	'/billing': 'A plan summary, not a list',
	'/admin/health': 'A status board of fixed rows'
};

const NAV_HREFS = [
	...new Set(NAV.flatMap((e) => (e.kind === 'link' ? [e.href] : e.children.map((c) => c.href))))
];

declare global {
	interface Window {
		__shifts: { value: number; sources: string[] }[];
		__shiftObserver?: PerformanceObserver;
	}
}

/** Collect every layout-shift entry, with the nodes it moved, from page load. */
async function observeLayoutShifts(page: Page) {
	await page.addInitScript(() => {
		window.__shifts = [];
		type Shift = PerformanceEntry & {
			value: number;
			sources?: { node?: Node | null }[];
		};
		const record = (entries: PerformanceEntryList) => {
			for (const e of entries as Shift[]) {
				window.__shifts.push({
					value: e.value,
					sources: (e.sources ?? []).map((s) => {
						const el = s.node instanceof Element ? s.node : null;
						return el ? `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}` : '#text';
					})
				});
			}
		};
		const obs = new PerformanceObserver((list) => record(list.getEntries()));
		obs.observe({ type: 'layout-shift', buffered: true });
		window.__shiftObserver = obs;
	});
}

/**
 * Hold every API response behind a gate; once released, answer each after
 * `LATENCY_MS`. Matched on a pathname under `/api/`, which never catches the
 * app's own `src/lib/api*.ts` modules under `vite dev`. `inflight()` counts
 * API requests the page has issued and not yet had answered.
 */
async function delayApi(page: Page) {
	let release!: () => void;
	const gate = new Promise<void>((resolve) => (release = resolve));
	const pending = new Set<unknown>();
	const isApi = (url: string) => new URL(url).pathname.startsWith('/api/');
	page.on('request', (r) => {
		if (isApi(r.url())) pending.add(r);
	});
	page.on('requestfinished', (r) => pending.delete(r));
	page.on('requestfailed', (r) => pending.delete(r));
	await page.route(
		(url) => url.pathname.startsWith('/api/'),
		async (route) => {
			await gate;
			await new Promise((resolve) => setTimeout(resolve, LATENCY_MS));
			await route.fallback();
		}
	);
	return { release, inflight: () => pending.size };
}

/** Two animation frames: everything the last response changed is laid out. */
async function nextFrames(page: Page) {
	await page.evaluate(
		() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))
	);
}

async function headerXs(table: Locator): Promise<number[]> {
	return table.locator('thead th').evaluateAll((ths) => ths.map((th) => th.getBoundingClientRect().x));
}

test.describe('list pages stay within the layout-shift budget', () => {
	test('every sidebar page is classified as a measured list page or not', () => {
		const measured = new Set(LIST_PAGES.map((p) => p.href));
		for (const href of NAV_HREFS) {
			expect(
				measured.has(href) || href in NOT_LIST_PAGES,
				`${href} is in the sidebar but in neither LIST_PAGES nor NOT_LIST_PAGES`
			).toBe(true);
			expect(
				measured.has(href) && href in NOT_LIST_PAGES,
				`${href} is in both LIST_PAGES and NOT_LIST_PAGES`
			).toBe(false);
		}
		for (const href of [...measured, ...Object.keys(NOT_LIST_PAGES)]) {
			expect(NAV_HREFS, `${href} is no longer a sidebar destination`).toContain(href);
		}
	});

	for (const { href, fixedTable } of LIST_PAGES) {
		test(`${href}: cumulative layout shift stays under ${CLS_BUDGET}`, async ({ page }) => {
			await observeLayoutShifts(page);
			await page.goto(START);
			await expect(page.locator('h1').first()).toBeVisible();
			const api = await delayApi(page);

			// A client-side route change, like a sidebar click, from script so
			// the shifts that follow are not excused as following user input.
			await page.evaluate((to) => {
				window.__shifts = [];
				const a = document.createElement('a');
				a.href = to;
				a.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
				document.body.appendChild(a);
				a.click();
				a.remove();
			}, href);
			await expect(page).toHaveURL((url) => url.pathname === href);
			await expect(page.locator('h1').first()).toBeVisible();

			// The page's loading state, drawn while every response is held.
			let before: number[] | undefined;
			if (fixedTable) {
				const table = page.locator(fixedTable).first();
				await expect(table.locator('thead th').first()).toBeVisible();
				before = await headerXs(table);
			}

			api.release();
			// Settled: no API request outstanding across two frames.
			await expect.poll(api.inflight, { timeout: 20_000 }).toBe(0);
			await nextFrames(page);
			await expect.poll(api.inflight, { timeout: 20_000 }).toBe(0);
			await nextFrames(page);

			if (fixedTable) {
				const table = page.locator(fixedTable).first();
				// Data, or a settled empty / error row, has replaced "Loading…".
				await expect(table.locator('tbody tr').first()).toBeVisible();
				expect(await headerXs(table), 'the header columns slid sideways').toEqual(before);
			}

			const shifts = await page.evaluate(() => {
				const late = window.__shiftObserver?.takeRecords() ?? [];
				const all = [
					...window.__shifts,
					...late.map((e) => ({ value: (e as PerformanceEntry & { value: number }).value, sources: [] }))
				];
				return all;
			});
			const cls = shifts.reduce((sum, s) => sum + s.value, 0);
			expect(
				cls,
				`layout shifts on ${href}: ${JSON.stringify(shifts.map((s) => [s.value.toFixed(3), s.sources]))}`
			).toBeLessThan(CLS_BUDGET);
		});
	}
});
