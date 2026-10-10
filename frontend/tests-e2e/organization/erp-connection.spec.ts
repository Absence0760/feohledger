import type { Page, Route } from '@playwright/test';

import type { ErpOAuthStatus } from '#lib/types/erpConnection.ts';

import { expectNoA11yViolations } from '../a11y/axe-helper';
import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → ERP Integration (`organization/ErpConnectionPanel.svelte`).
 *
 * The panel renders from the backend catalogue (`GET
 * /api/organization/erp/providers`), and ERP secrets are write-only: sealed
 * server-side, never returned, each one a `ui/SecretField` that starts blank and
 * says whether a value is stored (`GET /api/organization/credentials`, names
 * only). A save PATCHes the configuration and PUTs only typed or removed
 * secrets to `/api/organization/credentials/erp`. What this pins is what only
 * the browser shows: choose an ERP → its fields render → save → reload → the
 * secret is never in the page or any response; a new destination makes the
 * fields ask again; the remove toggle clears one; a missing required field is
 * marked and focused. The server rules behind it (sealing, the destination
 * drop, the OAuth block, the audit rows) are pytest's
 * (`backend/tests/test_erp_catalog.py`, `test_provider_credentials.py`).
 *
 * The OAuth tests stub `GET /oauth/status` (and the authorize / disconnect
 * calls) on their exact pathnames: whether the platform has a QuickBooks or
 * Xero app depends on the backend's env, and no test here may reach an ERP.
 * The authorize stub hands back this page's own callback-return URL, which is
 * the round trip the real provider ends with.
 *
 * NetSuite and SYSPRO are registered direct adapters and nothing here tests a
 * connection, so no ERP is contacted. Every test clears `settings.erp` in
 * `finally`.
 */

const CONSUMER_SECRET = 'e2e-consumer-secret-never-rendered';
const TOKEN_SECRET = 'e2e-token-secret-never-rendered';
const SAVED_HINT = 'A value is stored. It is never shown again; type a new one to replace it.';
const NOT_SAVED_HINT = 'Nothing is stored yet.';
const STATUS_PATH = '/api/organization/erp/oauth/status';
const ERP_SECRETS = [
	'api_key',
	'account_token',
	'client_secret',
	'consumer_secret',
	'token_secret',
	'password',
	'subscription_key',
	'operator_password',
	'company_password',
	'webhook_signing_secret',
	'webhook_secret'
];

/** Clear `settings.erp` and every sealed ERP secret. */
async function resetErp(page: Page): Promise<void> {
	const headers = { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers,
		data: { settings: { erp: null } }
	});
	expect(resp.status()).toBe(200);
	const creds = await page.request.put(`${API_BASE}/api/organization/credentials/erp`, {
		headers,
		data: { clear: ERP_SECRETS }
	});
	expect(creds.status()).toBe(200);
}

/** Which ERP secrets the server holds — names only. */
async function storedErpSecrets(page: Page): Promise<string[]> {
	const resp = await page.request.get(`${API_BASE}/api/organization/credentials`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(200);
	return ((await resp.json()) as { erp: string[] }).erp;
}

function credentialsPut(page: Page) {
	return page.waitForResponse(
		(r) =>
			new URL(r.url()).pathname === '/api/organization/credentials/erp' &&
			r.request().method() === 'PUT' &&
			r.status() === 200
	);
}

function panel(page: Page) {
	return page.getByTestId('erp-panel');
}

function saved(page: Page) {
	return page.waitForResponse(
		(r) =>
			new URL(r.url()).pathname === '/api/organization' &&
			r.request().method() === 'PATCH' &&
			r.status() === 200
	);
}

function oauthStatus(overrides: Partial<ErpOAuthStatus> = {}): ErpOAuthStatus {
	return {
		provider: null,
		connected: false,
		needs_reconnect: false,
		redirect_uri: `${API_BASE}/api/erp/oauth/callback`,
		providers: [
			{
				key: 'quickbooks_online',
				display_name: 'QuickBooks Online',
				available: true,
				client_source: 'platform'
			},
			{ key: 'xero', display_name: 'Xero', available: false, client_source: null }
		],
		...overrides
	} satisfies ErpOAuthStatus;
}

/** Answer `GET /oauth/status` with whatever `current()` returns at the time. */
async function stubStatus(page: Page, current: () => ErpOAuthStatus): Promise<void> {
	await page.route(
		(url) => url.pathname === STATUS_PATH,
		(route: Route) => route.fulfill({ json: current() })
	);
}

test.describe('/organization ERP connection', () => {
	test('choose an ERP, save, and the secrets are stored but never come back', async ({ page }) => {
		await page.goto('/organization?section=erp');
		const card = panel(page);
		try {
			await resetErp(page);
			await page.reload();
			const select = card.getByTestId('erp-provider-select');
			await expect(select).toBeVisible();

			// The dropdown leads with the two region groups.
			await expect(select.locator('optgroup[label="Popular in the United States"]')).toHaveCount(1);
			await expect(select.locator('optgroup[label="Popular in South Africa"]')).toHaveCount(1);

			await select.selectOption('netsuite');
			await expect(card.getByLabel('Account ID', { exact: true })).toBeVisible();
			await card.getByLabel('Account ID', { exact: true }).fill('1234567');
			await card.getByLabel('Consumer Key', { exact: true }).fill('ck-e2e');
			await card.getByLabel('Consumer Secret', { exact: true }).fill(CONSUMER_SECRET);
			await card.getByLabel('Token ID', { exact: true }).fill('tid-e2e');
			await card.getByLabel('Token Secret', { exact: true }).fill(TOKEN_SECRET);

			const first = saved(page);
			const firstPut = credentialsPut(page);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			const patch = await first;
			// The configuration PATCH carries no secret; the PUT does, and answers names.
			expect(JSON.stringify(patch.request().postDataJSON())).not.toContain(CONSUMER_SECRET);
			for (const resp of [patch, await firstPut]) {
				const body = await resp.text();
				expect(body).not.toContain(CONSUMER_SECRET);
				expect(body).not.toContain(TOKEN_SECRET);
			}
			expect((await storedErpSecrets(page)).sort()).toEqual(['consumer_secret', 'token_secret']);

			// The typed secret is dropped from the form as soon as it is stored.
			const secretInput = card.getByLabel('Consumer Secret', { exact: true });
			await expect(secretInput).toHaveValue('');
			await expect(secretInput).toHaveAttribute('data-secret-saved', 'true');

			await page.reload();
			await expect(card.getByTestId('erp-provider-select')).toHaveValue('netsuite');
			await expect(card.getByLabel('Account ID', { exact: true })).toHaveValue('1234567');
			for (const label of ['Consumer Secret', 'Token Secret']) {
				const input = card.getByLabel(label, { exact: true });
				await expect(input).toHaveValue('');
				await expect(input).toHaveAttribute('type', 'password');
				// "Stored" is said in visible text the input is described by, not
				// only in a placeholder that vanishes on the first keystroke.
				await expect(input).toHaveAccessibleDescription(SAVED_HINT);
			}
			const html = await page.content();
			expect(html).not.toContain(CONSUMER_SECRET);
			expect(html).not.toContain(TOKEN_SECRET);

			// A new Account ID is a new destination: the backend drops the stored
			// secrets rather than send them there, so the form asks for them again.
			await card.getByLabel('Account ID', { exact: true }).fill('7654321');
			await expect(card.getByLabel('Consumer Secret', { exact: true })).toHaveAttribute(
				'data-secret-saved',
				'false'
			);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			await expect(card.getByLabel('Consumer Secret', { exact: true })).toHaveAttribute(
				'aria-invalid',
				'true'
			);
			await card.getByLabel('Account ID', { exact: true }).fill('1234567');

			// Saving again with the secrets left blank keeps them stored, and sends
			// no credentials request at all.
			await card.getByLabel('Token ID', { exact: true }).fill('tid-e2e-2');
			const second = saved(page);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			const erp = ((await (await second).json()) as { settings: { erp: Record<string, string> } })
				.settings.erp;
			expect(erp.token_id).toBe('tid-e2e-2');
			expect(erp).not.toHaveProperty('consumer_secret');
			expect((await storedErpSecrets(page)).sort()).toEqual(['consumer_secret', 'token_secret']);

			// Removing a required secret makes it missing: the save is refused and
			// the field is marked and focused (WCAG 3.3.1); unticking undoes it.
			const tokenSecret = card.getByLabel('Token Secret', { exact: true });
			const remove = card.getByTestId('erp-secret-token_secret-clear');
			await remove.check();
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			await expect(card.getByTestId('erp-status')).toHaveText('Fill in: Token Secret.');
			await expect(tokenSecret).toHaveAttribute('aria-invalid', 'true');
			await expect(tokenSecret).toBeFocused();
			await remove.uncheck();
			await expect(tokenSecret).toHaveAccessibleDescription(SAVED_HINT);

			await expectNoA11yViolations(page);
		} finally {
			await resetErp(page);
		}
	});

	test('the remove toggle clears an optional stored secret', async ({ page }) => {
		await page.goto('/organization?section=erp');
		const card = panel(page);
		try {
			await card.getByTestId('erp-provider-select').selectOption('syspro');
			await card.getByLabel('Base URL', { exact: true }).fill('https://syspro.example.com/rest');
			await card.getByLabel('Operator', { exact: true }).fill('ADMIN');
			await card.getByLabel('Operator password', { exact: true }).fill('op-pass');
			await card.getByLabel('Company ID', { exact: true }).fill('EDU1');
			await card.getByLabel('Company password (optional)', { exact: true }).fill('co-pass');
			const firstPut = credentialsPut(page);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			await firstPut;

			await card.getByTestId('erp-secret-company_password-clear').check();
			const secondPut = credentialsPut(page);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			const put = await secondPut;
			expect(put.request().postDataJSON()).toEqual({ clear: ['company_password'] });
			// The required secret was left blank, so it is kept.
			expect(((await put.json()) as { erp: string[] }).erp).toEqual(['operator_password']);
			const company = card.getByLabel('Company password (optional)', { exact: true });
			await expect(company).toHaveAttribute('data-secret-saved', 'false');
			await expect(company).toHaveAccessibleDescription(NOT_SAVED_HINT);
		} finally {
			await resetErp(page);
		}
	});

	test('Merge.dev offers the long tail and names its plan', async ({ page }) => {
		await page.goto('/organization?section=erp');
		const card = panel(page);
		try {
			await card.getByTestId('erp-provider-select').selectOption('merge_dev');
			await expect(card.getByText('Connecting through Merge.dev is part of the Scale plan.')).toBeVisible();
			const longTail = card.getByTestId('erp-merge-type');
			await expect(longTail.locator('option[value="sap_s4hana"]')).toHaveCount(1);
			// Merge.dev's catch-all is labelled from the catalogue, not the wire.
			await expect(longTail.locator('option[value="other"]')).toHaveText('Other');
			const apiKey = card.getByLabel('Merge.dev API Key', { exact: true });
			await expect(apiKey).toHaveAttribute('type', 'password');
			await expect(card.getByLabel('Account Token', { exact: true })).toBeVisible();
			// A required field left empty is named in the live region, marked
			// invalid and focused, not sent.
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			await expect(card.getByTestId('erp-status')).toHaveText(
				'Fill in: Merge.dev API Key, Account Token.'
			);
			await expect(apiKey).toHaveAttribute('aria-invalid', 'true');
			await expect(card.getByLabel('Account Token', { exact: true })).toHaveAttribute(
				'aria-invalid',
				'true'
			);
			await expect(apiKey).toBeFocused();
			// Typing clears the mark on that field alone.
			await apiKey.fill('test_key');
			await expect(apiKey).not.toHaveAttribute('aria-invalid');
		} finally {
			await resetErp(page);
		}
	});

	test('an OAuth ERP connects with one button and disconnects on a confirmed click', async ({
		page
	}) => {
		let current = oauthStatus();
		await stubStatus(page, () => current);
		let authorizeCalls = 0;
		await page.route(
			(url) => url.pathname === '/api/organization/erp/oauth/quickbooks_online/authorize',
			(route) => {
				authorizeCalls += 1;
				// What the real provider ends with: the callback 302s back here.
				const back = new URL('/organization?section=erp&erp_connected=quickbooks_online', page.url());
				return route.fulfill({ json: { authorize_url: back.href } });
			}
		);
		await page.route(
			(url) => url.pathname === '/api/organization/erp/oauth/disconnect',
			(route) => {
				current = oauthStatus();
				return route.fulfill({ json: { disconnected: true, revoked: true } });
			}
		);

		await page.goto('/organization?section=erp');
		const card = panel(page);
		try {
			await card.getByTestId('erp-provider-select').selectOption('quickbooks_online');
			await expect(card.getByTestId('erp-oauth-state')).toHaveText(
				'Not connected to QuickBooks Online yet.'
			);
			// One primary action: Connect. No separate Save, no Test.
			const connect = card.getByRole('button', { name: 'Connect to QuickBooks Online' });
			await expect(connect).toBeVisible();
			await expect(card.getByRole('button', { name: 'Save ERP Settings' })).toHaveCount(0);
			// The platform has an app, so the own-app section starts collapsed and
			// its client id is optional.
			const byo = card.getByTestId('erp-byo-app');
			await expect(byo).not.toHaveAttribute('open');
			await byo.getByText('Use your own app (advanced)').click();
			await expect(card.getByLabel('Client ID (optional)', { exact: true })).toBeVisible();
			await expect(card.getByTestId('erp-redirect-uri')).toContainText(
				`${API_BASE}/api/erp/oauth/callback`
			);
			await expect(card.getByRole('button', { name: 'Copy' })).toBeVisible();

			// Connect saves the choice first, then opens the consent page.
			current = oauthStatus({
				provider: 'quickbooks_online',
				connected: true,
				external_tenant_id: '9341'
			});
			const save = saved(page);
			await connect.click();
			const sent = (await save).request().postDataJSON() as {
				settings: { erp: Record<string, unknown> };
			};
			expect(sent.settings.erp.type).toBe('quickbooks_online');
			expect(sent.settings.erp).not.toHaveProperty('oauth');

			// Back from the provider: the message shows, the params go.
			await expect(card.getByTestId('erp-status')).toHaveText(
				'QuickBooks Online is now connected.'
			);
			await expect(page).not.toHaveURL(/erp_connected/);
			await expect(page).toHaveURL(/section=erp/);
			expect(authorizeCalls).toBe(1);
			await expect(card.getByTestId('erp-oauth-state')).toHaveText(
				'Connected to QuickBooks Online (company 9341).'
			);
			// Connected: settings are saved with Save; Connect is gone.
			await expect(card.getByRole('button', { name: 'Save ERP Settings' })).toBeVisible();
			await expect(connect).toHaveCount(0);

			// Disconnect is armed by the first click, and disarmed by a click elsewhere.
			const disconnect = card.getByRole('button', { name: 'Disconnect' });
			await disconnect.click();
			const confirm = card.getByRole('button', { name: 'Confirm disconnect' });
			await expect(confirm).toBeVisible();
			await card.getByRole('heading', { name: 'ERP Integration' }).click();
			await expect(card.getByRole('button', { name: 'Disconnect' })).toBeVisible();
			await card.getByRole('button', { name: 'Disconnect' }).click();
			await card.getByRole('button', { name: 'Confirm disconnect' }).click();
			await expect(card.getByTestId('erp-status')).toHaveText(
				'Disconnected from QuickBooks Online.'
			);
			await expect(card.getByTestId('erp-oauth-state')).toHaveText(
				'Not connected to QuickBooks Online yet.'
			);
		} finally {
			await resetErp(page);
		}
	});

	test('an OAuth error return is shown once and leaves the address bar', async ({ page }) => {
		await stubStatus(page, () => oauthStatus());
		await page.goto('/organization?section=erp&erp_error=access_denied');
		const card = panel(page);
		const status = card.getByTestId('erp-status');
		await expect(status).toHaveText(
			'You declined access in your ERP. Connect again and approve access to finish.'
		);
		await expect(page).not.toHaveURL(/erp_error/);
		await expect(page).toHaveURL(/section=erp/);

		// Moving on replaces it, and it never comes back on its own.
		await card.getByTestId('erp-provider-select').selectOption('netsuite');
		await expect(status).toHaveText('');
		await card.getByTestId('erp-provider-select').selectOption('quickbooks_online');
		await expect(status).toHaveText('');
		await page.reload();
		await expect(card.getByTestId('erp-provider-select')).toBeVisible();
		await expect(status).toHaveText('');
	});

	test('an expired connection offers Reconnect, and no platform app needs your own', async ({
		page
	}) => {
		await stubStatus(page, () =>
			oauthStatus({ provider: 'xero', connected: false, needs_reconnect: true })
		);
		await page.goto('/organization?section=erp');
		const card = panel(page);
		try {
			await card.getByTestId('erp-provider-select').selectOption('xero');
			await expect(card.getByTestId('erp-oauth-state')).toHaveText(
				'The connection to Xero has expired. Reconnect to keep sending invoices.'
			);
			const reconnect = card.getByRole('button', { name: 'Reconnect to Xero' });
			await expect(reconnect).toBeVisible();
			await expect(card.getByRole('button', { name: 'Disconnect' })).toBeVisible();

			// No platform Xero app: the own-app section is open and required.
			await expect(card.getByTestId('erp-byo-app')).toHaveAttribute('open');
			await expect(card.getByTestId('erp-byo-hint')).toHaveText(
				'FeohLedger has no Xero app set up on this server, so enter the client ID and secret of an app you registered with Xero.'
			);
			const clientId = card.getByLabel('Client ID', { exact: true });
			await expect(clientId).toBeVisible();
			await expect(card.getByTestId('erp-redirect-uri')).toBeVisible();

			let patched = false;
			page.on('request', (r) => {
				if (new URL(r.url()).pathname === '/api/organization' && r.method() === 'PATCH') {
					patched = true;
				}
			});
			await reconnect.click();
			await expect(card.getByTestId('erp-status')).toHaveText(
				'Fill in: Client ID, Client Secret.'
			);
			await expect(clientId).toHaveAttribute('aria-invalid', 'true');
			await expect(clientId).toBeFocused();
			expect(patched).toBe(false);

			// Phone width: the open section, the URI and the action row still pass.
			await page.setViewportSize({ width: 320, height: 800 });
			await expect(card.getByTestId('erp-redirect-uri')).toBeVisible();
			await expectNoA11yViolations(page);
		} finally {
			await resetErp(page);
		}
	});

	test('a failed ERP list load offers Try again', async ({ page }) => {
		let failures = 1;
		await page.route(
			(url) => url.pathname === '/api/organization/erp/providers',
			(route) => {
				if (failures > 0) {
					failures -= 1;
					return route.fulfill({ status: 500, json: { detail: 'boom' } });
				}
				return route.fallback();
			}
		);
		await page.goto('/organization?section=erp');
		const card = panel(page);
		const failed = card.getByTestId('erp-load-failed');
		await expect(failed).toContainText("Couldn't load the ERP list.");
		await failed.getByRole('button', { name: 'Try again' }).click();
		await expect(card.getByTestId('erp-provider-select')).toBeVisible();
		await expect(failed).toHaveCount(0);
	});
});
