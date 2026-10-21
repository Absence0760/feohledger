import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * `RunDetailModal` must render every money cell in the currency the SERVER
 * stated — or in none at all.
 *
 * All five `fmt()` call sites in the dialog (run total, each payment's amount,
 * the Execute button, the armed note, the Confirm button) omitted the currency
 * argument the helper already accepted, so `formatMoney` fell back to
 * `DEFAULT_CURRENCY` and a EUR run was labelled `$` for every tenant — worse
 * than the `/payments` bug `docs/decisions.md` §107 fixed, because it ignored
 * the org's own default too. `GET /api/payments/runs/{id}` has carried
 * `currency` on the run AND on each payment since that work; nothing read it.
 *
 * The assertions are on the currency **SYMBOL**, never the digits: `$500.00`
 * and `€500.00` are indistinguishable to a digits-only assertion, which is
 * exactly how this shipped unseen (§107, "the guard is the assertion, not the
 * field"). EUR is used deliberately — a fixture in the tenant's own currency
 * makes the wrong rendering look right.
 *
 * Both responses are stubbed. The subject is what the dialog does with the
 * contract, and the alternative — seeding a EUR invoice, a run over it, and a
 * legacy run whose legs disagree (which `create_payment_run_for_invoices` now
 * refuses outright) — would leave money rows in the shared worker tenant to
 * prove something this pins directly.
 */

const RUNS_LIST_PATH = '/api/payments/runs/';
// Fixed ids: the Runs row renders `id.slice(0, 8)`, which is the accessible
// name the dialog is opened by, and the detail route is stubbed on the full id.
const EUR_RUN_ID = 'e0114a70-0000-4000-8000-0000000000e1';
const BARE_RUN_ID = 'ba7e0000-0000-4000-8000-0000000000b1';

/** One row for the Runs tab, shaped like `PaymentRunResponse`. */
function runListRow(id: string, currency: string | null) {
	return {
		id,
		status: 'draft',
		total_amount: '1200.00',
		currency,
		executed_at: null,
		created_at: '2026-09-01T10:00:00Z',
		payment_count: 2
	};
}

/** The detail payload, shaped like `GET /api/payments/runs/{id}`. */
function runDetail(id: string, currency: string | null, legCurrency: string | null) {
	return {
		id,
		status: 'draft',
		total_amount: '1200.00',
		currency,
		initiated_by: null,
		executed_at: null,
		created_at: '2026-09-01T10:00:00Z',
		requires_cfo_approval: false,
		cfo_approved_by: null,
		cfo_approved_at: null,
		payment_count: 2,
		// The caller's verdicts (`run_action_refusals`): everything open, so the
		// footer renders Execute and Cancel.
		actions: { approve: null, execute: null, cancel: null },
		payments: [
			{
				id: `${id}-leg-1`,
				invoice_id: `${id}-inv-1`,
				invoice_number: 'RUNCUR-1',
				vendor_name: 'Currency Fixture Ltd',
				amount: '700.00',
				currency: legCurrency,
				method: 'ach',
				status: 'pending',
				reference: null
			},
			{
				id: `${id}-leg-2`,
				invoice_id: `${id}-inv-2`,
				invoice_number: 'RUNCUR-2',
				vendor_name: 'Currency Fixture Ltd',
				amount: '500.00',
				currency: legCurrency,
				method: 'ach',
				status: 'pending',
				reference: null
			}
		]
	};
}

/** Stub the Runs tab list + this run's detail, and open the dialog. */
async function openRun(
	page: Page,
	id: string,
	currency: string | null,
	legCurrency: string | null
) {
	await page.route(
		(url) => url.pathname === RUNS_LIST_PATH,
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [runListRow(id, currency)], total: 1, page: 1, page_size: 100 })
			})
	);
	await page.route(
		(url) => url.pathname === `${RUNS_LIST_PATH}${id}`,
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(runDetail(id, currency, legCurrency))
			})
	);

	await page.goto('/payments?tab=runs');
	await page
		.getByRole('button', { name: `View payment run ${id.slice(0, 8)}` })
		.click();

	const dialog = page.getByRole('dialog', { name: 'Payment run' });
	await expect(dialog).toBeVisible({ timeout: 10_000 });
	return dialog;
}

test.describe('RunDetailModal — money carries the currency the server stated', () => {
	test('a EUR run renders EUR in every money cell, never the org default', async ({ page }) => {
		const dialog = await openRun(page, EUR_RUN_ID, 'EUR', 'EUR');

		// Shape, not an exact glyph: Intl may render EUR as "€" or "EUR"
		// depending on the runtime's locale data, but it can never render it as
		// a dollar sign — and the seeded tenants report in USD, so a `$` here is
		// the bug.
		const total = dialog.getByTestId('run-total');
		await expect(total).toContainText(/€|EUR/);
		await expect(total).not.toContainText('$');

		// Every leg, not just the first: `p.currency` is a per-row field.
		const amounts = dialog.getByTestId('run-payment-amount');
		await expect(amounts).toHaveCount(2);
		for (let i = 0; i < 2; i += 1) {
			await expect(amounts.nth(i)).toContainText(/€|EUR/);
			await expect(amounts.nth(i)).not.toContainText('$');
		}

		// The button that moves the money prints the amount in its label — the
		// most consequential of the five call sites.
		const execute = dialog.getByRole('button', { name: /^Execute · / });
		await expect(execute).toContainText(/€|EUR/);
		await expect(execute).not.toContainText('$');

		// Arming reveals the other two: the alert note and the Confirm label.
		// (First click only — the second is what sends money to the processor.)
		await execute.click();
		const armed = dialog.getByTestId('execute-armed-note');
		await expect(armed).toBeVisible();
		await expect(armed).toContainText(/€|EUR/);
		await expect(armed).not.toContainText('$');

		const confirm = dialog.getByRole('button', { name: /^Confirm execute · / });
		await expect(confirm).toContainText(/€|EUR/);
		await expect(confirm).not.toContainText('$');
	});

	test('a run whose currency the server could not prove renders bare figures', async ({
		page
	}) => {
		// `null` is what `_one_currency` returns for a legacy run whose legs
		// disagree — its total is denominated in nothing real. Stamping the org
		// default on it would be the fabrication the wire field exists to end
		// (`docs/decisions.md` §79/§82).
		const dialog = await openRun(page, BARE_RUN_ID, null, null);

		const total = dialog.getByTestId('run-total');
		await expect(total).toContainText('1,200');
		await expect(total).not.toContainText('$');
		await expect(total).not.toContainText(/€|EUR|USD/);

		const amounts = dialog.getByTestId('run-payment-amount');
		await expect(amounts.first()).toContainText('700');
		await expect(amounts.first()).not.toContainText('$');
		await expect(amounts.first()).not.toContainText(/€|EUR|USD/);

		const execute = dialog.getByRole('button', { name: /^Execute · / });
		await expect(execute).toContainText('1,200');
		await expect(execute).not.toContainText('$');
	});
});
