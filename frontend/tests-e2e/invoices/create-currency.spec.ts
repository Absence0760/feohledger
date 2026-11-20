import { expect, test } from '../fixtures/helpers';

/**
 * The Create Invoice modal's currency is `ui/CurrencyInput`, not a bare text
 * input. A bare input sent a blank or `ÄÖÜ` straight to the backend's
 * `CurrencyCode`, which came back as a raw 422 with no inline reason
 * (followups (c), "Seven create forms take a currency as bare text").
 *
 * The org lookup is stubbed to GBP so the seeding assertion doesn't depend on
 * what the shard's tenant is configured with; the create POST is never sent.
 */
test('the create-invoice currency seeds from the org, uppercases, and refuses a bad code inline', async ({
	page
}) => {
	await page.route('**/api/organization', (route) =>
		route.request().method() === 'GET'
			? route.fulfill({
					json: { settings: { reporting_currency: 'GBP' }, resolved_reporting_currency: 'GBP' }
				})
			: route.continue()
	);
	let posted = false;
	await page.route('**/api/invoices', (route) => {
		if (route.request().method() === 'POST') posted = true;
		return route.continue();
	});

	await page.goto('/invoices');
	await page.getByRole('button', { name: 'Create Invoice' }).click();
	const modal = page.locator('div.modal[role="dialog"][aria-label="Create Invoice"]');
	await expect(modal).toBeVisible();

	const currency = modal.getByTestId('invoice-currency');
	await expect(currency).toHaveValue('GBP');
	// Marked required like Vendor / Invoice # / Amount, visually and to AT.
	await expect(
		modal.locator('label', { hasText: 'Currency' }).locator('em.required')
	).toHaveText('*');
	await expect(currency).toHaveAttribute('required', '');

	await currency.fill('pln');
	await expect(currency).toHaveValue('PLN');
	await expect(modal.getByTestId('invoice-currency-error')).toHaveCount(0);

	await currency.fill('');
	await expect(modal.getByTestId('invoice-currency-error')).toHaveText(
		'Enter the currency as a three-letter ISO 4217 code, such as USD or PLN.'
	);
	await expect(currency).toHaveAttribute('aria-invalid', 'true');

	// Native validity blocks the submit: nothing reaches the backend.
	await modal.locator('label', { hasText: 'Vendor' }).locator('input').fill('E2E Ccy Vendor');
	await modal.locator('label', { hasText: 'Invoice #' }).locator('input').fill('E2E-CCY-1');
	await modal.locator('label', { hasText: 'Amount' }).locator('input').fill('10.00');
	// Every other required field is filled, so the blank currency alone keeps
	// Create disabled — and nothing reaches the backend.
	const create = modal.getByRole('button', { name: 'Create', exact: true });
	await expect(create).toBeDisabled();
	await expect(modal).toBeVisible();
	expect(posted).toBe(false);

	// A valid code re-enables it: the currency was the only thing missing.
	await currency.fill('GBP');
	await expect(create).toBeEnabled();
	await expect(currency).not.toHaveAttribute('aria-invalid', 'true');
	expect(posted).toBe(false);
});
