import type { Page } from '@playwright/test';
import type { CorporateCardTransaction, ExpenseReport } from '#lib/types/expense.ts';
import { expect, test } from '../fixtures/helpers';

/**
 * /expenses offers report and card-transaction actions from the server's
 * verdicts (`actions`), never a restated role / status / owner check
 * (`frontend/docs/ui-patterns.md` § Whether to offer an action).
 *
 * Stubbed: each test hands the page the verdicts it must obey, so what is
 * asserted is the page's reading of them — the parity between a verdict and
 * its endpoint is `backend/tests/test_expense_action_verdicts.py`'s job. The
 * stubs match the exact API pathname (`tests-e2e/README.md` § Stubbing).
 */

const REPORT_ID = '00000000-0000-0000-0000-0000000000e1';
const OWNER_ID = '00000000-0000-0000-0000-0000000000e2';

function report(actions: ExpenseReport['actions'], status = 'submitted'): ExpenseReport {
	return {
		id: REPORT_ID,
		report_number: 'RPT-VERDICT-1',
		title: 'Verdicts',
		employee_user_id: OWNER_ID,
		status,
		submitted_at: null,
		approved_at: null,
		approved_by: null,
		total_amount: 7500,
		total_amount_exact: '7500.00',
		currency: 'USD',
		reporting_amount: '7500.00',
		reporting_currency: 'USD',
		reporting_fx_rate: '1',
		reporting_fx_locked_at: null,
		notes: null,
		expenses: [],
		created_at: '2026-06-01T00:00:00Z',
		updated_at: '2026-06-01T00:00:00Z',
		actions
	};
}

async function stubReport(page: Page, row: ExpenseReport) {
	await page.route(
		(url) => url.pathname === '/api/expense-reports',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [row], total: 1, page: 1, page_size: 50 })
			})
	);
	await page.route(
		(url) => url.pathname === `/api/expense-reports/${REPORT_ID}`,
		(route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(row) })
	);
	await page.route(
		(url) => url.pathname === `/api/expense-reports/${REPORT_ID}/summary`,
		(route) => route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"x"}' })
	);
}

async function openReport(page: Page) {
	await page.goto('/expenses?tab=reports');
	await page.getByRole('button', { name: 'Open report RPT-VERDICT-1' }).click();
	await expect(page.locator('.report-title-block h2')).toHaveText('RPT-VERDICT-1');
}

const CFO_REFUSAL = {
	code: 'expense_cfo_required',
	message: 'Report total 7500.00 USD exceeds 5000. CFO approval required.',
	params: { amount: '7500.00', currency: 'USD', limit: '5000', report_currency: 'USD' }
};
const SEGREGATION = {
	code: 'approval_segregation',
	message: 'Segregation of duties: a user involved in creating this invoice cannot also approve it.',
	params: {}
};
const SELF_REJECT = {
	code: 'expense_report_self_reject',
	message:
		'Segregation of duties: you cannot reject your own expense report. Another reviewer must decide it.',
	params: {}
};
const NOT_PERMITTED = {
	code: 'role_not_permitted',
	message: 'Your role does not permit this action.',
	params: {}
};

test.describe('expense report controls read the server verdicts', () => {
	test('the CFO gate is stated beside a disabled Approve; Reject stays offered', async ({
		page
	}) => {
		await stubReport(page, report({ submit: null, approve: CFO_REFUSAL, reject: null, compose: null }));
		await openReport(page);
		await expect(page.getByRole('button', { name: 'Approve', exact: true })).toBeDisabled();
		await expect(page.locator('#approve-refusal')).toContainText('CFO');
		await expect(page.getByRole('button', { name: 'Reject', exact: true })).toBeEnabled();
	});

	test("the owner's own report offers neither Approve nor Reject", async ({ page }) => {
		// Segregation is not explained beside a control — it is the reader's own
		// claim — so both decisions are hidden, as the page always did.
		await stubReport(
			page,
			report({ submit: null, approve: SEGREGATION, reject: SELF_REJECT, compose: null })
		);
		await openReport(page);
		await expect(page.locator('.report-detail-actions')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
	});

	test('Submit and Attach follow their verdicts, and a missing verdict offers nothing', async ({
		page
	}) => {
		await stubReport(
			page,
			report({ submit: null, approve: NOT_PERMITTED, reject: NOT_PERMITTED, compose: null }, 'draft')
		);
		await openReport(page);
		await expect(page.getByRole('button', { name: 'Submit', exact: true })).toBeEnabled();
		await expect(page.locator('.attach-row')).toBeVisible();
		await expect(page.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
	});

	test('a report read without verdicts offers no action at all (fail closed)', async ({ page }) => {
		await stubReport(page, report(undefined, 'draft'));
		await openReport(page);
		await expect(page.locator('.report-detail-actions')).toBeVisible();
		for (const name of ['Submit', 'Approve', 'Reject']) {
			await expect(page.getByRole('button', { name, exact: true })).toHaveCount(0);
		}
		await expect(page.locator('.attach-row')).toHaveCount(0);
	});
});

function txn(
	id: string,
	merchant: string,
	reconciliation_status: string,
	actions: CorporateCardTransaction['actions']
): CorporateCardTransaction {
	return {
		id,
		card_ref: 'corp',
		card_last_four: '4242',
		virtual_card_id: null,
		txn_date: '2026-06-01',
		posted_date: null,
		merchant,
		amount: 12.5,
		currency: 'USD',
		external_txn_id: `ext-${id.slice(-4)}`,
		matched_expense_id: null,
		reconciliation_status,
		import_batch: null,
		created_at: '2026-06-01T00:00:00Z',
		updated_at: '2026-06-01T00:00:00Z',
		actions
	};
}

test('card transaction actions follow each row verdict', async ({ page }) => {
	const ALREADY_IGNORED = {
		code: 'card_txn_already_ignored',
		message: 'Transaction is already ignored',
		params: {}
	};
	const NOT_MATCHED = { code: 'card_txn_not_matched', message: 'Transaction is not matched', params: {} };
	const rows = [
		// An ignored row: Match / Create expense undo the ignore; Ignore again is refused.
		txn('00000000-0000-0000-0000-00000000c001', 'Ignored Cab', 'ignored', {
			match: null,
			create_expense: null,
			unmatch: NOT_MATCHED,
			ignore: ALREADY_IGNORED
		}),
		// A clerk's view of an unmatched row: only Create expense.
		txn('00000000-0000-0000-0000-00000000c002', 'Clerk Cab', 'unmatched', {
			match: NOT_PERMITTED,
			create_expense: null,
			unmatch: NOT_PERMITTED,
			ignore: NOT_PERMITTED
		}),
		// No verdicts (a payload predating them): nothing offered.
		txn('00000000-0000-0000-0000-00000000c003', 'Bare Cab', 'unmatched', undefined)
	];
	await page.route(
		(url) => url.pathname === '/api/corporate-card-transactions',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: rows, total: rows.length, page: 1, page_size: 50 })
			})
	);
	await page.goto('/expenses?tab=cards');

	const row = (merchant: string) => page.locator('tbody tr', { hasText: merchant });
	await expect(row('Ignored Cab')).toBeVisible();
	await expect(row('Ignored Cab').getByRole('button', { name: 'Match', exact: true })).toBeVisible();
	await expect(
		row('Ignored Cab').getByRole('button', { name: 'Create expense', exact: true })
	).toBeVisible();
	await expect(row('Ignored Cab').getByRole('button', { name: 'Ignore', exact: true })).toHaveCount(0);
	await expect(row('Ignored Cab').getByRole('button', { name: 'Unmatch', exact: true })).toHaveCount(0);

	await expect(row('Clerk Cab').getByRole('button', { name: 'Create expense', exact: true })).toBeVisible();
	await expect(row('Clerk Cab').getByRole('button', { name: 'Match', exact: true })).toHaveCount(0);

	await expect(row('Bare Cab')).toBeVisible();
	await expect(row('Bare Cab').locator('.actions button')).toHaveCount(0);
});
