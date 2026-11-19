import type { Catalog, CatalogListResponse } from '#lib/types/catalog.ts';

import { expect, test } from '../fixtures/helpers';

/**
 * /catalogs — loading / failed / genuinely empty are three different states.
 *
 * `isEmpty` was gated on `!loading`, which inverts what the flag is for: during
 * the first fetch the table rendered its header with NOTHING under it — no
 * rows, no spinner, no message — and a failed load, reported only by a toast
 * that then faded, left "No catalogs." standing forever with no way to retry.
 *
 * Same three-state rule `/exceptions` and the payments queue follow, and the
 * same error-with-retry block `/admin/api-keys` uses.
 */

test.describe('/catalogs load states', () => {
	test('a SLOW load shows the loading state, not a bare header', async ({ page }) => {
		// A real readiness gate, not a sleep: the response is released by
		// resolving this promise, so nothing is timing-dependent.
		let release!: () => void;
		const held = new Promise<void>((resolve) => {
			release = resolve;
		});
		await page.route('**/api/catalogs?**', async (route) => {
			await held;
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 })
			});
		});

		await page.goto('/catalogs');

		const empty = page.getByTestId('table-empty');
		await expect(empty).toBeVisible({ timeout: 10_000 });
		await expect(empty).toHaveText('Loading…');

		release();

		await expect(empty).toHaveText('No catalogs.', { timeout: 10_000 });
	});

	test('a FAILED load offers a retry — never "No catalogs."', async ({ page }) => {
		let attempts = 0;
		await page.route('**/api/catalogs?**', async (route) => {
			attempts += 1;
			if (attempts === 1) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 })
			});
		});

		await page.goto('/catalogs');

		const error = page.getByTestId('catalogs-error');
		await expect(error).toBeVisible({ timeout: 10_000 });
		await expect(error).toContainText('Could not load catalogs.');
		// The two must never be on screen together.
		await expect(page.getByTestId('table-empty')).toHaveCount(0);

		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(page.getByTestId('table-empty')).toHaveText('No catalogs.', { timeout: 10_000 });
		expect(attempts).toBeGreaterThan(1);
	});

	/**
	 * A failed "Load more" is not a failed list. It used to set the same flag a
	 * failed first page sets, so LoadError replaced the table and every row
	 * already on screen was thrown away. The rows stay; the failure toasts.
	 */
	test('a failed LOAD MORE keeps the rows already loaded', async ({ page }) => {
		const row = (i: number): Catalog => ({
			id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
			name: `Paged Catalog ${i}`,
			catalog_type: 'internal',
			vendor_id: null,
			vendor_name: null,
			punchout_url: null,
			is_active: true,
			is_preferred: false,
			description: null,
			item_count: 0,
			items: [],
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:00Z'
		});
		await page.route('**/api/catalogs?**', async (route) => {
			const pageNum = new URL(route.request().url()).searchParams.get('page');
			if (pageNum === '2') {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items: [row(1), row(2)],
					total: 5,
					page: 1,
					page_size: 100
				} satisfies CatalogListResponse)
			});
		});

		await page.goto('/catalogs');
		await expect(page.getByText('Paged Catalog 1')).toBeVisible();

		await page.getByRole('button', { name: /Load more/ }).click();

		await expect(page.getByText('Could not load catalogs').first()).toBeVisible();
		await expect(page.getByTestId('catalogs-error')).toHaveCount(0);
		await expect(page.getByText('Paged Catalog 1')).toBeVisible();
		await expect(page.getByText('Paged Catalog 2')).toBeVisible();
	});

	/** Search and type filter round-trip through the URL, like every sibling list. */
	test('search and type filter survive a reload via the URL', async ({ page }) => {
		const seen: URL[] = [];
		await page.route('**/api/catalogs?**', async (route) => {
			seen.push(new URL(route.request().url()));
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 })
			});
		});

		await page.goto('/catalogs');
		await expect(page.getByTestId('table-empty')).toHaveText('No catalogs.');

		await page.getByRole('button', { name: 'Punch-out', exact: true }).click();
		await expect(page).toHaveURL(/[?&]type=punchout/);
		await page.getByRole('textbox', { name: 'Search catalogs' }).fill('paper');
		await expect(page).toHaveURL(/[?&]search=paper/);

		seen.length = 0;
		await page.reload();
		await expect(page.getByRole('textbox', { name: 'Search catalogs' })).toHaveValue('paper');
		await expect(page.getByRole('button', { name: 'Punch-out', exact: true })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
		await expect.poll(() => seen.length).toBeGreaterThan(0);
		expect(seen[0].searchParams.get('search')).toBe('paper');
		expect(seen[0].searchParams.get('catalog_type')).toBe('punchout');
	});
});
