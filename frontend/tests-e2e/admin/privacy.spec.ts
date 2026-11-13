import { API_BASE, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * /admin/privacy — GDPR/CCPA DSAR export + right-to-erasure (admin only).
 *
 * Surfaces the existing backend endpoints (`backend/app/api/privacy.py`):
 *  - POST /api/privacy/dsar     → portable PII bundle for a subject
 *  - POST /api/privacy/erasure  → irreversible PII redaction (confirm: true required)
 *  - GET  /api/privacy/requests → PII-free request history
 *
 * Exercises a `vendor_contact` subject (a Vendor row) end to end: DSAR export
 * shows the bundle, then erasure redacts it — asserting both the UI's
 * confirm-then-act gate (checkbox required, armed two-click) and the real
 * effect on the vendor row via a follow-up API read.
 */

interface VendorSummary {
	id: string;
	name: string;
	email: string | null;
}

async function apiHeaders(page: import('@playwright/test').Page) {
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	return {
		Authorization: `Bearer ${token}`,
		'X-Tenant-Slug': currentTenantSlug(),
		'Content-Type': 'application/json'
	};
}

/** Create a throwaway vendor via the API so erasure has something disposable
 *  to act on — this spec must not touch seeded demo data other tests rely on. */
async function createVendor(page: import('@playwright/test').Page, name: string): Promise<string> {
	const headers = await apiHeaders(page);
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers,
		data: {
			name,
			email: `${name.toLowerCase().replace(/\s+/g, '.')}@example.test`,
			tax_id: `99-${Date.now()}`
		}
	});
	expect(resp.ok()).toBe(true);
	const vendor = (await resp.json()) as VendorSummary;
	return vendor.id;
}

test.describe('/admin/privacy (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('exports a DSAR bundle for a vendor contact', async ({ page }) => {
		const name = `E2E Privacy Vendor ${Date.now()}`;
		const vendorId = await createVendor(page, name);

		await page.goto('/admin/privacy');
		await expect(page.getByRole('heading', { name: 'Privacy & DSAR' })).toBeVisible();

		await page.getByLabel('Subject type').selectOption('vendor_contact');
		await page.getByLabel('Identifier').fill(vendorId);
		await page.getByRole('button', { name: 'Export data (DSAR)' }).click();

		const dsarModal = page.getByRole('dialog', { name: 'Data export' });
		await expect(dsarModal).toBeVisible({ timeout: 10_000 });
		const bundle = dsarModal.getByTestId('dsar-bundle');
		await expect(bundle).toBeVisible();
		await expect(bundle).toContainText(name);
		// The summary is built from the response's typed record_counts, not
		// server prose (decisions §360): a fresh vendor has no invoices yet.
		const summary = dsarModal.getByTestId('dsar-summary');
		await expect(summary.locator('[data-section="related_invoices"]')).toHaveText('0 invoices');
		// Nothing was cut, so there is no truncation notice (decisions §369).
		await expect(dsarModal.getByTestId('dsar-truncated-notice')).toHaveCount(0);
		await dsarModal.getByRole('button', { name: 'Close' }).click();
		await expect(dsarModal).toBeHidden();

		// The request lands in the history table.
		await expect(page.getByText('DSAR export').first()).toBeVisible();
	});

	test('offers the complete bundle as a download, and says when the view was cut', async ({
		page
	}) => {
		const name = `E2E Bundle Vendor ${Date.now()}`;
		const vendorId = await createVendor(page, name);

		// A vendor past the 1000-row cap is too heavy to seed per run, so the
		// cut is simulated on the REAL response (only the two flags change);
		// the download itself is the real, uncapped route. The cap and the
		// bundle's completeness are pinned server-side in
		// backend/tests/test_privacy_full_bundle.py.
		await page.route('**/api/privacy/dsar', async (route) => {
			const response = await route.fetch();
			const json = await response.json();
			await route.fulfill({
				response,
				json: { ...json, complete: false, truncated_sections: ['related_invoices'] }
			});
		});

		await page.goto('/admin/privacy');
		await page.getByLabel('Subject type').selectOption('vendor_contact');
		await page.getByLabel('Identifier').fill(vendorId);
		await page.getByRole('button', { name: 'Export data (DSAR)' }).click();

		const dsarModal = page.getByRole('dialog', { name: 'Data export' });
		await expect(dsarModal).toBeVisible({ timeout: 10_000 });
		await expect(dsarModal.getByTestId('dsar-truncated-notice')).toContainText(
			'1 section holds more records'
		);

		const [download] = await Promise.all([
			page.waitForEvent('download'),
			dsarModal.getByRole('button', { name: 'Download complete bundle' }).click()
		]);
		expect(download.suggestedFilename()).toMatch(/^dsar-[0-9a-f-]+\.json$/);
		const path = await download.path();
		const { readFile } = await import('node:fs/promises');
		const bundle = JSON.parse(await readFile(path, 'utf-8'));
		expect(bundle.complete).toBe(true);
		expect(bundle.data.vendor.name).toBe(name);
		expect(bundle.data.related_invoices.truncated).toBe(false);
	});

	test('erasure requires the acknowledgement checkbox and is a real, irreversible redaction', async ({
		page
	}) => {
		const name = `E2E Erase Vendor ${Date.now()}`;
		const vendorId = await createVendor(page, name);

		await page.goto('/admin/privacy');
		await page.getByLabel('Subject type').selectOption('vendor_contact');
		await page.getByLabel('Identifier').fill(vendorId);
		await page.getByRole('button', { name: 'Erase data…' }).click();

		const eraseModal = page.getByRole('dialog', { name: 'Erase subject data' });
		await expect(eraseModal).toBeVisible();
		await expect(eraseModal.getByText(name)).toHaveCount(0); // identifier shown is the UUID, not the name
		await expect(eraseModal.locator('.mono')).toHaveText(vendorId);

		const confirmBtn = eraseModal.getByRole('button', { name: 'Erase permanently' });
		// Gated: unchecked acknowledgement disables the destructive control.
		await expect(confirmBtn).toBeDisabled();

		await eraseModal.getByLabel(/I understand this action is permanent/).check();
		await expect(confirmBtn).toBeEnabled();

		// First click arms (two-step confirm), doesn't erase yet.
		await confirmBtn.click();
		await expect(eraseModal.getByRole('button', { name: 'Click again to confirm' })).toBeVisible();

		// Second click actually erases.
		await eraseModal.getByRole('button', { name: 'Click again to confirm' }).click();
		await expect(eraseModal).toBeHidden({ timeout: 10_000 });
		await expect(page.getByText(/^Erased: \d+ fields? redacted, \d+ documents? deleted\.$/)).toBeVisible({
			timeout: 10_000
		});

		// Verify the real effect server-side. `vendor.name` is DELIBERATELY
		// preserved by erasure (it's denormalised onto every Invoice's
		// `vendor_name` money field — see backend/docs/privacy.md § What is
		// redacted vs. preserved) — only contact PII (email/phone/address/
		// tax_id/bank_details) is redacted. Asserting the name changed would
		// be asserting behavior the backend explicitly does not have.
		const headers = await apiHeaders(page);
		const vendorResp = await page.request.get(`${API_BASE}/api/vendors/${vendorId}`, { headers });
		expect(vendorResp.ok()).toBe(true);
		const vendor = (await vendorResp.json()) as VendorSummary;
		expect(vendor.name).toBe(name);
		expect(vendor.email).toBeNull();

		// Re-running erasure on the same subject is a safe no-op, surfaced in
		// the history as "Already erased" rather than a second real redaction.
		await page.reload();
		await page.getByLabel('Subject type').selectOption('vendor_contact');
		await page.getByLabel('Identifier').fill(vendorId);
		await page.getByRole('button', { name: 'Erase data…' }).click();
		await page.getByLabel(/I understand this action is permanent/).check();
		const confirmBtn2 = page.getByRole('button', { name: 'Erase permanently' });
		await confirmBtn2.click();
		await page.getByRole('button', { name: 'Click again to confirm' }).click();
		await expect(page.getByText('This subject was already erased')).toBeVisible({
			timeout: 10_000
		});
	});

	test('lists past requests', async ({ page }) => {
		await page.goto('/admin/privacy');
		await expect(page.getByRole('heading', { name: 'Request history', level: 2 })).toBeVisible();
		// Loading resolves either to rows or the explicit empty state — never stuck.
		await expect(page.getByTestId('privacy-loading')).toHaveCount(0, { timeout: 10_000 });
	});
});

test.describe('/admin/privacy (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/privacy');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Privacy & DSAR' })).toHaveCount(0);

		const headers = await apiHeaders(page);
		const resp = await page.request.get(`${API_BASE}/api/privacy/requests`, { headers });
		expect(resp.status()).toBe(403);
	});
});
