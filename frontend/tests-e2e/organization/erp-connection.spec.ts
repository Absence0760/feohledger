import type { Page } from '@playwright/test';

import { expectNoA11yViolations } from '../a11y/axe-helper';
import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → ERP Integration (`organization/ErpConnectionPanel.svelte`).
 *
 * The panel renders from the backend catalogue (`GET
 * /api/organization/erp/providers`), and ERP secrets are write-only: a saved
 * secret reads back masked, its input starts blank with a "saved" placeholder,
 * and a blank secret on save keeps the stored value. What this pins is what only
 * the browser shows: choose an ERP → its fields render → save → reload → the
 * secret is never in the page or the response. The server rules behind it
 * (mask, keep-on-blank, explicit replace, the OAuth block, the audit row) are
 * pytest's (`backend/tests/test_erp_catalog.py`).
 *
 * NetSuite is a registered direct adapter and nothing here tests a connection,
 * so no ERP is contacted. Every test clears `settings.erp` in `finally`.
 */

const CONSUMER_SECRET = 'e2e-consumer-secret-never-rendered';
const TOKEN_SECRET = 'e2e-token-secret-never-rendered';

async function resetErp(page: Page): Promise<void> {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: { erp: null } }
	});
	expect(resp.status()).toBe(200);
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

test.describe('/organization ERP connection', () => {
	test('choose an ERP, save, and the secrets come back masked', async ({ page }) => {
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
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			const body = await (await first).text();
			expect(body).not.toContain(CONSUMER_SECRET);
			expect(body).not.toContain(TOKEN_SECRET);

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
				await expect(input).toHaveAttribute('placeholder', '•••• saved, leave blank to keep');
			}
			const html = await page.content();
			expect(html).not.toContain(CONSUMER_SECRET);
			expect(html).not.toContain(TOKEN_SECRET);

			// Saving again with the secrets left blank keeps them stored.
			await card.getByLabel('Account ID', { exact: true }).fill('7654321');
			const second = saved(page);
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			const erp = ((await (await second).json()) as { settings: { erp: Record<string, string> } })
				.settings.erp;
			expect(erp.account_id).toBe('7654321');
			expect(erp.consumer_secret).toBe('********');
			expect(erp.token_secret).toBe('********');

			await expectNoA11yViolations(page);
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
			await expect(card.getByLabel('Merge.dev API Key', { exact: true })).toHaveAttribute(
				'type',
				'password'
			);
			await expect(card.getByLabel('Account Token', { exact: true })).toBeVisible();
			// A required field left empty is named in the live region, not sent.
			await card.getByRole('button', { name: 'Save ERP Settings' }).click();
			await expect(card.getByTestId('erp-status')).toHaveText(
				'Fill in: Merge.dev API Key, Account Token.'
			);
		} finally {
			await resetErp(page);
		}
	});
});
