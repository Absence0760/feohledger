import type { Page, Route } from '@playwright/test';
import { expect, invoicePicker, selectInvoiceInPicker, test } from '../fixtures/helpers';
import type { CardGenerateResponse } from '#lib/types/cardGenerate.ts';

/**
 * The payments Cards tab's **Generate card** action (`POST /api/cards/generate`).
 *
 * The endpoint answered for a long time with no screen calling it, and since
 * decisions §342 it names every invoice it minted nothing for. This pins what
 * the browser does with that answer:
 *
 *  - offered only to a holder of `payment.execute` on a tenant FeohLedger
 *    dispatches for (the endpoint's own first two gates);
 *  - invoices are picked through the shared `ui/InvoicePicker`, searched
 *    server-side under the payable statuses, and posted as `invoice_ids`;
 *  - the result states each minted card AND each skip, localized from its code;
 *  - a whole-call refusal (`cards_not_enabled`) is stated in the dialog in the
 *    reader's language, and nothing the reader picked is lost.
 *
 * Every read is stubbed: the subject is the dialog's contract with the server.
 * The gates themselves are pytest's (`test_card_generate_gates.py`).
 */

const INVOICES = [
	{ id: '0c000000-0000-4000-8000-0000000000a1', invoice_number: 'GEN-CARD-1', amount: '120.00', currency: 'USD' },
	{ id: '0c000000-0000-4000-8000-0000000000a2', invoice_number: 'GEN-CARD-2', amount: '80.50', currency: 'USD' }
];

async function json(route: Route, body: unknown, status = 200) {
	await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stubCardsTab(page: Page, mode: 'processor' | 'record_only' = 'processor') {
	await page.route(
		(url) => url.pathname === '/api/payments/execution-mode',
		(route) => json(route, { mode, reason: 'configured' })
	);
	await page.route(
		(url) => url.pathname === '/api/cards/rebates',
		(route) =>
			json(route, { items: [], total: 0, page: 1, page_size: 20, total_amount: null, currency: 'USD' })
	);
	await page.route(
		(url) => url.pathname === '/api/cards',
		(route) => json(route, { items: [], total: 0, page: 1, page_size: 20 })
	);
	await page.route(
		(url) => url.pathname === '/api/cards/dashboard',
		(route) => json(route, { detail: 'not under test' }, 500)
	);
	await page.route(
		(url) => url.pathname === '/api/invoices',
		(route) => {
			const url = new URL(route.request().url());
			const search = (url.searchParams.get('search') ?? '').toLowerCase();
			const items = INVOICES.filter((i) => i.invoice_number.toLowerCase().includes(search));
			return json(route, { items, total: items.length, page: 1, page_size: 25 });
		}
	);
}

async function stubPermissions(page: Page, drop: string[]) {
	await page.route(
		(url) => url.pathname === '/api/auth/me',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			const permissions = (body.permissions ?? []).filter((p: string) => !drop.includes(p));
			await route.fulfill({ response: real, json: { ...body, permissions } });
		}
	);
}

async function openCards(page: Page) {
	// The button also waits on the execution mode, which loads on its own: an
	// absence assertion taken before it lands would pass with the gate broken.
	const mode = page.waitForResponse(
		(r) => new URL(r.url()).pathname === '/api/payments/execution-mode'
	);
	await page.goto('/payments');
	await mode;
	await page.getByRole('tab', { name: 'Cards', exact: true }).click();
	// The tab's own table, so a negative assertion below has something drawn first.
	await expect(page.getByTestId('cards-table').getByTestId('table-empty')).toHaveText(
		'No virtual cards issued yet.'
	);
}

async function addInvoice(page: Page, number: string) {
	const dialog = page.getByRole('dialog', { name: 'Generate virtual cards' });
	await selectInvoiceInPicker(invoicePicker(dialog, 'Invoice'), number);
	await dialog.getByTestId('card-generate-add').click();
	await expect(dialog.getByTestId('card-generate-chosen')).toContainText(number);
}

test.describe('/payments Cards tab — Generate card', () => {
	test('mints for the picked invoices and states every card and every skip', async ({ page }) => {
		await stubCardsTab(page);
		const posted: unknown[] = [];
		const answer: CardGenerateResponse = {
			items: [
				{
					id: 'card-1',
					invoice_id: INVOICES[0].id,
					vendor_id: null,
					card_provider: 'mock',
					last_four: '4242',
					amount_limit: '120.00',
					currency: 'USD',
					status: 'created',
					expires_at: null
				}
			],
			total: 1,
			skipped: [
				{
					invoice_id: INVOICES[1].id,
					code: 'card_skip_blocked',
					message: 'server English',
					params: { exception_type: 'duplicate' }
				}
			]
		};
		await page.route(
			(url) => url.pathname === '/api/cards/generate',
			(route) => {
				posted.push(route.request().postDataJSON());
				return json(route, answer, 201);
			}
		);

		await openCards(page);
		await page.getByTestId('cards-generate').click();
		const dialog = page.getByRole('dialog', { name: 'Generate virtual cards' });
		await expect(dialog).toBeVisible();
		const submit = dialog.getByTestId('card-generate-submit');
		await expect(submit).toBeDisabled();

		await addInvoice(page, 'GEN-CARD-1');
		await addInvoice(page, 'GEN-CARD-2');
		await expect(submit).toHaveText('Generate 2 cards');

		const reload = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/cards');
		await submit.click();

		const result = dialog.getByTestId('card-generate-result');
		await expect(result).toBeVisible();
		expect(posted).toEqual([{ invoice_ids: [INVOICES[0].id, INVOICES[1].id] }]);
		await expect(result.getByText('1 card issued')).toBeVisible();
		await expect(result.getByTestId('card-generate-minted')).toContainText('GEN-CARD-1');
		await expect(result.getByTestId('card-generate-minted')).toContainText('•••• 4242');
		await expect(result.getByText('1 invoice got no card')).toBeVisible();
		const skip = result.getByTestId('card-generate-skipped').locator('li');
		await expect(skip).toHaveAttribute('data-code', 'card_skip_blocked');
		await expect(skip).toContainText('GEN-CARD-2');
		// Localized from the code and its params, never the server's English.
		await expect(skip).toContainText('An open Duplicate Invoice exception blocks payment of this invoice.');
		await expect(skip).not.toContainText('server English');
		// A minted card re-reads the list behind the dialog.
		await reload;

		// The result is announced, and focus lands on its own control rather
		// than falling back to the page behind the dialog.
		await expect(result).toHaveAttribute('role', 'status');
		const done = dialog.getByRole('button', { name: 'Done' });
		await expect(done).toBeFocused();
		await done.click();
		await expect(dialog).toHaveCount(0);
	});

	test('a call that fails mid-way says cards may exist and re-reads the list', async ({ page }) => {
		await stubCardsTab(page);
		await page.route(
			(url) => url.pathname === '/api/cards/generate',
			(route) => json(route, { detail: 'Internal Server Error' }, 500)
		);

		await openCards(page);
		await page.getByTestId('cards-generate').click();
		const dialog = page.getByRole('dialog', { name: 'Generate virtual cards' });
		await addInvoice(page, 'GEN-CARD-1');
		// The mint commits per invoice, so a 5xx is not "nothing was issued".
		const reload = page.waitForRequest((r) => new URL(r.url()).pathname === '/api/cards');
		await dialog.getByTestId('card-generate-submit').click();

		await expect(dialog.getByTestId('card-generate-error')).toHaveText(
			'The request did not finish cleanly, so some cards may have been issued. The card list has been refreshed; check it before trying again.'
		);
		await reload;
	});

	test('a switched-off card programme is stated in place and keeps the selection', async ({
		page
	}) => {
		await stubCardsTab(page);
		await page.route(
			(url) => url.pathname === '/api/cards/generate',
			(route) =>
				json(
					route,
					{
						detail: {
							code: 'cards_not_enabled',
							message: 'Virtual cards are not enabled. Configure in Organization Settings.',
							params: {}
						}
					},
					400
				)
		);

		await openCards(page);
		await page.getByTestId('cards-generate').click();
		const dialog = page.getByRole('dialog', { name: 'Generate virtual cards' });
		await addInvoice(page, 'GEN-CARD-1');
		await dialog.getByTestId('card-generate-submit').click();

		const error = dialog.getByTestId('card-generate-error');
		await expect(error).toHaveAttribute('role', 'alert');
		await expect(error).toHaveText(
			'Virtual cards are not enabled. Turn them on in Organization settings.'
		);
		await expect(dialog.getByTestId('card-generate-chosen')).toContainText('GEN-CARD-1');
	});

	test('is not offered on a tenant whose payments are recorded, not sent', async ({ page }) => {
		await stubCardsTab(page, 'record_only');
		await openCards(page);
		await expect(page.getByTestId('cards-generate')).toHaveCount(0);
	});

	test('is not offered to a reader without payment.execute', async ({ page }) => {
		await stubCardsTab(page);
		await stubPermissions(page, ['payment.execute']);
		await openCards(page);
		await expect(page.getByTestId('cards-generate')).toHaveCount(0);
	});
});
