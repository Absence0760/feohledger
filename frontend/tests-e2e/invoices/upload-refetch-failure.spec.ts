import { deleteInvoicesWhere, expect, tenantPsql, test } from '../fixtures/helpers';

/**
 * /invoices — the Upload button must recover when the post-upload REFETCH fails.
 *
 * Regression: `handleUpload` had no try/finally. The uploads themselves are
 * wrapped in `Promise.allSettled` (so a failed file can't throw), but the
 * `invoiceStore.fetch(...)` / `fetchCounts()` calls that follow are bare awaits.
 * A rejection there skipped `uploading = false` and `input.value = ''`, leaving
 * the button permanently disabled reading "Uploading…" — and because the file
 * input still held the same value, re-picking the same file wouldn't even fire
 * `change`. Only a full page reload recovered, and the user had no way to know
 * the upload had in fact succeeded.
 *
 * The list fetch is stubbed to fail exactly once, AFTER the upload lands, so the
 * spec drives the real failure rather than simulating the symptom.
 */

const PDF = {
	name: 'upload-recovery.pdf',
	mimeType: 'application/pdf',
	buffer: Buffer.from('%PDF-1.4 e2e upload recovery')
};

test.describe('/invoices upload recovery', () => {
	// The upload really happens (that is the point — the failure is downstream of
	// it), so the row it creates is cleaned up here rather than left behind.
	let uploadedId: string | null = null;
	test.afterEach(async () => {
		if (!uploadedId) return;
		const id = uploadedId;
		// The upload really runs extraction, in the backend's own worker, which
		// keeps writing the invoice's children (workflow steps, line items, the
		// extraction result) until it lands the invoice in review or failed.
		// Deleting under it raced those inserts: a step written between the
		// helper's step and instance deletes broke the instance delete's FK. So
		// wait for the worker to finish with the invoice first.
		await expect
			.poll(() => tenantPsql(`SELECT status FROM invoices WHERE id='${id}'`).trim())
			.not.toMatch(/^(new|pending)$/);
		// `deleteInvoicesWhere` owns the full child graph rather than this spec
		// tracking a subset.
		deleteInvoicesWhere(`id='${id}'`);
		uploadedId = null;
	});

	test('a failed post-upload refetch re-enables the Upload button', async ({ page }) => {
		await page.goto('/invoices');

		const uploadBtn = page.getByRole('button', { name: '+ Upload Invoices' });
		await expect(uploadBtn).toBeEnabled();

		// Break ONLY the list refetch, and only while the upload is in flight, so
		// the failure lands exactly where the missing `finally` was.
		let breakList = false;
		await page.route(
			(url) => url.pathname === '/api/invoices',
			(route) => {
				if (breakList && route.request().method() === 'GET') {
					return route.fulfill({
						status: 500,
						contentType: 'application/json',
						body: JSON.stringify({ detail: 'refetch exploded' })
					});
				}
				return route.continue();
			}
		);

		const uploaded = page.waitForResponse(
			(r) => r.url().includes('/api/invoices/upload') && r.request().method() === 'POST'
		);
		breakList = true;
		await page.locator('input[type="file"]').setInputFiles(PDF);
		uploadedId = ((await (await uploaded).json()) as { id: string }).id;

		// The button comes back — this is the assertion the bug failed.
		await expect(uploadBtn).toBeEnabled({ timeout: 15_000 });
		await expect(uploadBtn).toHaveText('+ Upload Invoices');

		// And the failure is surfaced rather than swallowed: the upload worked,
		// the list did not refresh, and the toast says exactly that.
		await expect(page.locator('.toast.error')).toBeVisible();
		await expect(page.getByText(/list could not be refreshed/i)).toBeVisible();

		// Recovery is real, not cosmetic: a subsequent refetch succeeds.
		breakList = false;
		await page.reload();
		await expect(page.getByRole('button', { name: '+ Upload Invoices' })).toBeEnabled();
	});
});
