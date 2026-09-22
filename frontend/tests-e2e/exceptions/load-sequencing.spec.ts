import { expect, test } from '../fixtures/helpers';
import { resolveDialog } from './dialogs';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — list-load sequencing.
 *
 * The queue had no `createRequestSequencer`, so responses were committed in
 * arrival order rather than issue order. Click Load more, then change the
 * status chip: the chip's page-1 replace lands first, then the still-in-flight
 * append pushes the OLD filter's page-2 rows onto the new list and overwrites
 * `total`/`page` with them — a resolved exception reappearing under "Open" in
 * a financial-control queue, with a footer count that matches neither list.
 *
 * The summary is a second, independent request stream (it drives the chip
 * counts), so it gets its OWN sequencer — asserted here by an out-of-order
 * summary pair.
 *
 * Both endpoints are stubbed so the interleaving is exact and the spec never
 * depends on what the shard's tenant happens to hold.
 */

function exceptionRow(n: number, status: 'open' | 'escalated' = 'open') {
	return {
		id: `00000000-0000-4000-9100-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-EXC-${n}`,
		vendor_name: 'Sequencer Vendor',
		amount: 100,
		exception_type: 'duplicate',
		type_label: 'Duplicate',
		severity: 'warning',
		description: 'stubbed',
		status,
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

const SUMMARY = exceptionSummary({ open: 2, escalated: 1 });

test.describe('/exceptions — list request sequencing', () => {
	test('a held page-2 append cannot clobber a newer status-filtered page 1', async ({ page }) => {
		await page.route('**/api/exceptions/summary*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(SUMMARY)
			})
		);

		let releaseAppend: () => void = () => {};
		const appendGate = new Promise<void>((resolve) => (releaseAppend = resolve));

		await page.route('**/api/exceptions?*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/exceptions') {
				// `fallback`, not `continue`: Playwright checks handlers newest-first,
				// so deferring here is what lets the summary stub registered ABOVE
				// answer its own URL instead of this one sending it to the network.
				await route.fallback();
				return;
			}
			const status = url.searchParams.get('status');
			if (url.searchParams.get('page') === '2') {
				// The load-more append: held until the newer filter load has landed.
				await appendGate;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ items: [exceptionRow(2)], total: 2 })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body:
					status === 'escalated'
						? JSON.stringify({ items: [exceptionRow(3, 'escalated')], total: 1 })
						: JSON.stringify({ items: [exceptionRow(1)], total: 2 })
			});
		});

		await page.goto('/exceptions');
		await expect(page.getByText('E2E-EXC-1')).toBeVisible();

		const loadMore = page.getByRole('button', { name: /Load more/ });
		await expect(loadMore).toBeVisible();
		await loadMore.click();

		// Switch the status chip while the append is still out.
		await page.getByRole('button', { name: /^Escalated/ }).click();
		await expect(page.getByText('E2E-EXC-3')).toBeVisible();

		// Release the stale append and wait for the page to actually receive it —
		// a real signal, not a sleep.
		const staleResponse = page.waitForResponse(
			(r) =>
				r.request().method() === 'GET' &&
				new URL(r.url()).pathname === '/api/exceptions' &&
				new URL(r.url()).searchParams.get('page') === '2'
		);
		releaseAppend();
		await staleResponse;
		// One animation frame past the response guarantees the fetch continuation
		// (and any state write it would have made) has run.
		await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));

		await expect(page.getByText('E2E-EXC-2')).toHaveCount(0);
		await expect(page.getByText('E2E-EXC-1')).toHaveCount(0);
		await expect(page.getByText('E2E-EXC-3')).toBeVisible();
		// `total` is the new filter's 1, not the 2 the stale append carried.
		await expect(page.getByText('Showing all 1 exception')).toBeVisible();
	});

	test('a held summary response cannot relabel the chips after a newer one', async ({ page }) => {
		// The chip counts are a SECOND request stream, so they get their own
		// sequencer: a resolve refreshes the queue and the summary together, and
		// the mount summary still being out means the pre-resolve tallies can
		// land last and relabel the chips with counts nobody can reconcile
		// against the rows.
		let summaryRequests = 0;
		let releaseFirstSummary: () => void = () => {};
		const summaryGate = new Promise<void>((resolve) => (releaseFirstSummary = resolve));

		await page.route('**/api/exceptions/summary*', async (route) => {
			summaryRequests += 1;
			if (summaryRequests === 1) {
				// The mount read: held past the resolve that supersedes it.
				await summaryGate;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ ...SUMMARY, open: 9 })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ...SUMMARY, open: 1 })
			});
		});

		await page.route('**/api/exceptions?*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/exceptions') {
				// `fallback`, not `continue`: Playwright checks handlers newest-first,
				// so deferring here is what lets the summary stub registered ABOVE
				// answer its own URL instead of this one sending it to the network.
				await route.fallback();
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [exceptionRow(1)], total: 1 })
			});
		});

		await page.route('**/api/exceptions/*/resolve', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ ...exceptionRow(1), status: 'resolved' })
			})
		);

		await page.goto('/exceptions');
		// The row renders while the summary is still out (only the chips wait on
		// it), so the resolve below is reachable.
		const row = page.getByRole('row').filter({ hasText: 'E2E-EXC-1' });
		await expect(row).toBeVisible();

		await row.getByRole('button', { name: 'Resolve' }).click();
		const modal = resolveDialog(page);
		await modal.locator('input[type="text"]').fill('e2e: sequencer guard');
		await modal.getByRole('button', { name: 'Resolve', exact: true }).click();

		// The resolve's own summary refresh lands and paints the chips.
		const openChip = page.getByRole('button', { name: /^Open/ });
		await expect(openChip).toContainText('1');

		const staleSummary = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/exceptions/summary'
		);
		releaseFirstSummary();
		await staleSummary;
		await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));

		// The stale "9 open" must not repaint the chip.
		await expect(openChip).toContainText('1');
		await expect(openChip).not.toContainText('9');
	});
});
