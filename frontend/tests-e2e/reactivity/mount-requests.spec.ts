import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * Opening a list page loads each list ONCE.
 *
 * Measured on a production build while chasing the "glitch whenever I change
 * pages" report: four pages sent their list request twice on every visit.
 * /invoices, /vendors and /bank-reconciliation did it 300ms after mount — the
 * debounced search `$effect` ran its mount-time pass beside the effect that
 * had already loaded the list (on /invoices that re-flipped the loading state
 * the page draws, under a reader looking at the finished table). /payments did
 * it at once — a mount effect and the tab effect both loaded the queue.
 *
 * The search half also has a static guard (`effectTimerCleanup.test.ts`); this
 * spec is the behavioural one, and the only one that covers the /payments
 * queue.
 *
 * Listeners, not stubs, matched on the exact pathname (under `vite dev` the
 * app's own `src/lib/api/*.ts` modules are requests too —
 * tests-e2e/README.md § Stubbing an API route).
 */

function countRequests(page: Page, pathname: string): string[] {
	const seen: string[] = [];
	page.on('request', (request) => {
		const url = new URL(request.url());
		if (request.method() === 'GET' && url.pathname === pathname) seen.push(url.search);
	});
	return seen;
}

const CASES = [
	{ route: '/invoices', pathnames: ['/api/invoices', '/api/invoices/counts'], heading: 'Invoices' },
	{ route: '/vendors', pathnames: ['/api/vendors'], heading: 'Vendors' },
	{ route: '/payments', pathnames: ['/api/payments/queue'], heading: 'Payments' },
	{
		route: '/bank-reconciliation',
		pathnames: ['/api/bank-reconciliation/outstanding'],
		heading: 'Bank Reconciliation'
	}
] as const;

test.describe('list pages load their list once on mount', () => {
	for (const c of CASES) {
		test(`${c.route} requests ${c.pathnames.join(' + ')} exactly once`, async ({ page }) => {
			const seen = c.pathnames.map((p) => [p, countRequests(page, p)] as const);

			await page.goto(c.route);
			await expect(page.getByRole('heading', { level: 1, name: c.heading })).toBeVisible();
			for (const [p] of seen) {
				await expect.poll(() => seen.find(([q]) => q === p)![1].length, { message: p }).toBeGreaterThan(0);
			}
			// The claim is "no SECOND request", so the wait is the absence-of-further-
			// traffic signal, not a readiness stand-in — the same honest use
			// `credit-memos/load-sequencing.spec.ts` documents. A quiet 500ms window
			// covers the 300ms debounce that used to fire the duplicate; a
			// `waitForResponse` proves one fired, never that a second did not.
			await page.waitForLoadState('networkidle');

			for (const [p, requests] of seen) {
				expect(requests, `${c.route} sent ${requests.length} ${p} requests: ${requests.join(' | ')}`).toHaveLength(1);
			}
		});
	}
});
