import { expect, test } from '../fixtures/helpers';

/**
 * /payments → Cards tab → Reveal.
 *
 * Regression: the client typed `GET /api/cards/{id}/details` as the
 * SUPPLIER-PORTAL reveal's shape (`{pan, cvv, expires_at, last_four}`). The AP
 * route declares `response_model=CardDetailsResponse` =
 * `{card_number, exp_month, exp_year, cvv}` (`backend/app/schemas/virtual_card.py`)
 * and FastAPI strips anything the model doesn't declare — so only `cvv` lined
 * up. The dialog rendered a BLANK card number and no expiry, which is a
 * complete failure of the one thing the dialog exists to do.
 *
 * The fix is client-side by design: `backend/app/api/portal.py` carries a
 * comment recording that reading `details.pan` was a prior break, so the schema
 * must not be widened to suit the client.
 *
 * Both the list and the details endpoint are route-stubbed so the spec asserts
 * the CONTRACT (the real response_model's field names) deterministically,
 * without needing an issued card from a provider round trip.
 */

const CARD_ID = '11111111-2222-3333-4444-555555555555';

const CARD_ROW = {
	id: CARD_ID,
	invoice_id: '99999999-8888-7777-6666-555555555555',
	card_provider: 'mock',
	provider_card_id: 'mock_card_1',
	last_four: '4242',
	amount_limit: '500.00',
	amount_charged: null,
	currency: 'USD',
	status: 'active',
	expires_at: '2030-01-31',
	merchant_name: null,
	decline_reason: null,
	created_at: '2026-01-02T00:00:00Z',
	vendor_name: 'Stub Vendor Co',
	invoice_number: 'CARD-REVEAL-1'
};

// EXACTLY what `CardDetailsResponse` declares — no `pan`, no `expires_at`,
// no `last_four`. If the client goes back to reading those it renders blanks.
const CARD_DETAILS = {
	card_number: '4111111111114242',
	exp_month: 7,
	exp_year: 2030,
	cvv: '123'
};

test.describe('/payments virtual-card reveal', () => {
	test('renders the card number and expiry from the real CardDetailsResponse shape', async ({
		page
	}) => {
		await page.route('**/api/cards?**', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [CARD_ROW], total: 1, page: 1, page_size: 20 })
			})
		);
		await page.route('**/api/cards/dashboard', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					active_cards: 1,
					active_cards_value: '500.00',
					spend_this_month: '0',
					rebates_this_month: '0',
					rebates_ytd: '0',
					projected_annual_rebates: '0',
					rebates_this_month_by_status: {
						pending_total: '0',
						confirmed_total: '0',
						paid_out_total: '0'
					},
					rebates_ytd_by_status: {
						pending_total: '0',
						confirmed_total: '0',
						paid_out_total: '0'
					}
				})
			})
		);
		await page.route(`**/api/cards/${CARD_ID}/details`, (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(CARD_DETAILS)
			})
		);

		await page.goto('/payments');
		await page.getByRole('tab', { name: 'Cards', exact: true }).click();

		const row = page.getByRole('row', { name: /CARD-REVEAL-1/ });
		await expect(row).toBeVisible();

		await row.getByRole('button', { name: 'Reveal' }).click();

		const dialog = page.getByRole('dialog', { name: 'Card details' });
		await expect(dialog).toBeVisible();

		// The whole point: a real PAN, not an empty span.
		await expect(dialog.getByTestId('card-details-number')).toHaveText('4111111111114242');
		await expect(dialog.getByTestId('card-details-cvv')).toHaveText('123');
		// exp_month/exp_year → the MM/YYYY printed on the card face (zero-padded).
		await expect(dialog.getByTestId('card-details-expires')).toHaveText('07/2030');
	});

	test('the dialog never renders an empty card number or expiry', async ({ page }) => {
		// Guard against the specific failure mode: an `undefined` field renders as
		// an EMPTY element, which is visible but says nothing — the exact state the
		// old client shipped. Asserting non-empty catches a future field rename
		// even if the value itself changes.
		await page.route('**/api/cards?**', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [CARD_ROW], total: 1, page: 1, page_size: 20 })
			})
		);
		await page.route('**/api/cards/dashboard', (route) => route.fulfill({ status: 404, body: '{}' }));
		await page.route(`**/api/cards/${CARD_ID}/details`, (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(CARD_DETAILS)
			})
		);

		await page.goto('/payments');
		await page.getByRole('tab', { name: 'Cards', exact: true }).click();
		await page.getByRole('row', { name: /CARD-REVEAL-1/ }).getByRole('button', { name: 'Reveal' }).click();

		const dialog = page.getByRole('dialog', { name: 'Card details' });
		await expect(dialog.getByTestId('card-details-number')).not.toHaveText('');
		await expect(dialog.getByTestId('card-details-expires')).not.toHaveText('');
		// `formatDate(undefined)` used to leave this blank; a literal "—" would
		// also be a failure — we have the month + year, so we can always print it.
		await expect(dialog.getByTestId('card-details-expires')).not.toHaveText('—');
	});
});
