import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	tenantHeaders,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * A line on no report is its creator's (decisions §434): only they may change
 * its claim fields, replace its receipt or delete it (403 `expense_not_creator`),
 * while GL coding stays open to every entry role. The page must not offer what
 * the server refuses: a colleague's loose line shows no Delete, and its modal
 * opens with the claim fields read-only and the GL account still editable.
 *
 * Real stack, no stubs: the colleague's line is created by the worker's clerk
 * through the API, and the page is read as the worker admin (the default
 * storage state), so the creator the page compares against is the real one.
 */

interface Created {
	id: string;
}

async function clerkHeaders(
	page: Page,
	creds: { email: string; password: string }
): Promise<Record<string, string>> {
	const slug = currentTenantSlug();
	const res = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug },
		data: { email: creds.email, password: creds.password }
	});
	expect(res.status()).toBe(200);
	const body = (await res.json()) as { access_token?: string; token?: string };
	const token = body.access_token ?? body.token;
	expect(token).toBeTruthy();
	return tenantHeaders(token as string, slug);
}

async function createExpense(
	page: Page,
	headers: Record<string, string>,
	merchant: string
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/expenses`, {
		headers,
		data: { merchant, amount: '42.50', currency: 'USD', expense_date: '2026-03-01', category: 'travel' }
	});
	expect(res.status()).toBe(201);
	return ((await res.json()) as Created).id;
}

function rowFor(page: Page, merchant: string) {
	return page.locator('table tbody tr.clickable').filter({ hasText: merchant });
}

test.describe('/expenses — a loose line is its creator’s (§434)', () => {
	test("a colleague's loose line offers no Delete and opens with read-only claim fields", async ({
		page,
		tenantClerk
	}) => {
		const tag = `E2E-Creator-${Date.now()}`;
		const theirs = `${tag} Theirs`;
		const mine = `${tag} Mine`;
		const ids: string[] = [];
		try {
			await page.goto('/expenses');
			ids.push(await createExpense(page, await clerkHeaders(page, tenantClerk), theirs));
			ids.push(await createExpense(page, await authedTenantHeaders(page), mine));
			const glId = tenantPsql(
				'SELECT id FROM gl_accounts WHERE is_active AND entity_id IS NULL ORDER BY code LIMIT 1'
			).trim();
			expect(glId, 'worker tenant has a shared GL account').not.toBe('');

			await page.goto(`/expenses?search=${encodeURIComponent(tag)}`);
			await expect(rowFor(page, mine)).toBeVisible();
			await expect(rowFor(page, theirs)).toBeVisible();

			// The admin's own line keeps Delete; the clerk's does not.
			await expect(rowFor(page, mine).getByRole('button', { name: 'Delete' })).toBeVisible();
			await expect(rowFor(page, theirs).getByRole('button', { name: 'Delete' })).toHaveCount(0);

			await rowFor(page, theirs)
				.getByRole('button', { name: `Edit expense ${theirs}` })
				.click();
			const dialog = page.getByRole('dialog');
			await expect(dialog.getByTestId('expense-claim-locked')).toBeVisible();
			await expect(dialog.getByLabel('Merchant')).toBeDisabled();
			await expect(dialog.getByLabel('Amount')).toBeDisabled();
			await expect(dialog.getByLabel('Payment Method')).toBeDisabled();
			// No receipt upload on a line whose receipt only its creator may replace.
			await expect(dialog.getByRole('button', { name: 'Attach' })).toHaveCount(0);
			const gl = dialog.getByLabel('GL Account');
			await expect(gl).toBeEnabled();

			// Save sends the GL coding alone, and the server accepts it.
			await gl.selectOption(glId);
			const patch = page.waitForResponse(
				(r) => r.request().method() === 'PATCH' && new URL(r.url()).pathname === `/api/expenses/${ids[0]}`
			);
			await dialog.getByRole('button', { name: 'Save' }).click();
			const res = await patch;
			expect(res.request().postDataJSON()).toEqual({ gl_account_id: glId });
			expect(res.status()).toBe(200);
			expect(tenantPsql(`SELECT gl_account_id FROM expenses WHERE id='${ids[0]}'`).trim()).toBe(glId);

			// The admin's own line opens fully editable.
			await rowFor(page, mine)
				.getByRole('button', { name: `Edit expense ${mine}` })
				.click();
			const own = page.getByRole('dialog');
			await expect(own.getByTestId('expense-claim-locked')).toHaveCount(0);
			await expect(own.getByLabel('Merchant')).toBeEnabled();
		} finally {
			for (const id of ids) tenantPsql(`DELETE FROM expenses WHERE id='${id}'`);
		}
	});
});
