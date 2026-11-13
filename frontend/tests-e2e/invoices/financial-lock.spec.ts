import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * The invoice modal freezes the amount, payee and line items from the
 * server's `edit_financials` verdict (`GET /api/invoices/{id}/review-actions`,
 * `api/invoice_entry.edit_financials_refusal`), never a restated status list
 * (decisions §370).
 *
 * The drift this pins: the modal kept its own `FINANCIALLY_LOCKED_STATUSES`
 * (`approved` onward), while an approved invoice whose ERP push failed sits at
 * `failed` and is re-pushed by Retry ERP, then paid, with no new approval. The
 * backend now freezes it there too, so a modal reading its own list would have
 * kept the money fields editable and sent them, and every Save would have
 * 409'd. Here the fields render disabled with the server's sentence, and a
 * Save carries only the metadata. A `failed` EXTRACTION (never approved) stays
 * fully editable.
 *
 * Backend parity (verdict null exactly when both doors accept, every role and
 * status): `backend/tests/test_review_actions_parity.py`.
 */

const PREFIX = 'E2E-FINLOCK';

async function createAtFailed(
	page: import('@playwright/test').Page,
	number: string,
	{ approved }: { approved: boolean }
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E Finlock Vendor', invoice_number: number, amount: '42.17', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	// The stage is the fixture, not the path that leads there: a failed push
	// after approval carries the approval stamp; a failed extraction does not.
	tenantPsql(
		approved
			? `UPDATE invoices SET status='failed', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}'`
			: `UPDATE invoices SET status='failed' WHERE id='${id}'`
	);
	return id;
}

async function openWithVerdict(page: import('@playwright/test').Page, id: string, number: string) {
	const verdict = page.waitForResponse(
		(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
	);
	await page.goto(`/invoices?id=${id}`);
	await verdict;
	const modal = page.locator('div.modal[role="dialog"]');
	await expect(modal.locator('header h2')).toContainText(number);
	return modal;
}

/** The form-grid input under the label whose caption STARTS with `label` (so
 *  `Vendor` is not `Vendor Tax ID`). */
function field(modal: import('@playwright/test').Locator, label: string) {
	return modal
		.locator('.form-grid label', {
			has: modal.page().locator('span', { hasText: new RegExp(`^${label}`) })
		})
		.locator('input')
		.first();
}

function patchOf(page: import('@playwright/test').Page, id: string) {
	return {
		request: page.waitForRequest(
			(r) => r.method() === 'PATCH' && r.url().endsWith(`/api/invoices/${id}`)
		),
		response: page.waitForResponse(
			(r) => r.request().method() === 'PATCH' && r.url().endsWith(`/api/invoices/${id}`)
		)
	};
}

test.describe('invoice modal — the financial lock follows the server verdict', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	test('a failed ERP push after approval freezes the money fields and saves only metadata', async ({
		page
	}) => {
		const number = `${PREFIX}-PUSH-${Date.now()}`;
		const id = await createAtFailed(page, number, { approved: true });
		const modal = await openWithVerdict(page, id, number);

		await expect(modal.getByTestId('financials-locked')).toHaveText(
			'The amount, payee and line items cannot be changed once the invoice is approved: they are what the approver signed off and what will be paid.'
		);
		await expect(field(modal, 'Amount')).toBeDisabled();
		await expect(field(modal, 'Vendor')).toBeDisabled();
		await expect(field(modal, 'Tax Rate')).toBeDisabled();
		await expect(modal.getByRole('button', { name: '+ Add Line' })).toHaveCount(0);

		await field(modal, 'Description').fill('Retry after the ERP outage');
		const patch = patchOf(page, id);
		await modal.getByRole('button', { name: 'Save', exact: true }).click();
		const body = (await patch.request).postDataJSON() as Record<string, unknown>;
		for (const frozen of ['amount', 'vendor', 'tax_rate']) {
			expect(body, `the save must not carry ${frozen}`).not.toHaveProperty(frozen);
		}
		expect(body.description).toBe('Retry after the ERP outage');
		expect((await patch.response).status()).toBe(200);
		expect(tenantPsql(`SELECT amount FROM invoices WHERE id='${id}'`).trim()).toBe('42.17');
	});

	test('a failed extraction (never approved) keeps the money fields editable', async ({ page }) => {
		const number = `${PREFIX}-READ-${Date.now()}`;
		const id = await createAtFailed(page, number, { approved: false });
		const modal = await openWithVerdict(page, id, number);

		await expect(modal.getByTestId('financials-locked')).toHaveCount(0);
		await expect(field(modal, 'Amount')).toBeEnabled();
		await expect(field(modal, 'Vendor')).toBeEnabled();
		await expect(modal.getByRole('button', { name: '+ Add Line' })).toBeVisible();

		await field(modal, 'Amount').fill('43.50');
		const patch = patchOf(page, id);
		await modal.getByRole('button', { name: 'Save', exact: true }).click();
		expect((await patch.request).postDataJSON() as Record<string, unknown>).toHaveProperty(
			'amount'
		);
		expect((await patch.response).status()).toBe(200);
		expect(tenantPsql(`SELECT amount FROM invoices WHERE id='${id}'`).trim()).toBe('43.50');
	});

	test('until the verdict arrives, the money fields are locked', async ({ page }) => {
		const number = `${PREFIX}-PENDING-${Date.now()}`;
		const id = await createAtFailed(page, number, { approved: false });
		// Hold the verdict back: the modal must not offer what the server has
		// not yet said it accepts (fail closed — `#lib/api/actionVerdicts.ts`).
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => (release = resolve));
		await page.route(
			(url) => url.pathname === `/api/invoices/${id}/review-actions`,
			async (route) => {
				await held;
				await route.continue();
			}
		);
		await page.goto(`/invoices?id=${id}`);
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal.locator('header h2')).toContainText(number);
		await expect(field(modal, 'Amount')).toBeDisabled();

		const verdict = page.waitForResponse(
			(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
		);
		release();
		await verdict;
		await expect(field(modal, 'Amount')).toBeEnabled();
	});
});
