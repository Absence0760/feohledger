import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
import type { SsoSettingsStatus } from '#lib/types/ssoSettings.ts';

/**
 * /organization → Single Sign-On panel, over `GET/PUT /api/organization/sso`.
 *
 * The panel is the first UI for SSO at all — before it the raw PATCH was the
 * only way, and that PATCH dropped the whole block on a partial save. What
 * this pins is what only the browser can show: the client secret field is
 * write-only (never pre-filled, blank keeps the stored secret), and the
 * server's coded `sso_only` refusal reaches the admin as a sentence naming the
 * fields by their on-screen labels. Every server rule behind it is pytest's
 * (`backend/tests/test_organization_sso_settings.py`).
 *
 * `sso_only` is never left on: it would close password sign-in for the e2e
 * tenant this worker signs in to. Each test resets the block through the API in
 * `finally`.
 */

const SECRET = 'e2e-client-secret-never-rendered';

async function getSso(page: import('@playwright/test').Page): Promise<SsoSettingsStatus> {
	const resp = await page.request.get(`${API_BASE}/api/organization/sso`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return (await resp.json()) as SsoSettingsStatus;
}

async function resetSso(page: import('@playwright/test').Page): Promise<void> {
	const resp = await page.request.put(`${API_BASE}/api/organization/sso`, {
		headers: await authedTenantHeaders(page),
		data: { enabled: false, sso_only: false, protocol: 'oidc', clear_client_secret: true }
	});
	expect(resp.status()).toBe(200);
}

function panel(page: import('@playwright/test').Page) {
	return page.getByTestId('sso-panel');
}

test.describe('/organization single sign-on', () => {
	test('configures OIDC, and the secret is write-only', async ({ page }) => {
		await page.goto('/organization?section=sso');
		const card = panel(page);
		try {
			await expect(card.getByRole('button', { name: 'Save SSO Settings' })).toBeVisible();
			await card.getByLabel('Enable single sign-on').check();
			await card.getByLabel('Discovery URL').fill(
				'https://idp.example.com/.well-known/openid-configuration'
			);
			await card.getByLabel('Client ID').fill('feoh-e2e');
			await card.getByTestId('sso-client-secret').fill(SECRET);

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization/sso') &&
					r.request().method() === 'PUT' &&
					r.status() === 200
			);
			await card.getByTestId('sso-save').click();
			const body = await (await saved).text();
			expect(body).not.toContain(SECRET);

			// The field is emptied rather than holding the secret, and says one is stored.
			await expect(card.getByTestId('sso-client-secret')).toHaveValue('');
			await expect(card.getByTestId('sso-client-secret')).toHaveAttribute(
				'placeholder',
				'Leave blank to keep the stored secret'
			);
			const stored = await getSso(page);
			expect(stored.client_secret_configured).toBe(true);
			expect(stored.client_id).toBe('feoh-e2e');

			// A save with the field left blank keeps it.
			await card.getByLabel('Client ID').fill('feoh-e2e-2');
			const resaved = page.waitForResponse(
				(r) => r.url().endsWith('/api/organization/sso') && r.request().method() === 'PUT'
			);
			await card.getByTestId('sso-save').click();
			const request = (await resaved).request().postDataJSON() as Record<string, unknown>;
			expect(request).not.toHaveProperty('client_secret');
			expect((await getSso(page)).client_secret_configured).toBe(true);

			// Nothing on the page ever carries it.
			await page.reload();
			await expect(card.getByLabel('Client ID')).toHaveValue('feoh-e2e-2');
			expect(await page.content()).not.toContain(SECRET);
		} finally {
			await resetSso(page);
		}
	});

	test('requiring SSO over an incomplete provider is refused, naming the fields', async ({
		page
	}) => {
		await page.goto('/organization?section=sso');
		const card = panel(page);
		try {
			await expect(card.getByRole('button', { name: 'Save SSO Settings' })).toBeVisible();
			await card.getByLabel('Enable single sign-on').check();
			await card.getByLabel('Client ID').fill('feoh-e2e');
			await card.getByTestId('sso-only-toggle').check();
			await card.getByTestId('sso-save').click();

			await expect(card.getByTestId('sso-save-error')).toHaveText(
				'Requiring SSO closes password sign-in, so the identity-provider settings must be ' +
					'complete first. Missing or invalid: Discovery URL, Client secret.'
			);
			// Refused, so nothing was stored.
			const stored = await getSso(page);
			expect(stored.sso_only).toBe(false);
			expect(stored.enabled).toBe(false);
		} finally {
			await resetSso(page);
		}
	});

	test('shows the values to register at the IdP', async ({ page }) => {
		await page.goto('/organization?section=sso');
		const card = panel(page);
		const status = await getSso(page);
		await expect(card.getByText(status.oidc_redirect_uri, { exact: true })).toBeVisible();
		await card.getByLabel('Protocol').selectOption('saml');
		await expect(card.getByText(status.saml_acs_url, { exact: true })).toBeVisible();
		await expect(card.getByLabel('IdP signing certificate')).toBeVisible();
	});
});
