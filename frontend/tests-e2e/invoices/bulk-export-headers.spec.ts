import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	test
} from '../fixtures/helpers';

/**
 * `/invoices` bulk export must go through the shared API client.
 *
 * Regression: `bulkExport` hand-rolled its own `fetch` — reading
 * `PUBLIC_API_URL`, pulling the JWT straight out of `localStorage`, and
 * setting `X-Tenant-Slug` by hand — because `api.downloadBlob` is GET-only and
 * this endpoint is a POST that answers with a file. A hand-rolled request
 * silently loses whatever the shared client does for everyone else, and this
 * one lost two things:
 *
 *   1. `X-Entity-ID`. Every other request on the page carries it, so the list
 *      the user selected rows FROM was scoped to one subsidiary while the
 *      export of that selection was not.
 *   2. The 401 clear-and-bounce, so an expired session produced a cryptic
 *      "Export failed: 401" toast instead of a re-login.
 *
 * The fix is `api.downloadBlobPost(path, body)`, composed from the same
 * `authHeaders()` as `request` / `downloadBlob` / the SSE stream helper. This
 * spec asserts the headers actually on the wire, which is the only place the
 * two implementations were distinguishable.
 */

/** Fulfil the export POST ourselves so the assertion is about the REQUEST, and
 *  no real export runs.
 *
 *  Returns the pending promise WRAPPED in an object. Returning it bare from an
 *  `async` function is a deadlock: `await captureExportRequest(page)` unwraps
 *  the inner promise too, so the caller blocks on a request that only happens
 *  after the click it has not made yet. */
async function captureExportRequest(page: import('@playwright/test').Page) {
	let resolveHeaders: (h: Record<string, string>) => void;
	const captured = new Promise<Record<string, string>>((r) => (resolveHeaders = r));

	await page.route(
		(url) => url.pathname === '/api/invoices/bulk/export',
		async (route) => {
			resolveHeaders(route.request().headers());
			await route.fulfill({
				status: 200,
				contentType: 'text/csv',
				body: 'invoice_number,amount\nE2E-1,1.00\n'
			});
		}
	);

	return { headers: captured };
}

async function selectFirstSelectableRow(page: import('@playwright/test').Page) {
	const checkbox = page
		.locator('table tbody tr td.checkbox-col input[type="checkbox"]:not([disabled])')
		.first();
	await checkbox.check();
	await expect(page.locator('.bulk-bar')).toBeVisible();
}

test.describe('/invoices bulk export request headers', () => {
	test('carries Authorization + X-Tenant-Slug through the shared client', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const capture = await captureExportRequest(page);
		await selectFirstSelectableRow(page);
		await page.locator('.bulk-bar').getByRole('button', { name: 'CSV' }).click();

		const headers = await capture.headers;
		expect(headers['authorization']).toMatch(/^Bearer .+/);
		expect(headers['x-tenant-slug']).toBe(currentTenantSlug());
		expect(headers['content-type']).toContain('application/json');
	});

	test('carries X-Entity-ID when the page is scoped to a subsidiary', async ({ page }) => {
		// Resolve a real entity for this tenant — the header is only sent when a
		// specific entity is selected (absent = consolidated, per `#lib/entity.ts`).
		const res = await page.request.get(`${API_BASE}/api/entities`, {
			headers: await authedTenantHeaders(page)
		});
		expect(res.ok()).toBeTruthy();
		const entities = (await res.json()) as Array<{ id: string; is_default: boolean }>;
		const entity = entities.find((e) => e.is_default) ?? entities[0];
		expect(entity, 'every tenant is seeded with a default entity').toBeTruthy();

		const slug = currentTenantSlug();
		await page.addInitScript(
			([key, value]) => localStorage.setItem(key, value),
			[`selected_entity_id:${slug}`, entity!.id] as const
		);

		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const capture = await captureExportRequest(page);
		await selectFirstSelectableRow(page);
		await page.locator('.bulk-bar').getByRole('button', { name: 'CSV' }).click();

		const headers = await capture.headers;
		// The header the hand-rolled fetch omitted. Without it the export is
		// consolidated while the list it came from is scoped.
		expect(headers['x-entity-id']).toBe(entity!.id);
	});

	test('a 401 on export clears the session and bounces to /login', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		await page.route(
			(url) => url.pathname === '/api/invoices/bulk/export',
			(route) =>
				route.fulfill({
					status: 401,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'Unauthorized' })
				})
		);

		await selectFirstSelectableRow(page);
		await page.locator('.bulk-bar').getByRole('button', { name: 'CSV' }).click();

		// The shared client's 401 handling: token cleared, redirected to login.
		await page.waitForURL(/\/login/, { timeout: 15_000 });
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
	});
});
