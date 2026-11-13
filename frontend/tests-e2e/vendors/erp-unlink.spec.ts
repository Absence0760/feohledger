import { expectNoA11yViolations } from '../a11y/axe-helper';
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
			const modal = page.getByRole('dialog', {
				name: 'Vendor screening and risk'
			});
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

			// A click anywhere else un-arms it (ui-patterns § Per-row actions):
			// an armed Confirm is never left for a later stray click.
			await panel.getByRole('heading', { name: 'ERP links' }).click();
			await expect(unlink).toHaveText('Unlink');
			expect(keptLinkCount(vendor.id)).toBe(1);

			// Arm again for the confirmed path below.
			await unlink.click();
			await expect(unlink).toHaveText('Confirm unlink');

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

	test('the ERP links panel has no axe violations at desktop width or at 320px', async ({
		page
	}) => {
		await page.goto('/vendors');

		const stamp = Date.now();
		const vendor = await createVendor(page, `${VENDOR_MARKER}A11Y-${stamp}`);
		// A long realm and supplier id: the widest content the table carries.
		const rowRealm = `quickbooks:e2e-a11y-row-realm-${stamp}`;
		const keptRealm = `quickbooks:e2e-a11y-kept-realm-${stamp}`;
		try {
			tenantPsql(
				`UPDATE vendors SET erp_vendor_id='VENDOR-0000000000057', erp_link_realm='${rowRealm}' WHERE id='${vendor.id}'`
			);
			tenantPsql(
				`INSERT INTO vendor_erp_links (id, organization_id, vendor_id, realm, erp_vendor_id, created_at, updated_at)
				 SELECT gen_random_uuid(), organization_id, id, '${keptRealm}', 'VENDOR-0000000000009', now(), now()
				 FROM vendors WHERE id='${vendor.id}'`
			);

			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse((r) => isVendorListResponse(r.url(), 'search='));
			const row = page.locator('table tbody tr').first();
			await expect(row).toContainText(vendor.name);
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', {
				name: 'Vendor screening and risk'
			});
			await expect(modal).toBeVisible();
			const panel = modal.locator('[data-testid="vendor-erp-links"]');
			await expect(panel.locator('[data-testid="vendor-erp-link"]')).toHaveCount(2);

			// The scroller is a named, focusable region whatever the width:
			// axe's scrollable-region-focusable only fires while it overflows.
			const region = panel.getByRole('region', { name: 'ERP links' });
			await expect(region).toHaveAttribute('tabindex', '0');

			await expectNoA11yViolations(page);

			await page.setViewportSize({ width: 320, height: 720 });
			await expect(region).toBeVisible();
			// At 320px the table really is wider than the dialog, so the rule
			// above is live, and the page itself must not scroll sideways.
			const regionOverflows = await region.evaluate((el) => el.scrollWidth > el.clientWidth);
			expect(regionOverflows, 'the links table should pan inside its region at 320px').toBe(true);
			const pageOverflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(pageOverflow, 'the page scrolls sideways at 320px').toBeLessThanOrEqual(1);
			await expectNoA11yViolations(page);
		} finally {
			try {
				deleteVendorsWhere(`id='${vendor.id}'`);
			} catch {
				/* best-effort */
			}
		}
	});
});
