import { expect, test } from '../fixtures/helpers';

/**
 * `/credit-memos` — list-load sequencing + the loading state.
 *
 * Three defects, one loader:
 *
 * 1. TWO unsequenced page-1 requests fired on mount. `$effect(() => loadAll())`
 *    awaited `loadMemos()`, and `$effect(() => { statusFilter; loadMemos(); })`
 *    fired its own mount-time run — whichever landed last won. Worse, the first
 *    effect read `statusFilter` *transitively* through `loadMemos` (Svelte
 *    tracks reads through called functions), so it subscribed to the filter too
 *    and every chip click re-ran BOTH — two racing page-1 requests per click,
 *    plus a pointless refetch of the vendor and invoice selects.
 * 2. `loadMemos` never touched `loading`, so a status-chip change sat on the
 *    previous filter's rows — or, from empty, kept asserting "No credit memos."
 *    — with no spinner until the response landed.
 * 3. No `createRequestSequencer`, so a page-2 append could land after a newer
 *    page-1 replace and push the OLD filter's rows onto the new list, taking
 *    `total`/`page` with them.
 *
 * The whole `/api/credit-memos` surface is stubbed so the interleaving is exact
 * and the spec never depends on what the shard's tenant happens to hold.
 */

function memo(n: number, status: 'open' | 'applied' | 'void' = 'open') {
	return {
		id: `00000000-0000-4000-b000-${String(n).padStart(12, '0')}`,
		memo_number: `E2E-CM-${n}`,
		vendor_id: '00000000-0000-4000-b001-000000000001',
		vendor_name: 'Sequencer Vendor',
		invoice_id: null,
		invoice_number: null,
		amount: 25,
		currency: 'USD',
		issued_date: '2026-01-01',
		reason: null,
		status,
		applied_at: null,
		applied_by: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

/** Stub the two selects the page loads alongside the memo list. */
async function stubSelects(page: import('@playwright/test').Page) {
	await page.route('**/api/vendors*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ items: [], total: 0 })
		})
	);
	await page.route('**/api/invoices*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ items: [], total: 0 })
		})
	);
}

test.describe('/credit-memos — list request sequencing', () => {
	test('mount issues exactly one page-1 list request', async ({ page }) => {
		await stubSelects(page);

		const listUrls: string[] = [];
		await page.route('**/api/credit-memos*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/credit-memos') {
				// `fallback`, not `continue`: defer to any handler registered before
				// this one rather than sending the request straight to the network.
				await route.fallback();
				return;
			}
			listUrls.push(url.search);
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [memo(1)], total: 1 })
			});
		});

		await page.goto('/credit-memos');
		await expect(page.getByText('E2E-CM-1')).toBeVisible();
		// `networkidle` STAYS in this file, and only in this file. Everywhere else
		// in the suite it stood in for "the page is ready", which the next
		// auto-waiting assertion already establishes — but the assertion here is
		// that NO SECOND request was issued, and a quiet network is the only
		// honest signal for "nothing more is coming". A `waitForResponse` proves
		// at least one fired, never that a duplicate did not; deleting the wait
		// would let the test pass because the duplicate had not landed yet, which
		// is a false negative on the very defect it guards.
		await page.waitForLoadState('networkidle');

		expect(listUrls, `mount fired ${listUrls.length} list requests: ${listUrls.join(', ')}`)
			.toHaveLength(1);

		// A status chip is one request too — not one per subscribed effect.
		listUrls.length = 0;
		await page.getByRole('button', { name: /^Applied/ }).click();
		// Same reason as above: the claim is "exactly one", so the wait is the
		// absence-of-further-traffic signal, not a readiness stand-in.
		await page.waitForLoadState('networkidle');
		expect(listUrls, `the chip fired ${listUrls.length} requests: ${listUrls.join(', ')}`)
			.toHaveLength(1);
		expect(listUrls[0]).toContain('status=applied');
	});

	test('a status change shows the loading state instead of claiming the list is empty', async ({
		page
	}) => {
		await stubSelects(page);

		let releaseSecond: () => void = () => {};
		const secondGate = new Promise<void>((resolve) => (releaseSecond = resolve));
		let listRequests = 0;

		await page.route('**/api/credit-memos*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/credit-memos') {
				// `fallback`, not `continue`: defer to any handler registered before
				// this one rather than sending the request straight to the network.
				await route.fallback();
				return;
			}
			listRequests += 1;
			if (listRequests > 1) await secondGate;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0 })
			});
		});

		await page.goto('/credit-memos');
		// Zero memos and no active filter is the genuine zero-data case, which
		// renders the onboarding block rather than a table row.
		await expect(page.getByTestId('credit-memos-empty-state')).toBeVisible();

		// Switch filter — the answer is unknown until the held response lands, so
		// the table must say so rather than repeat the previous verdict.
		await page.getByRole('button', { name: /^Applied/ }).click();
		const empty = page.getByTestId('table-empty');
		await expect(empty).toHaveText('Loading…');

		releaseSecond();
		// And once it lands: "the Applied filter matched nothing", never the
		// global "there are no credit memos" claim.
		await expect(empty).toHaveText('No credit memos match this filter.');
	});

	test('a held page-2 append cannot clobber a newer status-filtered page 1', async ({ page }) => {
		await stubSelects(page);

		let releaseAppend: () => void = () => {};
		const appendGate = new Promise<void>((resolve) => (releaseAppend = resolve));

		await page.route('**/api/credit-memos*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/credit-memos') {
				// `fallback`, not `continue`: defer to any handler registered before
				// this one rather than sending the request straight to the network.
				await route.fallback();
				return;
			}
			const status = url.searchParams.get('status');
			const pageParam = url.searchParams.get('page');

			if (pageParam === '2') {
				// The load-more append: held until the newer filter load has landed.
				await appendGate;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ items: [memo(2)], total: 2 })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body:
					status === 'applied'
						? JSON.stringify({ items: [memo(3, 'applied')], total: 1 })
						: JSON.stringify({ items: [memo(1)], total: 2 })
			});
		});

		await page.goto('/credit-memos');
		await expect(page.getByText('E2E-CM-1')).toBeVisible();

		const loadMore = page.getByRole('button', { name: /Load more/ });
		await expect(loadMore).toBeVisible();
		await loadMore.click();

		// Switch the filter while the append is still out. Its page-1 replace
		// lands first.
		await page.getByRole('button', { name: /^Applied/ }).click();
		await expect(page.getByText('E2E-CM-3')).toBeVisible();

		// Release the stale append and wait for the page to actually receive it —
		// a real signal, not a sleep.
		const staleResponse = page.waitForResponse(
			(r) =>
				r.request().method() === 'GET' &&
				new URL(r.url()).pathname === '/api/credit-memos' &&
				new URL(r.url()).searchParams.get('page') === '2'
		);
		releaseAppend();
		await staleResponse;
		// One animation frame past the response guarantees the fetch continuation
		// (and any state write it would have made) has run: microtasks drain
		// before a frame paints.
		await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));

		// The old filter's page-2 row must not appear under the new filter, and
		// `total` must still be the new filter's 1 — not the 2 the stale response
		// carried.
		await expect(page.getByText('E2E-CM-2')).toHaveCount(0);
		await expect(page.getByText('E2E-CM-3')).toBeVisible();
		await expect(page.getByText('Showing all 1 credit memo')).toBeVisible();
	});
});
