import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { resolveDialog } from './dialogs';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — the record dialog over a payment the ERP split across rails
 * (decisions §396, §413).
 *
 * Whether a split stands is the SERVER's verdict (`erp_payment_split_stands`),
 * because it is an exact sum the client may not do. The verdict has three
 * states and the dialog must keep them apart:
 *
 *  - `true`: the split is the record, so the rail picker becomes a read-only
 *    line and the body sends no `method`;
 *  - `false`: the server judged the parts and they do not add up, so the note
 *    says so and the picker stays (the chosen rail is the record);
 *  - `null` / absent: nobody judged (no stated figure, parts the server's
 *    stricter parser rejects, an older server). The note must not claim the
 *    parts fail to add up, and the picker stays.
 *
 * Everything is stubbed so each verdict is exact; the end-to-end booking
 * against a real backend is `tests-e2e/payments/erp-reported-payment.spec.ts`.
 */

const EXC_ID = '00000000-0000-4000-9413-000000000001';
const INVOICE = 'E2E-SPLIT-1';

function splitRow(stands: boolean | null | undefined) {
	const row: Record<string, unknown> = {
		id: EXC_ID,
		invoice_id: '00000000-0000-4000-9413-0000000000aa',
		invoice_number: INVOICE,
		vendor_name: 'Split Vendor',
		amount: '150.00',
		currency: 'USD',
		exception_type: 'payment_reconciliation',
		type_label: 'Payment Reconciliation',
		severity: 'error',
		description: 'stubbed §413',
		status: 'open',
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z',
		decision_refusals: { resolve: null, dismiss: null, escalate: null },
		erp_report: {
			kind: 'paid_not_recorded',
			erp_type: 'quickbooks_online',
			erp_document_id: 'BILL-SPLIT',
			event_id: null,
			method: null,
			reference: 'BILL-SPLIT',
			paid_on: '2026-10-01',
			paid_on_reported: true,
			amount: '150.00',
			amount_currency: 'USD',
			payment_parts: [
				{ method: 'check', amount: '100.00' },
				{ method: 'card', amount: '50.00' }
			],
			refusal_code: 'external_payment_in_run'
		},
		erp_name: 'QuickBooks Online',
		record_erp_payment_refusal: null
	};
	// `undefined` is the older server: the key is absent, not null.
	if (stands !== undefined) row.erp_payment_split_stands = stands;
	return row;
}

/** Stub the queue around one exception, opened by `?id=`, and record every
 *  body posted to its record endpoint. */
async function stubSplitException(page: Page, stands: boolean | null | undefined) {
	const row = splitRow(stands);
	const posted: Record<string, unknown>[] = [];
	await page.route(
		(url) => url.pathname.startsWith('/api/exceptions'),
		async (route) => {
			const req = route.request();
			const path = new URL(req.url()).pathname;
			if (req.method() === 'OPTIONS') return route.fallback();
			if (path === '/api/exceptions/summary') {
				return route.fulfill({
					json: exceptionSummary({
						open: 1,
						by_type: { payment_reconciliation: 1 },
						by_severity: { error: 1 }
					})
				});
			}
			if (path === '/api/exceptions') {
				return route.fulfill({ json: { items: [row], total: 1, page: 1, page_size: 20 } });
			}
			if (path === `/api/exceptions/${EXC_ID}` && req.method() === 'GET') {
				return route.fulfill({ json: row });
			}
			if (path === `/api/exceptions/${EXC_ID}/record-erp-payment`) {
				posted.push(req.postDataJSON() as Record<string, unknown>);
				return route.fulfill({ status: 201, json: { id: 'stub-payment' } });
			}
			return route.fallback();
		}
	);
	return posted;
}

async function openPanel(page: Page) {
	await page.goto(`/exceptions?id=${EXC_ID}`);
	await expect(resolveDialog(page)).toBeVisible();
	const panel = page.getByTestId('erp-payment-panel');
	await expect(panel).toBeVisible();
	await expect(panel.getByTestId('erp-payment-split-part')).toHaveCount(2);
	return panel;
}

test.describe('/exceptions — the record dialog over an ERP split', () => {
	test('a split that stands replaces the picker and the record names no rail', async ({ page }) => {
		const posted = await stubSplitException(page, true);
		const panel = await openPanel(page);

		await expect(panel.getByTestId('erp-payment-split-note')).toHaveText(
			'This payment was made with more than one method, so it is recorded with this split. One method cannot describe it.'
		);
		await expect(panel.getByTestId('erp-payment-method')).toHaveCount(0);
		await expect(panel.getByTestId('erp-payment-method-fixed')).toHaveText(
			'Split across the methods above'
		);

		await panel.getByTestId('erp-payment-submit').click();
		await expect(panel).toBeHidden();
		expect(posted).toHaveLength(1);
		expect(posted[0]).not.toHaveProperty('method');
		expect(posted[0]).toMatchObject({ reference: 'BILL-SPLIT', paid_on: '2026-10-01' });
	});

	test('a split the server judged not to add up keeps the picker and sends the rail', async ({
		page
	}) => {
		const posted = await stubSplitException(page, false);
		const panel = await openPanel(page);

		await expect(panel.getByTestId('erp-payment-split-note')).toHaveText(
			'These parts do not add up to the amount paid, so the split is not kept. The method you choose is recorded for the whole payment.'
		);
		await expect(panel.getByTestId('erp-payment-method-fixed')).toHaveCount(0);
		const picker = panel.getByTestId('erp-payment-method');
		await expect(picker).toBeVisible();
		await picker.selectOption('check');

		await panel.getByTestId('erp-payment-submit').click();
		await expect(panel).toBeHidden();
		expect(posted).toHaveLength(1);
		expect(posted[0]).toMatchObject({ method: 'check' });
	});

	for (const [label, stands] of [
		['null', null],
		['absent (an older server)', undefined]
	] as const) {
		test(`an unjudged split (${label}) claims nothing about the sum and keeps the picker`, async ({
			page
		}) => {
			const posted = await stubSplitException(page, stands);
			const panel = await openPanel(page);

			const note = panel.getByTestId('erp-payment-split-note');
			await expect(note).toHaveText(
				'Whether this split is kept is decided when the payment is recorded. If it is not kept, the method you choose is recorded for the whole payment.'
			);
			// Nobody checked the sum, so the dialog may not say it fails.
			await expect(note).not.toContainText('do not add up');
			await expect(panel.getByTestId('erp-payment-method-fixed')).toHaveCount(0);
			const picker = panel.getByTestId('erp-payment-method');
			await expect(picker).toBeVisible();
			await picker.selectOption('card');

			await panel.getByTestId('erp-payment-submit').click();
			await expect(panel).toBeHidden();
			expect(posted).toHaveLength(1);
			expect(posted[0]).toMatchObject({ method: 'card' });
		});
	}
});
