import { expect, test } from '../fixtures/helpers';

/**
 * /tax — per-box 1099 allocation.
 *
 * A vendor's reportable total is split across IRS boxes (NEC box 1, MISC
 * boxes 1/2/3/6/10) by the paying invoice's GL account, via the per-org
 * `settings.tax.boxes` mapping. The page has to let a preparer SEE that split
 * — and the fallback money nobody has written a rule for — before filing.
 *
 * The lean e2e seed has no GL→box mapping and no multi-category vendor, so
 * these tests patch the report response (the same route-interception pattern
 * `dashboard.spec.ts` uses for the card-exclusion cases) to pin the rendering
 * deterministically instead of depending on seed drift. What is under test is
 * the page's own reading of the contract, not the aggregation — that is
 * covered exactly in `backend/tests/test_tax_1099_boxes.py`.
 */

type Json = Record<string, unknown>;

const BOXES = [
	{
		box: 'NEC-1',
		form_type: '1099-NEC',
		box_number: '1',
		label: 'Nonemployee compensation',
		amount: '899.50',
		payment_count: 4,
		fallback: true
	},
	{
		box: 'MISC-1',
		form_type: '1099-MISC',
		box_number: '1',
		label: 'Rents',
		amount: '1200.00',
		payment_count: 2,
		fallback: false
	},
	{
		box: 'MISC-6',
		form_type: '1099-MISC',
		box_number: '6',
		label: 'Medical and health care payments',
		amount: '300.50',
		payment_count: 1,
		fallback: false
	}
];

async function loadWithAllocation(
	page: import('@playwright/test').Page,
	overrides: Json = {}
) {
	await page.route('**/api/tax/1099-report**', async (route) => {
		const resp = await route.fetch();
		const body = await resp.json();
		body.box_allocations = BOXES;
		body.total_reportable = '2400.00';
		body.total_reportable_usd = '2400.00';
		body.vendor_count_eligible_over_threshold = 1;
		body.total_unmapped = '899.50';
		body.box_unallocated = '0.00';
		body.box_allocation_reconciled = true;
		Object.assign(body, overrides);
		// Exactly the first vendor carries the split; the rest are left flat so
		// the "no allocation" render path shares the same table.
		body.rows = body.rows.map((r: Json, i: number) =>
			i === 0
				? {
						...r,
						is_1099_eligible: true,
						over_threshold: true,
						ytd_paid: '2400.00',
						payment_count: 7,
						box_allocations: BOXES,
						unmapped_paid: '899.50',
						unmapped_payment_count: 4,
						box_unallocated: '0.00',
						// The backend's per-box verdict — the filing modal reads
						// this, never a client-side threshold.
						required_forms: ['1099-NEC', '1099-MISC']
					}
				: {
						...r,
						box_allocations: [],
						unmapped_paid: '0',
						unmapped_payment_count: 0,
						box_unallocated: '0.00',
						required_forms: []
					}
		);
		await route.fulfill({ response: resp, json: body });
	});
	await page.goto('/tax');
}

test.describe('/tax — 1099 box allocation (admin)', () => {
	test('the summary panel lists every populated box and says the split reconciles', async ({
		page
	}) => {
		await loadWithAllocation(page);

		const panel = page.locator('.box-panel');
		await expect(panel).toBeVisible({ timeout: 10_000 });
		await expect(panel.getByRole('heading', { name: '1099 box allocation' })).toBeVisible();

		const items = panel.locator('.box-item');
		await expect(items).toHaveCount(3);
		await expect(items.nth(0)).toContainText('NEC-1');
		await expect(items.nth(0)).toContainText('Nonemployee compensation');
		await expect(items.nth(0)).toContainText('$899.50');
		await expect(items.nth(1)).toContainText('Rents');
		await expect(items.nth(1)).toContainText('$1,200.00');
		await expect(items.nth(2)).toContainText('Medical and health care payments');
		await expect(items.nth(2)).toContainText('$300.50');

		// The reconciliation guarantee is stated, not assumed.
		await expect(panel).toContainText('add up to the total on required 1099s');
	});

	test('the headline and the panel describe only the forms a vendor must receive', async ({
		page
	}) => {
		// The 2026 law firm: $800 of attorney proceeds (MISC-10, $600 threshold)
		// and $1,000 of fees (NEC-1, $2,000 threshold) — a MISC only. The backend
		// derives the summary from the required form's boxes (pytest pins that:
		// `test_total_reportable_counts_only_boxes_on_required_forms`); what is
		// under test here is that the page labels the figure for what it is and
		// keeps the vendor's own row whole.
		const misc10 = {
			box: 'MISC-10',
			form_type: '1099-MISC',
			box_number: '10',
			label: 'Gross proceeds paid to an attorney',
			amount: '800.00',
			payment_count: 1,
			fallback: false
		};
		const nec1 = { ...BOXES[0], amount: '1000.00', payment_count: 1, fallback: false };
		await page.route('**/api/tax/1099-report**', async (route) => {
			const resp = await route.fetch();
			const body = await resp.json();
			Object.assign(body, {
				box_allocations: [misc10],
				total_reportable: '800.00',
				total_reportable_usd: '800.00',
				vendor_count_eligible_over_threshold: 1,
				total_unmapped: '0',
				unmapped_payment_count: 0,
				box_unallocated: '0.00',
				box_allocation_reconciled: true
			});
			body.rows = body.rows.map((r: Json, i: number) =>
				i === 0
					? {
							...r,
							is_1099_eligible: true,
							over_threshold: true,
							ytd_paid: '1800.00',
							payment_count: 2,
							box_allocations: [nec1, misc10],
							unmapped_paid: '0',
							unmapped_payment_count: 0,
							box_unallocated: '0.00',
							required_forms: ['1099-MISC']
						}
					: { ...r, box_allocations: [], box_unallocated: '0.00', required_forms: [] }
			);
			await route.fulfill({ response: resp, json: body });
		});
		await page.goto('/tax');

		const kpi = page.locator('.kpi', { hasText: 'Total on required 1099s' });
		await expect(kpi.locator('.kpi-value')).toHaveText('$800.00', { timeout: 10_000 });

		const panel = page.locator('.box-panel');
		await expect(panel).toContainText('is not required to receive is not counted');
		await expect(panel.locator('.box-item')).toHaveCount(1);
		await expect(panel.locator('.box-item').first()).toContainText('MISC-10');
		await expect(panel).toContainText('add up to the total on required 1099s');

		// The vendor's own row still shows every box it was paid into.
		const split = page.locator('.grid-container tbody tr').first().locator('.box-split');
		await expect(split.locator('.box-split-item')).toHaveCount(2);
	});

	test('fallback money is called out with the box it landed in', async ({ page }) => {
		await loadWithAllocation(page);

		const panel = page.locator('.box-panel');
		await expect(panel).toBeVisible({ timeout: 10_000 });
		// The box that absorbed unmapped spend is tagged in the list…
		await expect(panel.locator('.box-item').nth(0).locator('.box-fallback')).toBeVisible();
		// …and the amount + destination box are spelled out beneath it.
		const note = panel.locator('.box-note.warn');
		await expect(note).toContainText('$899.50');
		await expect(note).toContainText('NEC-1');
	});

	test('a residual that does not reconcile is raised as an alert', async ({ page }) => {
		await loadWithAllocation(page, {
			box_allocation_reconciled: false,
			box_unallocated: '12.34'
		});

		const alert = page.locator('.box-panel [role="alert"]');
		await expect(alert).toBeVisible({ timeout: 10_000 });
		await expect(alert).toContainText('$12.34');
		await expect(alert).toContainText('Do not file');
	});

	test('a vendor row shows its own split, and the Unmapped box chip is the worklist', async ({
		page
	}) => {
		await loadWithAllocation(page);

		const firstRow = page.locator('.grid-container tbody tr').first();
		const split = firstRow.locator('.box-split');
		await expect(split).toBeVisible({ timeout: 10_000 });
		const parts = split.locator('.box-split-item');
		await expect(parts).toHaveCount(3);
		await expect(parts.nth(1)).toContainText('MISC-1');
		await expect(parts.nth(1)).toContainText('$1,200.00');
		// The fallback share is marked on the row too.
		await expect(split.locator('.box-split-item.fallback')).toHaveCount(1);

		// The chip narrows to exactly the vendors whose spend has no rule.
		await page.locator('.filter-chip', { hasText: 'Unmapped box' }).click();
		await expect(page.locator('.grid-container tbody tr')).toHaveCount(1);
		await expect(page.locator('.grid-container tbody tr').first().locator('.box-split')).toBeVisible();
	});

	test('the filing modal files one form at a time, with that form’s own subtotal', async ({
		page
	}) => {
		await loadWithAllocation(page);

		await page.getByRole('button', { name: 'File 1099s' }).click();
		const modal = page.getByRole('dialog', { name: /File 1099s for \d{4}/ });
		await expect(modal).toBeVisible();

		// NEC is the default and carries only the NEC box.
		const formType = modal.getByLabel('Form type');
		await expect(formType).toHaveValue('1099-NEC');
		await expect(modal).toContainText('$899.50');
		await expect(modal.getByRole('button', { name: /File 1 1099s for \d{4}/ })).toBeVisible();

		// Switching to MISC re-totals to the MISC boxes (1,200.00 + 300.50).
		await formType.selectOption('1099-MISC');
		await expect(modal).toContainText('$1,500.50');
		await expect(modal).toContainText('Only the boxes belonging to this form are filed');

		// Nothing is submitted — this is the arm step, and the modal closes clean.
		await modal.getByRole('button', { name: 'Cancel' }).click();
		await expect(modal).not.toBeVisible();
	});
});
