import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	isVendorListResponse,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * /vendors — the vendor modal's ERP links panel (decisions §375).
 *
 * A vendor holds one ERP link per company: the row link (last synced) and the
 * links kept for other companies (`vendor_erp_links`, §356). The panel lists
 * them, and a `vendor.manage` holder clears one company's link with an armed
 * two-click **Unlink** (`POST /api/vendors/{id}/erp-links/unlink`) — the way
 * through a merge refused `vendor_merge_erp_link_conflict` (§373).
 *
 * The links are written straight into the tenant DB: no API sets an ERP link
 * by hand (a sync is the only door that creates one), and that is the point.
 */

const VENDOR_MARKER = 'ERP-UNLINK-';

async function createVendor(
	page: import('@playwright/test').Page,
	name: string
): Promise<{ id: string; name: string }> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status()).toBe(201);
	return (await resp.json()) as { id: string; name: string };
}

function keptLinkCount(vendorId: string): number {
	return Number(
		tenantPsql(`SELECT count(*) FROM vendor_erp_links WHERE vendor_id='${vendorId}'`).trim()
	);
}

test.describe('/vendors ERP links panel (admin)', () => {
	test('lists each company link and unlinks one with a confirmed second click', async ({
		page
	}) => {
		await page.goto('/vendors');

		const stamp = Date.now();
		const vendor = await createVendor(page, `${VENDOR_MARKER}${stamp}`);
		const rowRealm = `quickbooks:e2e-row-${stamp}`;
		const keptRealm = `quickbooks:e2e-kept-${stamp}`;
		try {
			tenantPsql(
				`UPDATE vendors SET erp_vendor_id='57', erp_link_realm='${rowRealm}' WHERE id='${vendor.id}'`
			);
			tenantPsql(
				`INSERT INTO vendor_erp_links (id, organization_id, vendor_id, realm, erp_vendor_id, created_at, updated_at)
				 SELECT gen_random_uuid(), organization_id, id, '${keptRealm}', '9', now(), now()
				 FROM vendors WHERE id='${vendor.id}'`
			);

			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse((r) => isVendorListResponse(r.url(), 'search='));
			const row = page.locator('table tbody tr').first();
			await expect(row).toContainText(vendor.name);
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
			await expect(modal).toBeVisible();

			const panel = modal.locator('[data-testid="vendor-erp-links"]');
			await expect(panel.getByRole('heading', { name: 'ERP links' })).toBeVisible();
			const links = panel.locator('[data-testid="vendor-erp-link"]');
			await expect(links).toHaveCount(2);
			await expect(links.nth(0)).toContainText(rowRealm);
			await expect(links.nth(0)).toContainText('Last synced');
			await expect(links.nth(1)).toContainText(keptRealm);
			await expect(links.nth(1)).toContainText('Kept from an earlier connection');

			// First click only arms; nothing is sent.
			const unlink = panel.getByRole('button', {
				name: `Unlink supplier record 9 in ${keptRealm}`
			});
			await unlink.click();
			await expect(unlink).toHaveText('Confirm unlink');
			expect(keptLinkCount(vendor.id)).toBe(1);

			const done = page.waitForResponse(
				(r) => r.url().includes(`/api/vendors/${vendor.id}/erp-links/unlink`) && r.ok()
			);
			await unlink.click();
			await done;

			await expect(links).toHaveCount(1);
			await expect(links.first()).toContainText(rowRealm);
			expect(keptLinkCount(vendor.id)).toBe(0);
			expect(
				tenantPsql(
					`SELECT count(*) FROM audit_log WHERE entity_id='${vendor.id}' AND action='vendor.erp_unlinked'`
				).trim()
			).toBe('1');
		} finally {
			try {
				deleteVendorsWhere(`id='${vendor.id}'`);
			} catch {
				/* best-effort */
			}
		}
	});
});
