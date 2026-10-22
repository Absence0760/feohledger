import type { Locator, Page } from '@playwright/test';

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
