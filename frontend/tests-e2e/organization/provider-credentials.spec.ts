import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import type { ProviderCredentialStatus } from '#lib/types/providerCredentials.ts';

/**
 * /organization → Payments and AI Extraction: the processor API key and the
 * bring-your-own extraction key are write-only.
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
const BYOK_SECRET = 'sk-e2e-byok-key-never-rendered-0001';
const OPENAI_SECRET = 'sk-e2e-openai-key-never-rendered-0001';
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

async function resetExtraction(page: import('@playwright/test').Page): Promise<void> {
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
	const cleared = await page.request.put(`${API_BASE}/api/organization/credentials/extraction`, {
		headers,
		data: { clear: ['api_key', 'aws_secret_access_key'] }
	});
	expect(cleared.status()).toBe(200);
	const patched = await page.request.patch(`${API_BASE}/api/organization`, {
		headers,
		data: { settings: { extraction: { program_type: 'platform' } } }
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

	test('the BYOK extraction key is write-only, and a provider switch needs the new key', async ({
		page
	}) => {
		const loaded = Promise.all([
			page.waitForResponse(
				(r) => r.url().endsWith('/api/organization') && r.request().method() === 'GET'
			),
			page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials') && r.request().method() === 'GET'
			)
		]);
		await page.goto('/organization?section=extraction');
		await loaded;
		const save = page.getByRole('button', { name: 'Save Extraction Settings' });
		const field = page.getByTestId('extraction-api-key');
		const saved = () =>
			page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization') &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
		try {
			await expect(save).toBeVisible();
			await page.getByRole('combobox', { name: 'Program', exact: true }).selectOption('byok');
			await page
				.getByRole('combobox', { name: 'Provider', exact: true })
				.selectOption('claude_vision');
			await expect(field).toHaveValue('');
			await field.fill(BYOK_SECRET);

			// A refused credential PUT after a good PATCH is one error toast,
			// not a success toast and an error toast for the one click.
			const PUT_REFUSED = 'Credential store unavailable (e2e)';
			const putUrl = `${API_BASE}/api/organization/credentials/extraction`;
			await page.route(putUrl, (route) =>
				route.request().method() === 'PUT'
					? route.fulfill({ status: 503, json: { detail: PUT_REFUSED } })
					: route.fallback()
			);
			const refusedPatch = saved();
			await save.click();
			await refusedPatch;
			await expect(
				page.getByTestId('toast').filter({ hasText: PUT_REFUSED })
			).toHaveAttribute('data-toast-type', 'error');
			await expect(page.locator('[data-testid="toast"][data-toast-type="success"]')).toHaveCount(0);
			await expect(field).toHaveValue(BYOK_SECRET);
			await page.unroute(putUrl);

			const patch = saved();
			const put = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials/extraction') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await save.click();
			expect(await (await patch).text()).not.toContain(BYOK_SECRET);
			expect(await (await put).text()).not.toContain(BYOK_SECRET);
			await expect(
				page.getByTestId('toast').filter({ hasText: 'AI Extraction saved' })
			).toHaveCount(1);
			await expect(field).toHaveValue('');
			await expect(field).toHaveAttribute('placeholder', KEEP);
			expect((await credentialStatus(page)).extraction).toEqual(['api_key']);

			// Another provider: the stored Anthropic key belongs to Claude, so the
			// field stops saying a key is stored and says the save would drop it.
			await page
				.getByRole('combobox', { name: 'Provider', exact: true })
				.selectOption('openai_vision');
			await expect(field).not.toHaveAttribute('placeholder', KEEP);
			await expect(field).toHaveAttribute('data-secret-saved', 'false');
			await expect(page.getByTestId('extraction-api-key-clear')).toHaveCount(0);
			const hint = page.getByTestId('extraction-switch-hint');
			await expect(hint).toHaveText(
				'Saving drops the stored key; enter one for GPT-4V (OpenAI).'
			);
			await expect(field).toHaveAttribute('aria-describedby', /extraction-switch-hint/);

			// A blank save would leave BYOK keyless: refused before any request,
			// the Claude key still stored, and the field marked.
			const writes: string[] = [];
			const onRequest = (r: import('@playwright/test').Request) => {
				if (r.url().includes('/api/organization') && r.method() !== 'GET')
					writes.push(`${r.method()} ${r.url()}`);
			};
			page.on('request', onRequest);
			await save.click();
			await expect(
				page.getByTestId('toast').filter({
					hasText: 'Enter a key for GPT-4V (OpenAI): switching provider drops the stored key.'
				})
			).toHaveAttribute('data-toast-type', 'error');
			await expect(field).toHaveAttribute('aria-invalid', 'true');
			await expect(field).toBeFocused();
			page.off('request', onRequest);
			expect(writes).toEqual([]);
			expect((await credentialStatus(page)).extraction).toEqual(['api_key']);

			// With the OpenAI key typed: one PATCH (dropping the Claude key),
			// then the PUT, and a single success toast once both have settled.
			await field.fill(OPENAI_SECRET);
			await expect(field).not.toHaveAttribute('aria-invalid', 'true');
			const switched = saved();
			const reread = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials') && r.request().method() === 'GET'
			);
			const switchPut = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/credentials/extraction') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await save.click();
			await switched;
			expect(await (await reread).json()).toMatchObject({ extraction: [] });
			expect(await (await switchPut).text()).not.toContain(OPENAI_SECRET);
			await expect(field).toHaveValue('');
			await expect(field).toHaveAttribute('placeholder', KEEP);
			await expect(hint).toHaveCount(0);
			expect((await credentialStatus(page)).extraction).toEqual(['api_key']);
				} finally {
			await resetExtraction(page);
		}
	});
});
