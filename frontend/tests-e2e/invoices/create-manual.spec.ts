import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/** Every invoice number this file creates starts here — the prefix `afterEach`
 *  cleans up. */
const INVOICE_MARKER = 'E2E-MANUAL-';
/** The vendor names typed into the modal. NOT deleted in teardown — see the
 *  note on the describe block. */
const VENDOR_NAMES = ['E2E Manual Vendor', 'E2E File Vendor'];

interface OrgResponse {
	settings: Record<string, unknown> & {
		invoice_defaults?: { currency?: string };
	};
}

async function getOrg(page: import('@playwright/test').Page): Promise<OrgResponse> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page)
	});
	return (await resp.json()) as OrgResponse;
}

async function patchOrg(
	page: import('@playwright/test').Page,
	body: Record<string, unknown>
): Promise<void> {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: body
	});
}

/**
 * Manual invoice entry — "+ Create Invoice" on the /invoices toolbar opens
 * `CreateInvoiceModal`, which POSTs the keyed-in fields to the pre-existing
 * `POST /api/invoices` (creates at status `new`, no extraction) and, if a
 * file was picked, follows up with `POST /api/invoices/{id}/file` (new
 * attach-only endpoint — refuses once the invoice already has a file).
 *
 * Roadmap item "Manual Invoice Entry (No-OCR Creation)" in
 * docs/roadmap.md § Priority 1.
 */

test.describe('/invoices — Create Invoice modal', () => {
	/**
	 * The worker's `e2e<N>` tenant is shared with every other spec in the run
	 * AND with every previous run, so a spec that creates rows and leaves them
	 * is not merely untidy — it changes what the next spec counts and pages
	 * through. This file used to leave two invoices per run: the numbers carry
	 * `Date.now()`, so they never collide and never stopped accumulating.
	 *
	 * `deleteInvoicesWhere` owns their 16-FK child graph. The predicate is the
	 * prefix rather than the ids created here, so a run also clears whatever
	 * earlier runs stranded.
	 *
	 * **The vendors are deliberately NOT deleted.** Manual entry provisions one
	 * as a side effect — an unmatched name goes through
	 * `vendor_matching.match_and_link_vendor`, which creates an `unverified`
	 * vendor stamped `source=manual` so the invoice carries a provable payee —
	 * but that row is not this file's to remove. The matcher's third leg is a
	 * normalized-name Jaccard, so it LINKS other specs' invoices to it too:
	 * `invoices/file-management.spec.ts` types "E2E File Mgmt Vendor" and lands
	 * on the "E2E File Vendor" row this file caused to exist. Deleting it here
	 * failed the FK on that spec's invoice — loudly, which is how this was
	 * found. Unlike the invoices, the vendor is also BOUNDED: one row per
	 * distinct name, re-matched rather than re-created on every later run, so
	 * it is a shared fixture rather than a leak.
	 */
	test.afterEach(() => {
		deleteInvoicesWhere(`invoice_number LIKE '${INVOICE_MARKER}%'`);
	});

	test('admin sees the toolbar button; required fields gate submit', async ({ page }) => {
		await page.goto('/invoices');

		const button = page.getByRole('button', { name: 'Create Invoice' });
		await expect(button).toBeVisible();
		await button.click();

		const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
		await expect(modal).toBeVisible();

		const submitBtn = modal.getByRole('button', { name: 'Create' });
		await expect(submitBtn).toBeDisabled();

		await modal.locator('label', { hasText: 'Vendor' }).locator('input').fill(VENDOR_NAMES[0]);
		await expect(submitBtn).toBeDisabled();
		await modal
			.locator('label', { hasText: 'Invoice #' })
			.locator('input')
			.fill(`${INVOICE_MARKER}1`);
		await expect(submitBtn).toBeDisabled();
		await modal.locator('label', { hasText: 'Amount' }).locator('input').fill('42.50');
		await expect(submitBtn).toBeEnabled();

		await modal.getByRole('button', { name: 'Cancel' }).click();
		await expect(modal).not.toBeVisible();
	});

	test('creating without a file lands the invoice at New in the list', async ({ page }) => {
		await page.goto('/invoices');
		const uniqueNumber = `${INVOICE_MARKER}${Date.now()}`;

		await page.getByRole('button', { name: 'Create Invoice' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
		await expect(modal).toBeVisible();

		await modal.locator('label', { hasText: 'Vendor' }).locator('input').fill(VENDOR_NAMES[0]);
		await modal.locator('label', { hasText: 'Invoice #' }).locator('input').fill(uniqueNumber);
		await modal.locator('label', { hasText: 'Amount' }).locator('input').fill('42.50');
		await modal.getByRole('button', { name: 'Create' }).click();

		await expect(modal).not.toBeVisible({ timeout: 10_000 });

		const row = page.locator('table tbody tr', { hasText: uniqueNumber });
		await expect(row).toBeVisible({ timeout: 10_000 });
		await expect(row).toContainText('New');
	});

	test('creating with a file attaches it — Activity timeline shows the attach event', async ({
		page
	}) => {
		await page.goto('/invoices');
		const uniqueNumber = `${INVOICE_MARKER}FILE-${Date.now()}`;

		await page.getByRole('button', { name: 'Create Invoice' }).click();
		const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
		await expect(modal).toBeVisible();

		await modal.locator('label', { hasText: 'Vendor' }).locator('input').fill(VENDOR_NAMES[1]);
		await modal.locator('label', { hasText: 'Invoice #' }).locator('input').fill(uniqueNumber);
		await modal.locator('label', { hasText: 'Amount' }).locator('input').fill('99.00');
		await modal
			.locator('input[type="file"]')
			.setInputFiles({ name: 'invoice.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 fake') });
		await modal.getByRole('button', { name: 'Create' }).click();

		await expect(modal).not.toBeVisible({ timeout: 10_000 });

		const row = page.locator('table tbody tr', { hasText: uniqueNumber });
		await expect(row).toBeVisible({ timeout: 10_000 });
		await row.getByRole('button', { name: 'Edit' }).click();

		const detail = page.locator('div.modal[role="dialog"]', { hasText: uniqueNumber });
		await expect(detail).toBeVisible();
		await expect(detail.locator('.activity-action', { hasText: 'File attached' })).toBeVisible({
			timeout: 5_000
		});
	});

	test('currency field defaults to the org-configured default, not a hardcoded USD', async ({
		page
	}) => {
		// `authedTenantHeaders` reads the JWT out of localStorage, which only
		// exists once the page has visited the tenant origin (storageState
		// applies at context creation, but localStorage is only readable once
		// we're actually on that origin).
		await page.goto('/invoices');

		const before = await getOrg(page);
		const originalCurrency = before.settings.invoice_defaults?.currency ?? 'USD';
		// Pick a non-USD, non-current currency so a hardcoded 'USD' default
		// (the bug) is unambiguously distinguishable from the fix.
		const next = originalCurrency === 'EUR' ? 'GBP' : 'EUR';

		try {
			await patchOrg(page, {
				settings: {
					invoice_defaults: {
						...(before.settings.invoice_defaults ?? {}),
						currency: next
					}
				}
			});

			// Reload so the page's ensureLoaded() effect resolves the org
			// currency from scratch (the store is session-cached) before the
			// modal reads it at construction time. The modal reads the currency
			// AT CONSTRUCTION, so a retrying assertion below cannot recover from
			// clicking too early — wait on the org fetch itself (registered
			// before the navigation that triggers it), not on a quiet network.
			const orgLoaded = page.waitForResponse(
				(r) => r.url().includes('/api/organization') && r.request().method() === 'GET'
			);
			await page.goto('/invoices');
			await orgLoaded;

			await page.getByRole('button', { name: 'Create Invoice' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
			await expect(modal).toBeVisible();

			const currencyInput = modal.locator('label', { hasText: 'Currency' }).locator('input');
			await expect(currencyInput).toHaveValue(next);

			await modal.getByRole('button', { name: 'Cancel' }).click();
		} finally {
			await patchOrg(page, {
				settings: {
					invoice_defaults: {
						...(before.settings.invoice_defaults ?? {}),
						currency: originalCurrency
					}
				}
			});
		}
	});

	test('ap_clerk sees the toolbar button (entry is a clerk duty)', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		// `POST /api/invoices` takes every entry role, ap_clerk included
		// (`backend/app/api/invoice_entry.py`). The toolbar gates on
		// `auth.hasAnyRole(...)`, populated once GET /api/auth/me lands, so wait
		// on the identity fetch itself rather than on a quiet network.
		const me = page.waitForResponse(
			(r) => r.url().includes('/api/auth/me') && r.request().method() === 'GET'
		);
		await page.goto('/invoices');
		await me;
		await expect(page.getByRole('button', { name: 'Create Invoice' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Upload' })).toBeVisible();
	});
});
