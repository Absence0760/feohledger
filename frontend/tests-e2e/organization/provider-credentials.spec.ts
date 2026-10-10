import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import type { ProviderCredentialStatus } from '#lib/types/providerCredentials.ts';

/**
 * /organization → Payments: the processor API key is write-only.
 *
 * ERP, payment-rail and card-issuer secrets are stored envelope-encrypted
 * server-side (`backend/app/services/provider_credentials.py`) and no endpoint
 * returns them, so the settings page can no longer pre-fill them. What this pins
 * is what only the browser can show: the field is empty on load and after a
 * save, it says a value is stored, a blank save keeps it, and the remove toggle
 * clears it — and the secret appears in no response the page reads. Every
 * server rule behind it is pytest's (`backend/tests/test_provider_credentials.py`).
 *
 * The block is reset through the API in `finally` (provider back to `mock`, the
 * stored key cleared) so a later spec on this worker's tenant starts clean.
 */

const SECRET = 'mt_e2e_api_key_never_rendered_0001';
const KEEP = 'Leave blank to keep the stored value';

async function credentialStatus(
	page: import('@playwright/test').Page
): Promise<ProviderCredentialStatus> {
	const resp = await page.request.get(`${API_BASE}/api/organization/credentials`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return (await resp.json()) as ProviderCredentialStatus;
}

async function reset(page: import('@playwright/test').Page): Promise<void> {
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
	const cleared = await page.request.put(`${API_BASE}/api/organization/credentials/payments`, {
		headers,
		data: { clear: ['api_key', 'webhook_secret'] }
	});
	expect(cleared.status()).toBe(200);
	const patched = await page.request.patch(`${API_BASE}/api/organization`, {
		headers,
		data: { settings: { payments: { provider: 'mock' } } }
	});
	expect(patched.status()).toBe(200);
}

test.describe('/organization provider credentials', () => {
	test('the payments API key is write-only end to end', async ({ page }) => {
		// Both reads fill this form; interacting before they land would have the
		// org read overwrite the provider choice.
		const loaded = Promise.all([
			page.waitForResponse(
				(r) => r.url().endsWith('/api/organization') && r.request().method() === 'GET'
			),
			page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials') && r.request().method() === 'GET'
			)
		]);
		await page.goto('/organization?section=payments');
		await loaded;
		const field = page.getByTestId('payments-api-key');
		const save = page.getByRole('button', { name: 'Save Payment Settings' });
		try {
			await expect(save).toBeVisible();
			await page
				.getByRole('combobox', { name: 'Provider', exact: true })
				.selectOption('modern_treasury');
			await expect(field).toHaveValue('');
			await field.fill(SECRET);

			const put = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials/payments') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await save.click();
			expect(await (await put).text()).not.toContain(SECRET);

			// Emptied, not holding the secret, and saying one is stored.
			await expect(field).toHaveValue('');
			await expect(field).toHaveAttribute('placeholder', KEEP);
			expect((await credentialStatus(page)).payments).toEqual(['api_key']);

			// A reload reads the page's data afresh: still empty, nothing echoed.
			const reread = Promise.all([
				page.waitForResponse(
					(r) => r.url().endsWith('/api/organization') && r.request().method() === 'GET'
				),
				page.waitForResponse(
					(r) =>
						r.url().endsWith('/api/organization/credentials') && r.request().method() === 'GET'
				)
			]);
			await page.reload();
			for (const resp of await reread) expect(await resp.text()).not.toContain(SECRET);
			await expect(field).toHaveAttribute('placeholder', KEEP);
			await expect(field).toHaveValue('');

			// Remove it explicitly.
			await page.getByTestId('payments-api-key-clear').check();
			const removed = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials/payments') &&
					r.request().method() === 'PUT'
			);
			await save.click();
			expect((await removed).status()).toBe(200);
			await expect(page.getByTestId('payments-api-key-clear')).toHaveCount(0);
			expect((await credentialStatus(page)).payments).toEqual([]);
		} finally {
			await reset(page);
		}
	});
});
