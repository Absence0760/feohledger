import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * /payments → History → Void — the card leg's outcome, and its remedy.
 *
 * Voiding a `virtual_card` payment has to reach the card provider, not just our
 * books: the card is bearer-spendable, so a leg that did not confirm leaves a
 * live card behind a payment the ledger calls `voided`. That leg is best-effort
 * by design (a provider outage must never block the accounting void), and its
 * outcome used to land ONLY on the `payment.voided` audit row — so a clean
 * success toast was indistinguishable from "the card is still open", and
 * nothing in the app could close it.
 *
 * The remedy sits ON the void rather than beside it (`docs/decisions.md` §96,
 * §132). `POST /api/cards/{id}/cancel` would also close the card, but it is
 * reachable on a LIVE payment, where it kills the card while the payment and
 * its invoice still claim money is in flight — two controls that both close a
 * card and leave the ledger in different states.
 *
 * The void / retry responses are driven through `page.route()` — real responses
 * the page parses — because manufacturing a card payment whose provider refused
 * a cancel would pin these assertions to whichever rows the shared e2e tenant
 * happens to hold, and would leave a live provider card behind. The permission
 * gate is asserted against the REAL backend at the bottom, where a mock would
 * prove nothing.
 */

const PAYMENT_ID = '44444444-4444-4444-4444-444444444444';
const INVOICE_ID = '55555555-5555-5555-5555-555555555555';

/** A `completed` virtual-card payment, i.e. one the History tab offers Void on. */
function cardPayment(overrides: Record<string, unknown> = {}) {
	return {
		id: PAYMENT_ID,
		correlation_id: null,
		invoice_id: INVOICE_ID,
		payment_run_id: null,
		amount: '250.00',
		method: 'virtual_card',
		status: 'completed',
		reference: 'E2E-VOIDCARD-REF',
		created_at: '2026-06-01T00:00:00Z',
		updated_at: null,
		settled_amount: null,
		settled_currency: null,
		currency: 'USD',
		vendor_name: 'E2E Card Vendor',
		invoice_number: 'E2E-VOIDCARD-001',
		card_last_four: '4242',
		card_provider: 'mock',
		card_id: null,
		...overrides
	};
}

async function mockHistory(page: import('@playwright/test').Page) {
	await page.route(/\/api\/payments\?/, (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ items: [cardPayment()], total: 1, page: 1, page_size: 20 })
		})
	);
	await page.route('**/api/payments/counts**', (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ total: 1, by_status: { completed: 1 } })
		})
	);
}

/** Answer the void with a given card verdict. */
async function mockVoid(
	page: import('@playwright/test').Page,
	body: { void_card_outcome: string | null; void_card_disposition: string | null }
) {
	await page.route(`**/api/payments/${PAYMENT_ID}/void`, (r) =>
		r.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(cardPayment({ status: 'voided', ...body }))
		})
	);
}

async function openVoidDialog(page: import('@playwright/test').Page) {
	await page.goto('/payments');
	await page.getByRole('tab', { name: 'History', exact: true }).click();
	// `exact` matters: the status filter chips carry a "Voided" label, and the
	// dialog's own confirm is "Void payment".
	await page.getByRole('button', { name: 'Void', exact: true }).first().click();
	await page.getByTestId('void-reason').fill('e2e: duplicate run');
	await page.getByTestId('void-confirm').click();
}

test.describe('/payments — the void tells you what happened to the card', () => {
	test('a failed card leg keeps the dialog open, says the card is live, and offers a retry', async ({
		page
	}) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_cancel_rejected',
			void_card_disposition: 'not_closed_retryable'
		});

		await openVoidDialog(page);

		const warning = page.getByTestId('void-card-warning');
		await expect(warning).toBeVisible();
		// The whole point: the operator is told the card is STILL SPENDABLE,
		// rather than reading a success toast and closing the dialog.
		await expect(warning).toContainText('still live');
		// The provider's own verdict, labelled — not the raw tag.
		await expect(page.getByTestId('void-card-outcome')).toContainText('refused the close');
		await expect(page.getByTestId('void-card-retry')).toBeVisible();
	});

	test('an unknown outcome tag renders raw and is still offered the retry', async ({ page }) => {
		// The backend can add a failure tag before this build's label map does.
		// It must surface as SOMETHING (the operator quotes it at support) and it
		// must still be treated as not-closed — classifying an unknown as closed
		// is the one direction that reports a live card as shut.
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'some_future_failure_mode',
			void_card_disposition: 'not_closed_retryable'
		});

		await openVoidDialog(page);

		await expect(page.getByTestId('void-card-outcome')).toContainText('some_future_failure_mode');
		await expect(page.getByTestId('void-card-retry')).toBeVisible();
	});

	test('an already-charged card is shown but NOT offered a retry that can only fail', async ({
		page
	}) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_already_charged',
			void_card_disposition: 'not_closed_final'
		});

		await openVoidDialog(page);

		await expect(page.getByTestId('void-card-warning')).toContainText('already been charged');
		await expect(page.getByTestId('void-card-retry')).toHaveCount(0);
		// It is still an outcome the operator must see — closing silently here
		// would hide a card that was never shut.
		await expect(page.getByTestId('void-card-close')).toBeVisible();
	});

	test('a clean close reports nothing at all — the dialog just closes', async ({ page }) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_cancelled',
			void_card_disposition: 'closed'
		});

		await openVoidDialog(page);

		await expect(page.getByTestId('void-card-warning')).toHaveCount(0);
		await expect(page.getByTestId('void-card-retry')).toHaveCount(0);
	});

	test('a non-card payment reports nothing — the leg never ran', async ({ page }) => {
		// `null` is not `closed`: "we never asked" must not be dressed up as a
		// reassuring outcome (decisions §34). Here it means there was no card.
		await mockHistory(page);
		await mockVoid(page, { void_card_outcome: null, void_card_disposition: null });

		await openVoidDialog(page);

		await expect(page.getByTestId('void-card-warning')).toHaveCount(0);
	});
});

test.describe('/payments — the retry finishes the void', () => {
	test('retrying posts to the void-scoped endpoint and confirms the close', async ({ page }) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_cancel_error:ReadTimeout',
			void_card_disposition: 'not_closed_retryable'
		});

		let retryUrl: string | null = null;
		await page.route(`**/api/payments/${PAYMENT_ID}/void/retry-card-cancel`, async (route) => {
			retryUrl = route.request().url();
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(
					cardPayment({
						status: 'voided',
						void_card_outcome: 'card_cancelled',
						void_card_disposition: 'closed'
					})
				)
			});
		});

		await openVoidDialog(page);
		await page.getByTestId('void-card-retry').click();

		// The retry hits the VOID-scoped endpoint, never `/api/cards/{id}/cancel`
		// — the standalone control is reachable on a live payment and would leave
		// the ledger and the card disagreeing (decisions §96).
		await expect
			.poll(() => retryUrl)
			.toContain(`/api/payments/${PAYMENT_ID}/void/retry-card-cancel`);
		await expect(page.getByTestId('void-card-closed')).toBeVisible();
		await expect(page.getByTestId('void-card-retry')).toHaveCount(0);
	});

	test('a retry that still fails says so and stays retryable', async ({ page }) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_cancel_rejected',
			void_card_disposition: 'not_closed_retryable'
		});
		await page.route(`**/api/payments/${PAYMENT_ID}/void/retry-card-cancel`, (r) =>
			r.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(
					cardPayment({
						status: 'voided',
						void_card_outcome: 'card_cancel_rejected',
						void_card_disposition: 'not_closed_retryable'
					})
				)
			})
		);

		await openVoidDialog(page);
		await page.getByTestId('void-card-retry').click();

		// The failure stays ON SCREEN rather than fading in a toast: the card is
		// still open, and the operator has to be able to try again.
		await expect(page.getByTestId('void-card-retry-failed')).toBeVisible();
		await expect(page.getByTestId('void-card-retry')).toBeEnabled();
	});

	test('a backend refusal is surfaced, not swallowed', async ({ page }) => {
		await mockHistory(page);
		await mockVoid(page, {
			void_card_outcome: 'card_cancel_rejected',
			void_card_disposition: 'not_closed_retryable'
		});
		await page.route(`**/api/payments/${PAYMENT_ID}/void/retry-card-cancel`, (r) =>
			r.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify({
					detail: "Only a voided payment's card close can be retried"
				})
			})
		);

		await openVoidDialog(page);
		await page.getByTestId('void-card-retry').click();

		await expect(page.getByTestId('void-card-retry-failed')).toBeVisible();
	});
});

test.describe('/payments — the card-close retry is gated server-side too', () => {
	// The UI gate is a courtesy; this asserts the REAL backend refuses a role
	// without `payment.void`, so hiding the control is never the only lock. It
	// deliberately gates on `payment.void` — the permission of the void it
	// completes — and NOT on the card router's bare roles, which would let an
	// org that split the duties reach the reversal's other half.
	test('clerk gets 403 on the void card-cancel retry', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const resp = await page.request.post(
			`${API_BASE}/api/payments/00000000-0000-0000-0000-000000000000/void/retry-card-cancel`,
			{ headers: await authedTenantHeaders(page), data: {} }
		);
		expect(resp.status()).toBe(403);
	});
});
