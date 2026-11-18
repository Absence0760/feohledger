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
			// An uncoded 503 (KMS down: retried later) offers no reset — only
			// `credential_block_unopenable` does.
			await expect(page.getByTestId('credential-reset-extraction')).toHaveCount(0);
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

	test('a block whose stored value will not open offers clearing it, after a confirm', async ({
		page
	}) => {
		// Every save of such a block answers this 503 (the writer must open the
		// stored value to merge into it); DELETE removes it unopened (decisions
		// §464). Both are stubbed: the server side is pytest's.
		const credsUrl = `${API_BASE}/api/organization/credentials`;
		const putUrl = `${credsUrl}/payments`;
		await page.route(credsUrl, (route) =>
			route.request().method() === 'GET'
				? route.fulfill({
						json: { erp: [], payments: ['api_key'], cards: [], commerce: [], extraction: [] }
					})
				: route.fallback()
		);
		const deletes: string[] = [];
		await page.route(putUrl, (route) => {
			const method = route.request().method();
			if (method === 'PUT') {
				return route.fulfill({
					status: 503,
					json: {
						detail: {
							code: 'credential_block_unopenable',
							message: 'The stored credentials for this section can no longer be opened.',
							params: { block: 'payments' }
						}
					}
				});
			}
			if (method === 'DELETE') {
				deletes.push(route.request().url());
				return route.fulfill({
					json: { erp: [], payments: [], cards: [], commerce: [], extraction: [] }
				});
			}
			return route.fallback();
		});

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
		const save = page.getByRole('button', { name: 'Save Payment Settings' });
		const field = page.getByTestId('payments-api-key');
		const prompt = page.getByTestId('credential-reset-payments');
		try {
			await expect(save).toBeVisible();
			await page
				.getByRole('combobox', { name: 'Provider', exact: true })
				.selectOption('modern_treasury');
			await expect(field).toHaveAttribute('placeholder', KEEP);
			await expect(prompt).toHaveCount(0);
			await field.fill(SECRET);

			const refused = page.waitForResponse(
				(r) => r.url() === putUrl && r.request().method() === 'PUT'
			);
			await save.click();
			expect((await refused).status()).toBe(503);
			await expect(
				page.getByTestId('toast').filter({
					hasText:
						'The stored credentials for this section can no longer be opened, so nothing was saved.'
				})
			).toHaveAttribute('data-toast-type', 'error');
			await expect(prompt).toBeVisible();

			// Opening the prompt asks first and says what goes; cancelling
			// removes nothing.
			await page.getByTestId('credential-reset-open-payments').click();
			const dialog = page.getByRole('dialog', { name: 'Clear stored credentials?' });
			await expect(dialog).toContainText(
				'Every stored secret in this section (API keys, client secrets, webhook signing secrets, and for an ERP its sign-in tokens) is removed without being opened. Each one must be entered again before this integration works.'
			);
			await dialog.getByRole('button', { name: 'Cancel' }).click();
			await expect(dialog).toHaveCount(0);
			expect(deletes).toEqual([]);

			await page.getByTestId('credential-reset-open-payments').click();
			const cleared = page.waitForResponse(
				(r) => r.url() === putUrl && r.request().method() === 'DELETE'
			);
			await page.getByTestId('credential-reset-confirm-payments').click();
			expect((await cleared).status()).toBe(200);
			expect(deletes).toEqual([putUrl]);
			await expect(
				page.getByTestId('toast').filter({
					hasText: 'Stored credentials cleared. Enter each one again.'
				})
			).toHaveAttribute('data-toast-type', 'success');
			await expect(prompt).toHaveCount(0);
			// Nothing is stored now, so the field no longer offers "keep".
			await expect(field).not.toHaveAttribute('placeholder', KEEP);
		} finally {
			await page.unroute(putUrl);
			await page.unroute(credsUrl);
			await reset(page);
		}
	});

	test('a stored card block with no sandbox key reads as live', async ({ page }) => {
		// The card adapters run such a block LIVE (`config.get("sandbox",
		// False)`); the page once showed it as testing, so the next save moved
		// it to sandbox and, since §464, dropped its keys. Stubbed: no tenant
		// state changes.
		const orgUrl = `${API_BASE}/api/organization`;
		await page.route(orgUrl, async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const resp = await route.fetch();
			const body = await resp.json();
			body.settings = {
				...body.settings,
				cards: { enabled: true, program_type: 'byok', provider: 'lithic', region: 'US' }
			};
			return route.fulfill({ response: resp, json: body });
		});
		const loaded = page.waitForResponse(
			(r) => r.url() === orgUrl && r.request().method() === 'GET'
		);
		try {
			await page.goto('/organization?section=cards');
			await loaded;
			const mode = page.getByRole('combobox', { name: 'Sandbox Mode', exact: true });
			await expect(mode).toBeVisible();
			await expect(mode.locator('option:checked')).toHaveText('Production');
		} finally {
			await page.unroute(orgUrl);
		}
	});
});
