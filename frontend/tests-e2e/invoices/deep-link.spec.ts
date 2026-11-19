import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import type { Route } from '@playwright/test';

/**
 * /invoices?id=<uuid> deep-link.
 *
 * The exceptions queue (and any future caller) links a row to
 * `/invoices?id=<invoice_id>` expecting the invoice's detail modal to
 * open. Before the fix the invoices page ignored the `id` param entirely,
 * stranding the user on the unfiltered list — a dead-end deep-link.
 *
 * These tests lock the behaviour: the param opens the right invoice's
 * modal (even when it lives past the first page of results), closing the
 * modal scrubs `id` from the URL, and a bad id fails gracefully without a
 * stuck modal.
 */

test.describe('/invoices?id deep-link', () => {
	test('opens the detail modal for the linked invoice on load', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as {
			items: Array<{ id: string; invoice_number: string }>;
		};
		const target = listed.items[0];
		expect(target).toBeTruthy();

		await page.goto(`/invoices?id=${target.id}`);

		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();
		await expect(modal.locator('header h2')).toContainText(target.invoice_number);
	});

	test('closing the modal scrubs id from the URL', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as { items: Array<{ id: string }> };
		const target = listed.items[0];
		expect(target).toBeTruthy();

		await page.goto(`/invoices?id=${target.id}`);
		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();

		await modal.getByRole('button', { name: 'Close' }).click();
		await expect(modal).toBeHidden();
		await expect(page).toHaveURL(/\/invoices$/);
		// ...and it STAYS closed. The deep-link effect re-arms off the scrubbed
		// URL; when the close handler cleared its marker itself, an effect run
		// that landed before the `$page` store caught up read the still-present
		// `id` against a null marker and re-opened the modal the user had just
		// dismissed. Re-asserting after the URL has settled covers that window
		// without a sleep.
		await expect(modal).toBeHidden();
	});

	test('a non-existent id does not strand a stuck modal', async ({ page }) => {
		await page.goto('/invoices?id=00000000-0000-0000-0000-000000000000');
		// The list still renders; no detail modal hangs around.
		await expect(page.locator('table')).toBeVisible();
		await expect(
			page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]')
		).toHaveCount(0);
	});
	test('a 404 says not found, and no Retry is offered', async ({ page }) => {
		await page.goto('/invoices?id=00000000-0000-0000-0000-000000000000');
		await expect(page.locator('.toast-text', { hasText: 'Invoice not found' })).toBeVisible();
		await expect(page.getByTestId('invoices-deeplink-error')).toHaveCount(0);
	});

	test('a 404 drops id from the URL, keeping the filters, so a reload does not re-toast', async ({
		page
	}) => {
		await page.goto('/invoices?status=new&id=00000000-0000-0000-0000-000000000000');
		await expect(page.locator('.toast-text', { hasText: 'Invoice not found' })).toBeVisible();
		// The dead id goes; the filter the link carried stays.
		await expect(page).toHaveURL(/\/invoices\?status=new$/);

		// Wait on the reloaded list itself, so the no-toast check below runs
		// after the page has done everything a reload does.
		const listLoaded = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/invoices' && r.request().method() === 'GET'
		);
		await page.reload();
		await listLoaded;
		await expect(page.locator('.toast-text', { hasText: 'Invoice not found' })).toHaveCount(0);
	});

	test('a server failure is a failed load with Retry, not "not found"', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as {
			items: Array<{ id: string; invoice_number: string }>;
		};
		const target = listed.items[0];
		expect(target).toBeTruthy();

		// Fail only the FIRST detail GET, so Retry reaches the real backend.
		let failed = false;
		await page.route(
			(url) => url.pathname === `/api/invoices/${target.id}`,
			async (route: Route) => {
				if (route.request().method() !== 'GET' || failed) return route.fallback();
				failed = true;
				return route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
			}
		);

		await page.goto(`/invoices?id=${target.id}`);
		const loadError = page.getByTestId('invoices-deeplink-error');
		await expect(loadError).toBeVisible();
		await expect(loadError).toHaveAttribute('role', 'alert');
		await expect(page.locator('.toast-text', { hasText: 'Invoice not found' })).toHaveCount(0);

		await loadError.getByRole('button').click();
		await expect(loadError).toHaveCount(0);
		const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
		await expect(modal).toBeVisible();
		await expect(modal.locator('header h2')).toContainText(target.invoice_number);
	});
});
