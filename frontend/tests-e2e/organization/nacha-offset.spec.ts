import type { Page } from '@playwright/test';
import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /organization → Payments → the NACHA balanced-file offset account
 * (decisions §432). The account number is write-only: `GET /api/organization`
 * reads it back as a last-4 mask, so the input starts blank and a blank save
 * keeps the number on file (`services/nacha.carry_offset_account`). The only
 * sign an account is on file used to be the input's placeholder, which
 * vanishes on focus and is no accessible description; it is now visible
 * "On file: ****6789" text tied to the input with `aria-describedby`.
 *
 * Hermetic: `PATCH /api/organization` replaces the whole `settings.payments`
 * block, so the block is snapshotted first and restored afterwards (the
 * worker tenant holds no offset account, so the masked snapshot restores as
 * it was).
 */

const ORIGINATOR = { company_name: 'E2E CORP', company_id: '1123456789', odfi_routing: '021000021' };
const OFFSET_ROUTING = '021000021';
const OFFSET_ACCOUNT = '123456789';
const MASK = '****6789';

async function getPayments(page: Page): Promise<Record<string, unknown> | null> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	const body = (await resp.json()) as { settings: Record<string, unknown> };
	return (body.settings.payments as Record<string, unknown> | undefined) ?? null;
}

async function putPayments(page: Page, payments: Record<string, unknown> | null) {
	const resp = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: { payments: payments ?? {} } }
	});
	expect(resp.ok(), await resp.text()).toBe(true);
}

async function storedNacha(page: Page): Promise<Record<string, unknown> | null> {
	return ((await getPayments(page))?.nacha as Record<string, unknown> | null | undefined) ?? null;
}

function offsetFields(page: Page) {
	const block = page.getByTestId('payments-nacha-offset');
	return {
		routing: block.getByLabel('Offset account routing number', { exact: true }),
		account: block.getByLabel('Offset account number', { exact: true }),
		type: block.getByLabel('Offset account type', { exact: true }),
		onFile: block.getByTestId('payments-nacha-offset-onfile')
	};
}

/** Click Save and return the PATCH it sent. */
async function save(page: Page) {
	const patch = page.waitForResponse(
		(r) =>
			r.request().method() === 'PATCH' && new URL(r.url()).pathname === '/api/organization'
	);
	await page.getByRole('button', { name: 'Save Payment Settings' }).click();
	return patch;
}

test.describe('/organization — NACHA offset account', () => {
	let savedPayments: Record<string, unknown> | null = null;

	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=payments');
		savedPayments = await getPayments(page);
		await putPayments(page, { ...(savedPayments ?? {}), mode: 'record_only', nacha: ORIGINATOR });
		await page.reload();
		await expect(page.getByTestId('payments-nacha-offset')).toBeVisible();
	});

	test.afterEach(async ({ page }) => {
		await putPayments(page, savedPayments);
	});

	test('saved, kept on a blank re-save, and dropped when all three are cleared', async ({
		page
	}) => {
		let f = offsetFields(page);
		await expect(f.onFile).toHaveCount(0);

		// Save the offset.
		await f.routing.fill(OFFSET_ROUTING);
		await f.account.fill(OFFSET_ACCOUNT);
		await f.type.selectOption('checking');
		expect((await save(page)).status()).toBe(200);
		expect((await storedNacha(page))?.offset_account).toBe(MASK);

		// Reload: the input is blank and the mask is visible text describing it.
		await page.reload();
		f = offsetFields(page);
		await expect(f.account).toHaveValue('');
		await expect(f.onFile).toHaveText(`On file: ${MASK}`);
		await expect(f.account).toHaveAttribute(
			'aria-describedby',
			/(^|\s)org-payments-nacha-offset-onfile(\s|$)/
		);
		await expect(f.account).toHaveAccessibleDescription(new RegExp(`On file: \\*\\*\\*\\*6789`));

		// Re-save with the account blank: the stored number is kept.
		const resave = await save(page);
		expect(resave.status()).toBe(200);
		const sent = (resave.request().postDataJSON() as {
			settings: { payments: { nacha: Record<string, unknown> } };
		}).settings.payments.nacha;
		expect(sent.offset_account).toBe('');
		const kept = await storedNacha(page);
		expect(kept?.offset_account).toBe(MASK);
		expect(kept?.offset_routing).toBe(OFFSET_ROUTING);
		await expect(f.onFile).toHaveText(`On file: ${MASK}`);

		// Clear all three: the balanced file is dropped, account and all.
		await f.routing.fill('');
		await f.type.selectOption('');
		expect((await save(page)).status()).toBe(200);
		const dropped = await storedNacha(page);
		expect(dropped?.offset_account ?? null).toBeNull();
		expect(dropped?.offset_routing ?? null).toBeNull();
		await expect(f.onFile).toHaveCount(0);
	});

	test('a half-filled offset block is refused on the page, with no PATCH sent', async ({
		page
	}) => {
		const patches: string[] = [];
		page.on('request', (req) => {
			if (req.method() === 'PATCH' && new URL(req.url()).pathname === '/api/organization') {
				patches.push(req.url());
			}
		});
		const f = offsetFields(page);
		await f.routing.fill(OFFSET_ROUTING);
		await page.getByRole('button', { name: 'Save Payment Settings' }).click();
		// The toast names the two missing fields; nothing reached the server.
		await expect(page.getByText(/Check the bank-file details:.*Offset account number/)).toBeVisible();
		await expect(page.getByText(/Check the bank-file details:.*Offset account type/)).toBeVisible();
		expect(patches).toEqual([]);
		expect((await storedNacha(page))?.offset_routing ?? null).toBeNull();
	});
});
