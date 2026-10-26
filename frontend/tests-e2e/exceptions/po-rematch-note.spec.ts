import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * A PO-match hold an ERP PO sync's re-check no longer finds stays open, since
 * the sync never clears a hold (decisions §323, amended). The row carries
 * `erp_report.kind = "po_rematch_clears"`, and this pins that the queue and
 * the resolve dialog both say why a person may now clear it.
 *
 * Each test builds its own vendor, invoice and exception with a unique tag and
 * deletes them afterwards.
 */

async function heldInvoice(page: Page, tag: string): Promise<string> {
	const vendorName = `${tag} Supplier`;
	tenantPsql(
		`INSERT INTO vendors (id, organization_id, name, status, source, accepts_virtual_cards, is_1099_eligible, kyc_status, screening_status, payments_blocked, risk_level, created_at, updated_at)
		 SELECT gen_random_uuid(), (SELECT organization_id FROM vendors LIMIT 1), '${vendorName}', 'active', 'manual', false, false, 'not_required', 'clear', false, 'low', now(), now()`
	);
	const vendorId = tenantPsql(`SELECT id FROM vendors WHERE name='${vendorName}'`).trim();
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: vendorName, invoice_number: tag, amount: 150.0, currency: 'USD' }
	});
	expect(resp.status(), await resp.text()).toBe(201);
	const id = ((await resp.json()) as { id: string }).id;
	tenantPsql(`UPDATE invoices SET vendor_id='${vendorId}' WHERE id='${id}'`);
	return id;
}

function poMismatch(invoiceId: string, erpReport: Record<string, unknown> | null): string {
	const report = erpReport ? `'${JSON.stringify(erpReport)}'::jsonb` : 'NULL';
	return tenantPsql(
		`INSERT INTO exceptions (id, invoice_id, organization_id, entity_id, exception_type, severity, description, status, erp_report, created_at, updated_at)
		 SELECT gen_random_uuid(), i.id, i.organization_id, i.entity_id, 'po_mismatch', 'error', 'e2e §323', 'open', ${report}, now(), now()
		 FROM invoices i WHERE i.id='${invoiceId}' RETURNING id`
	)
		.split('\n')[0]
		.trim();
}

test.describe('/exceptions — a hold the ERP PO sync re-check no longer finds', () => {
	test('the row and its dialog say the PO changed and a re-check now matches', async ({
		page
	}) => {
		const tag = `E323R-${Date.now().toString(36)}`;
		try {
			const invoiceId = await heldInvoice(page, tag);
			const id = poMismatch(invoiceId, {
				kind: 'po_rematch_clears',
				erp_type: 'netsuite',
				po_number: `PO-${tag}`,
				observed_on: '2026-10-26'
			});
			const note = new RegExp(
				`^PO PO-${tag} changed in NetSuite on .+, and a re-check now matches\\. Review it to clear the hold\\.$`
			);

			await page.goto(`/exceptions?search=${tag}`);
			const row = page.locator('table tbody tr', { hasText: tag });
			await expect(row.getByTestId('exception-rematch-note')).toHaveText(note);

			await page.goto(`/exceptions?id=${id}`);
			await expect(page.getByTestId('exception-resolve-form')).toBeVisible();
			await expect(page.getByTestId('exception-rematch-note-detail')).toHaveText(note);
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});

	test('a hold with no such report shows no note', async ({ page }) => {
		const tag = `E323N-${Date.now().toString(36)}`;
		try {
			const invoiceId = await heldInvoice(page, tag);
			poMismatch(invoiceId, null);

			await page.goto(`/exceptions?search=${tag}`);
			const row = page.locator('table tbody tr', { hasText: tag });
			await expect(row).toBeVisible();
			await expect(row.getByTestId('exception-rematch-note')).toHaveCount(0);
		} finally {
			deleteVendorsWhere(`name='${tag} Supplier'`);
		}
	});
});
