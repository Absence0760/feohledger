import {
	API_BASE,
	authedTenantHeaders,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { resolveDialog } from './dialogs';

interface ExceptionRow {
	id: string;
	invoice_id: string;
	exception_type: string;
	status: string;
}

async function listExceptions(
	page: import('@playwright/test').Page,
	status: string
): Promise<ExceptionRow[]> {
	const resp = await page.request.get(`${API_BASE}/api/exceptions?status=${status}`, {
		headers: await authedTenantHeaders(page)
	});
	const body = (await resp.json()) as { items: ExceptionRow[] };
	return body.items;
}

/**
 * Reset an exception back to `open` state in the worker's tenant DB.
 * The product API has no "reopen" endpoint by design (resolved
 * exceptions are immutable for audit purposes), so direct SQL is the
 * only revertible path. psql is available on dev workstations and in
 * the CI backend container.
 */
function resetExceptionToOpen(id: string): void {
	tenantPsql(
		`UPDATE exceptions SET status='open', resolution=NULL, resolved_by=NULL, resolved_at=NULL WHERE id='${id}'`
	);
}

/**
 * /exceptions — resolve / escalate / dismiss UI flows. Each test
 * mutates state then reverts it via psql in finally so the suite is
 * re-runnable. Open exceptions come from the seed (3 per tenant).
 */

test.describe('/exceptions resolve actions', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/exceptions');
	});

	test('Resolve button opens the resolve modal with three action choices', async ({ page }) => {
		const open = await listExceptions(page, 'open');
		expect(open.length).toBeGreaterThan(0);

		const row = page.locator('table tbody tr').first();
		await row.getByRole('button', { name: 'Resolve' }).click();

		const modal = resolveDialog(page);
		await expect(modal).toBeVisible();
		// Found by test id, but it must still HAVE a name — a screen reader
		// announces the dialog by it (WCAG 4.1.2). Which words is the catalogue's
		// business, not this spec's.
		await expect(modal).toHaveAccessibleName(/\S/);
		await expect(modal.getByRole('button', { name: 'Resolve', exact: true })).toBeVisible();
		await expect(modal.getByRole('button', { name: 'Escalate' })).toBeVisible();
		await expect(modal.getByRole('button', { name: 'Dismiss' })).toBeVisible();

		// Cancel closes the modal.
		await modal.getByRole('button', { name: 'Cancel' }).click();
		await expect(modal).toBeHidden();
	});

	test('Resolve flips an open exception to resolved status', async ({ page }) => {
		const open = await listExceptions(page, 'open');
		expect(open.length).toBeGreaterThan(0);
		const target = open[0];

		try {
			const row = page.locator('table tbody tr').first();
			await row.getByRole('button', { name: 'Resolve' }).click();

			const modal = resolveDialog(page);
			await modal.locator('input[type="text"]').fill('e2e: confirmed and closed');

			const posted = page.waitForResponse(
				(r) =>
					r.url().includes('/api/exceptions/') &&
					r.url().includes('/resolve') &&
					r.request().method() === 'POST'
			);
			await modal.getByRole('button', { name: 'Resolve', exact: true }).click();
			const resp = await posted;
			expect(resp.status()).toBe(200);
			expect(((await resp.json()) as { status: string }).status).toBe('resolved');
		} finally {
			resetExceptionToOpen(target.id);
		}
	});

	test('Escalate flips an open exception to escalated status', async ({ page }) => {
		const open = await listExceptions(page, 'open');
		expect(open.length).toBeGreaterThan(0);
		const target = open[0];

		try {
			const row = page.locator('table tbody tr').first();
			await row.getByRole('button', { name: 'Resolve' }).click();

			const modal = resolveDialog(page);
			await modal.locator('input[type="text"]').fill('e2e: needs CFO review');

			const posted = page.waitForResponse(
				(r) =>
					r.url().includes('/api/exceptions/') &&
					r.url().includes('/resolve') &&
					r.request().method() === 'POST'
			);
			await modal.getByRole('button', { name: 'Escalate' }).click();
			const resp = await posted;
			expect(resp.status()).toBe(200);
			expect(((await resp.json()) as { status: string }).status).toBe('escalated');
		} finally {
			resetExceptionToOpen(target.id);
		}
	});

	test('Dismiss flips an open exception to dismissed status (no note required)', async ({
		page
	}) => {
		const open = await listExceptions(page, 'open');
		expect(open.length).toBeGreaterThan(0);
		const target = open[0];

		try {
			const row = page.locator('table tbody tr').first();
			await row.getByRole('button', { name: 'Resolve' }).click();

			const modal = resolveDialog(page);
			// Dismiss accepts an empty note, and posts it empty: the real backend
			// takes `resolution: ''` and records no note, rather than the invented
			// "dismissd by user" the page used to store.
			const posted = page.waitForResponse(
				(r) =>
					r.url().includes('/api/exceptions/') &&
					r.url().includes('/resolve') &&
					r.request().method() === 'POST'
			);
			await modal.getByRole('button', { name: 'Dismiss' }).click();
			const resp = await posted;
			expect(resp.request().postDataJSON()).toEqual({ resolution: '', action: 'dismiss' });
			expect(resp.status()).toBe(200);
			expect(((await resp.json()) as { status: string }).status).toBe('dismissed');
		} finally {
			resetExceptionToOpen(target.id);
		}
	});
});
