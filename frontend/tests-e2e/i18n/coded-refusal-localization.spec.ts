import {
	API_BASE,
	authedTenantHeaders,
	expect,
	tenantBase,
	test
} from '../fixtures/helpers';
import type { Invoice } from '#lib/types/invoice.ts';

/**
 * Coded refusals (`backend/app/api/refusals.py::coded_refusal`) are stated in
 * the reader's language — and a page that BRANCHES on one keys on its code.
 *
 * The invoice modal's stale-edit prompt used to fire on
 * `err.message.includes('modified since you loaded it')`. Once the transport
 * localizes the refusal (`api/codedRefusals.ts`), that English substring is
 * absent in every other locale, so the reload prompt would silently stop
 * appearing for a German operator and their save would read as a generic
 * failure. This pins the branch in `de`: the PATCH's 409 carries the code, the
 * modal offers the reload in German, and declining it leaves the German toast.
 *
 * The 409 is stubbed: provoking a real one needs a second writer to land
 * between the modal's load and its save, and what is under test is the
 * client's handling of the body — the backend half of the contract is pinned
 * in `backend/tests/test_invoice_critical_path.py`.
 */

const IMMUTABLE = new Set([
	'sending_to_erp',
	'sent_to_erp',
	'posted_in_erp',
	'payment_scheduled',
	'paid',
	'done'
]);

test.describe('coded refusals are localized', () => {
	test('a de reader saving a stale invoice is offered the reload in German', async ({
		page,
		tenantSlug
	}) => {
		const headers = await authedTenantHeaders(page, tenantSlug);
		const listResp = await page.request.get(`${API_BASE}/api/invoices`, { headers });
		const listed = (await listResp.json()) as { items: Invoice[] };
		const target = listed.items.find((i) => !IMMUTABLE.has(i.status));
		expect(target, 'no editable invoice in the seed').toBeTruthy();
		const patchPath = `/api/invoices/${target!.id}`;

		let patched = false;
		await page.route(
			(url) => url.pathname === patchPath,
			async (route) => {
				if (route.request().method() !== 'PATCH') return route.fallback();
				patched = true;
				await route.fulfill({
					status: 409,
					json: {
						detail: {
							code: 'invoice_stale_edit',
							message:
								'This invoice was modified since you loaded it. Reload and reapply your changes.',
							params: {}
						}
					}
				});
			}
		);

		const dialogs: string[] = [];
		page.on('dialog', (d) => {
			dialogs.push(d.message());
			void d.dismiss();
		});

		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto(`${tenantBase(tenantSlug)}/invoices?id=${target!.id}`);
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(target!.invoice_number);

		await modal.getByRole('button', { name: 'Speichern', exact: true }).click();

		await expect.poll(() => patched).toBe(true);
		await expect.poll(() => dialogs.length).toBe(1);
		expect(dialogs[0]).toContain('Diese Rechnung wurde geändert, seit Sie sie geladen haben.');

		// Declined → the German toast, never the server's English sentence.
		const toast = page.locator('.toast-text');
		await expect(toast).toContainText('Nicht gespeichert');
		await expect(toast).not.toContainText('modified since you loaded it');
	});
});
