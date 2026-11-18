import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';
import type { Locator, Page } from '@playwright/test';

import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * `/requisitions` — linking a requisition to a budget from the form.
 *
 * The backend always took `budget_id` on a requisition, and `GET /budgets/check`
 * always existed for the requisition flow to call before submit, but the form
 * had no budget field: a budget's Committed figure could only be filled through
 * the API. The form now carries `ui/BudgetPicker` plus the overspend pre-check.
 *
 * What these lock, against the real backend:
 *   - the picker offers only budgets in the requisition's currency (the
 *     server filters with `?currency=`), so another currency's budget is never
 *     on offer;
 *   - the check reads the backend's `would_overspend` verdict and says so
 *     before save, and updates as the total changes;
 *   - the requisition saves with `budget_id`, and once submitted the budget's
 *     `committed` includes it;
 *   - a budget left in another currency after the currency field changes is
 *     refused in the form (the backend would 422), not compared at face value;
 *   - once submitted, the approver opening it sees the budget's headroom read
 *     from `/budgets/{id}/spend` (which already counts it), not re-checked;
 *   - at a 390px phone width the form, picker popup and warnings reflow and
 *     pass axe;
 *   - an `ap_clerk` (no read on `/api/budgets`) gets no picker at all.
 *
 * Options are always read from the BUDGET listbox: the dialog also holds the
 * line's GL-account `<select>`, whose native `<option>`s carry the same role,
 * so an unscoped `getByRole('option')` counted them too on a tenant with GL
 * accounts.
 */

type Budget = { id: string; name: string };
type Requisition = { id: string; requisition_number: string; budget_id: string | null };

async function createBudget(
	page: Page,
	headers: Record<string, string>,
	name: string,
	amount: string,
	currency: string
): Promise<Budget> {
	const resp = await page.request.post(`${API_BASE}/api/budgets`, {
		headers,
		data: {
			name,
			dimension: 'department',
			dimension_value: `E2E ${name}`,
			period: '2026',
			amount,
			currency
		}
	});
	expect(resp.status(), `create budget ${name}`).toBe(201);
	return (await resp.json()) as Budget;
}

function cleanup(budgetIds: string[], requisitionNumbers: string[]): void {
	for (const n of requisitionNumbers) {
		tenantPsql(
			`DELETE FROM requisition_line_items WHERE requisition_id IN (SELECT id FROM purchase_requisitions WHERE requisition_number='${n}')`
		);
		tenantPsql(`DELETE FROM purchase_requisitions WHERE requisition_number='${n}'`);
	}
	for (const id of budgetIds) tenantPsql(`DELETE FROM budgets WHERE id='${id}'`);
}

async function openNewRequisition(page: Page, number: string, currency: string) {
	await page.goto('/requisitions');
	await page.getByRole('button', { name: '+ New Requisition' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Requisition Number').fill(number);
	await dialog.getByTestId('requisition-currency').fill(currency);
	await dialog.getByLabel('Line 1 description').fill('Widgets');
	await dialog.getByLabel('Line 1 quantity').fill('1');
	return dialog;
}

/** The budget picker's own options — never the GL `<select>`'s. */
function budgetOptions(dialog: Locator, name?: RegExp): Locator {
	const list = dialog.getByRole('listbox', { name: 'Budget' });
	return name ? list.getByRole('option', { name }) : list.getByRole('option');
}

test.describe('/requisitions budget link', () => {
	test('pick a budget, see the overspend warning, save, and Committed reflects it', async ({
		page
	}) => {
		const tag = `${Date.now()}`;
		const headers = await authedTenantHeaders(page);
		const usd = await createBudget(page, headers, `RB-${tag} Ops`, '100.00', 'USD');
		const eur = await createBudget(page, headers, `RB-${tag} Euro`, '5000.00', 'EUR');
		const number = `RQ-BUDGET-${tag}`;
		try {
			const dialog = await openNewRequisition(page, number, 'USD');
			await dialog.getByLabel('Line 1 unit price').fill('40');

			// Search the tag both budgets share: only the USD one is offered.
			const picker = dialog.getByRole('combobox', { name: 'Budget' });
			await picker.fill(`RB-${tag}`);
			await expect(budgetOptions(dialog)).toHaveCount(1);
			await expect(budgetOptions(dialog, /Ops \(2026\)/)).toBeVisible();
			await expect(budgetOptions(dialog, /Euro/)).toHaveCount(0);
			await budgetOptions(dialog, /Ops \(2026\)/).click();
			await expect(picker).toHaveValue(`RB-${tag} Ops (2026)`);

			// 40 of 100: within, with the headroom left after this requisition.
			const status = dialog.getByTestId('requisition-budget-status');
			await expect(status).toHaveText('Within budget: this leaves $60.00 of $100.00.');
			await expect(dialog.getByTestId('requisition-budget-overspend')).toHaveCount(0);

			// Raise the total past the allocation: the warning replaces it.
			await dialog.getByLabel('Line 1 unit price').fill('150');
			const overspend = dialog.getByTestId('requisition-budget-overspend');
			await expect(overspend).toContainText('Over budget: this would leave -$50.00 of $100.00.');
			await expect(overspend).toHaveAttribute('role', 'status');

			// Advisory, not a refusal: save goes through, carrying the link.
			await dialog.getByRole('button', { name: 'Create' }).click();
			await expect(dialog).toHaveCount(0);

			const list = await page.request.get(
				`${API_BASE}/api/requisitions?search=${encodeURIComponent(number)}`,
				{ headers }
			);
			const [saved] = ((await list.json()) as { items: Requisition[] }).items;
			expect(saved.budget_id).toBe(usd.id);

			// A draft is not yet a commitment; submitting makes it one.
			const row = page.locator('table tbody tr', { hasText: number });
			await row.getByRole('button', { name: 'Submit' }).click();
			await expect(row.locator('.badge.pending_approval')).toBeVisible();

			const spend = await page.request.get(`${API_BASE}/api/budgets/${usd.id}/spend`, { headers });
			expect(spend.status()).toBe(200);
			expect(Number(((await spend.json()) as { committed: number }).committed)).toBe(150);

			// The approver's view of the submitted requisition: the headroom as
			// the budget stands, this requisition counted once — not -$200.00,
			// which a re-check of its 150 against the spend would report.
			await page.goto(`/requisitions?id=${saved.id}`);
			const detail = page.getByRole('dialog');
			await expect(detail.getByTestId('requisition-budget-headroom')).toContainText(
				'Over budget, counting this requisition: -$50.00 of $100.00 left.'
			);
			await expect(detail.getByTestId('requisition-budget-overspend')).toHaveCount(0);
			await expect(detail.getByTestId('requisition-budget-status')).toHaveCount(0);
		} finally {
			cleanup([usd.id, eur.id], [number]);
		}
	});

	test('a budget left in another currency is refused in the form, not compared', async ({
		page
	}) => {
		const tag = `${Date.now()}`;
		const headers = await authedTenantHeaders(page);
		const usd = await createBudget(page, headers, `RB-${tag} Mismatch`, '100.00', 'USD');
		const number = `RQ-BUDGET-MM-${tag}`;
		try {
			const dialog = await openNewRequisition(page, number, 'USD');
			await dialog.getByLabel('Line 1 unit price').fill('10');
			const picker = dialog.getByRole('combobox', { name: 'Budget' });
			await picker.fill(`RB-${tag}`);
			await budgetOptions(dialog, /Mismatch/).click();
			await expect(dialog.getByTestId('requisition-budget-status')).toContainText('Within budget');

			await dialog.getByTestId('requisition-currency').fill('EUR');
			await expect(dialog.getByTestId('requisition-budget-mismatch')).toHaveText(
				/This budget is in USD and the requisition is in EUR\./
			);
			await expect(dialog.getByTestId('requisition-budget-status')).toHaveCount(0);
			await expect(dialog.getByRole('button', { name: 'Create' })).toBeDisabled();

			// Clearing the link releases the form.
			await dialog.getByRole('button', { name: 'Clear the selected budget' }).click();
			await expect(dialog.getByTestId('requisition-budget-mismatch')).toHaveCount(0);
			await expect(dialog.getByRole('button', { name: 'Create' })).toBeEnabled();
		} finally {
			cleanup([usd.id], [number]);
		}
	});
});

test.describe('/requisitions budget link at phone width', () => {
	test.use({ viewport: { width: 390, height: 844 } });

	test('the form reflows and passes axe with the picker and the overspend warning open', async ({
		page
	}) => {
		const tag = `${Date.now()}`;
		const headers = await authedTenantHeaders(page);
		const usd = await createBudget(page, headers, `RB-${tag} Narrow`, '100.00', 'USD');
		const number = `RQ-BUDGET-NARROW-${tag}`;
		try {
			const dialog = await openNewRequisition(page, number, 'USD');
			await dialog.getByLabel('Line 1 unit price').fill('150');
			const picker = dialog.getByRole('combobox', { name: 'Budget' });

			const noSidewaysScroll = async () => {
				const overflow = await page.evaluate(
					() => document.documentElement.scrollWidth - document.documentElement.clientWidth
				);
				expect(overflow, 'the document scrolls sideways at 390px').toBeLessThanOrEqual(0);
				const box = await dialog.boundingBox();
				expect(box, 'dialog box').not.toBeNull();
				expect(box!.x).toBeGreaterThanOrEqual(0);
				expect(box!.x + box!.width).toBeLessThanOrEqual(390);
			};

			// The picker's popup open over the form.
			await picker.fill(`RB-${tag}`);
			await expect(budgetOptions(dialog, /Narrow/)).toBeVisible();
			await noSidewaysScroll();
			await expectNoA11yViolations(page);

			// Chosen, and over budget: the warning under the picker.
			await budgetOptions(dialog, /Narrow/).click();
			const overspend = dialog.getByTestId('requisition-budget-overspend');
			await expect(overspend).toContainText('Over budget');
			const warningBox = await overspend.boundingBox();
			const dialogBox = await dialog.boundingBox();
			expect(warningBox!.x + warningBox!.width).toBeLessThanOrEqual(dialogBox!.x + dialogBox!.width);
			await noSidewaysScroll();
			await expectNoA11yViolations(page);
		} finally {
			cleanup([usd.id], [number]);
		}
	});
});

test.describe('/requisitions budget link (ap_clerk — no budget read)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a clerk creates requisitions with no budget picker', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/requisitions');
		await page.getByRole('button', { name: '+ New Requisition' }).click();
		const dialog = page.getByRole('dialog');
		await expect(dialog.getByLabel('Requisition Number')).toBeVisible();
		await expect(dialog.getByRole('combobox', { name: 'Budget' })).toHaveCount(0);
	});
});
