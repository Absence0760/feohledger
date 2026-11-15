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
 * The links are written straight into the tenant DB here. The one API that
 * sets a link by hand, "Find this vendor in the ERP" (decisions §415), needs a
 * searchable ERP connected; its spec below stubs the ERP side.
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

			// First click only arms; nothing is sent. The accessible name says
			// what the next click does, as the visible text does (WCAG 2.5.3 /
			// 4.1.2), so the button is found by its row, not by one name.
			const unlink = links.nth(1).getByRole('button');
			await expect(unlink).toHaveAccessibleName(`Unlink supplier record 9 in ${keptRealm}`);
			await unlink.click();
			await expect(unlink).toHaveText('Confirm unlink');
			await expect(unlink).toHaveAccessibleName(
				`Confirm unlink of supplier record 9 in ${keptRealm}`
			);
			expect(keptLinkCount(vendor.id)).toBe(1);

			// A click anywhere else un-arms it (ui-patterns § Per-row actions):
			// an armed Confirm is never left for a later stray click.
			await panel.getByRole('heading', { name: 'ERP links' }).click();
			await expect(unlink).toHaveText('Unlink');
			await expect(unlink).toHaveAccessibleName(`Unlink supplier record 9 in ${keptRealm}`);
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

	test('finds a customer in the ERP and links it with a confirmed second click', async ({
		page
	}) => {
		// decisions §415; the panel reads the server's per-vendor verdict
		// (`search_refusal`, ui-patterns § Whether to offer an action). The ERP is stubbed at the API boundary (no searchable
		// ERP runs in this suite): the search answers one Xero customer, the
		// link answers the vendor linked. The server-side rules are pinned in
		// backend/tests/test_vendor_erp_manual_link.py.
		await page.goto('/vendors');
		const stamp = Date.now();
		const vendor = await createVendor(page, `${VENDOR_MARKER}FIND-${stamp}`);
		const realm = `xero:e2e-${stamp}`;
		const record = 'c0ffee00-0000-4000-8000-0000000e2e01';
		const searches: unknown[] = [];
		const links: unknown[] = [];
		await page.route(`**/api/vendors/${vendor.id}/erp-links`, (route) =>
			route.fulfill({ json: { vendor_id: vendor.id, links: [], search_refusal: null } })
		);
		await page.route(`**/api/vendors/${vendor.id}/erp-links/search`, (route) => {
			searches.push(route.request().postDataJSON());
			return route.fulfill({
				json: {
					realm,
					truncated: false,
					candidates: [
						{
							erp_vendor_id: record,
							name: 'Bloom Florists Ltd',
							email: 'accounts@bloom.example',
							active: true,
							tax_id_matches: true,
							linked_elsewhere: false
						}
					]
				}
			});
		});
		await page.route(`**/api/vendors/${vendor.id}/erp-links/link`, (route) => {
			links.push(route.request().postDataJSON());
			return route.fulfill({
				json: {
					realm,
					erp_vendor_id: record,
					vendor: { ...vendor, erp_vendor_id: record },
					links: [{ realm, erp_vendor_id: record, placement: 'row' }],
					// Linked in the connected company now: both doors refuse it.
					search_refusal: {
						code: 'vendor_erp_link_exists',
						message: 'This vendor is already linked to a supplier record in this ERP company.',
						params: {}
					}
				}
			});
		});
		try {
			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse((r) => isVendorListResponse(r.url(), 'search='));
			const row = page.locator('table tbody tr').first();
			await expect(row).toContainText(vendor.name);
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
			const find = modal.getByTestId('vendor-erp-find');
			await expect(find.getByRole('heading', { name: 'Find this vendor in the ERP' })).toBeVisible();

			// Prefilled with the vendor's name; the server reads its tax number itself.
			const query = find.getByLabel('Name to search for');
			await expect(query).toHaveValue(vendor.name);
			await query.fill('Bloom');
			await find.getByRole('button', { name: 'Search' }).click();
			const candidate = find.getByTestId('vendor-erp-candidate');
			await expect(candidate).toContainText('Bloom Florists Ltd');
			await expect(candidate).toContainText('Tax number matches');
			expect(searches).toEqual([{ query: 'Bloom' }]);

			// Arming changes the accessible name with the visible text (WCAG
			// 2.5.3 / 4.1.2), so the button is found by its row.
			const link = candidate.getByRole('button');
			await expect(link).toHaveAccessibleName('Link this vendor to Bloom Florists Ltd');
			await link.click();
			await expect(link).toHaveText('Confirm link');
			await expect(link).toHaveAccessibleName('Confirm link to Bloom Florists Ltd');
			expect(links).toEqual([]);

			// A click anywhere else un-arms it (ui-patterns § Per-row actions).
			await find.getByRole('heading', { name: 'Find this vendor in the ERP' }).click();
			await expect(link).toHaveText('Link');
			await expect(link).toHaveAccessibleName('Link this vendor to Bloom Florists Ltd');
			expect(links).toEqual([]);

			await link.click();
			await link.click();
			await expect.poll(() => links).toEqual([{ erp_vendor_id: record }]);
			const linked = modal.locator('[data-testid="vendor-erp-link"]');
			await expect(linked).toHaveCount(1);
			await expect(linked).toContainText(realm);

			// Linked in this company: the search and its Link are gone (every
			// further Link would be refused), the reason stands in their place,
			// and focus moves to it rather than dropping with the button.
			await expect(find).toHaveCount(0);
			const note = modal.getByTestId('vendor-erp-find-refusal');
			await expect(note).toHaveText(
				'This vendor is already linked to a supplier record in this ERP company. Unlink it first.'
			);
			await expect(note).toBeFocused();
			await expectNoA11yViolations(page);
		} finally {
			try {
				deleteVendorsWhere(`id='${vendor.id}'`);
			} catch {
				/* best-effort */
			}
		}
	});
	test('offers no ERP search to a vendor the server refuses it for', async ({ page }) => {
		// The panel reads the per-vendor `search_refusal` (issue #575): a vendor
		// already linked in the connected company gets the reason, not a Link
		// the server would refuse; no searchable ERP gets nothing at all.
		await page.goto('/vendors');
		const stamp = Date.now();
		const vendor = await createVendor(page, `${VENDOR_MARKER}REFUSED-${stamp}`);
		const realm = `xero:e2e-refused-${stamp}`;
		let refusal: { code: string; message: string; params: object } = {
			code: 'vendor_erp_link_exists',
			message: 'This vendor is already linked to a supplier record in this ERP company.',
			params: {}
		};
		await page.route(`**/api/vendors/${vendor.id}/erp-links`, (route) =>
			route.fulfill({
				json: {
					vendor_id: vendor.id,
					links: [{ realm, erp_vendor_id: '57', placement: 'row' }],
					search_refusal: refusal
				}
			})
		);
		const row = page.locator('table tbody tr').first();
		const open = async () => {
			await row.locator('td.vendor-name .row-link').click();
			const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
			await expect(modal.locator('[data-testid="vendor-erp-link"]')).toHaveCount(1);
			return modal;
		};
		try {
			const searchBox = page.getByPlaceholder('Search vendors...');
			await searchBox.fill(vendor.name);
			await page.waitForResponse((r) => isVendorListResponse(r.url(), 'search='));
			await expect(row).toContainText(vendor.name);
			let modal = await open();
			await expect(modal.getByTestId('vendor-erp-find')).toHaveCount(0);
			await expect(modal.getByTestId('vendor-erp-find-refusal')).toHaveText(
				'This vendor is already linked to a supplier record in this ERP company. Unlink it first.'
			);

			await page.keyboard.press('Escape');
			await expect(modal).toBeHidden();
			refusal = {
				code: 'vendor_erp_search_unavailable',
				message: "The connected ERP can't be searched for a supplier record from here.",
				params: {}
			};
			modal = await open();
			await expect(modal.getByTestId('vendor-erp-find')).toHaveCount(0);
			await expect(modal.getByTestId('vendor-erp-find-refusal')).toHaveCount(0);
		} finally {
			try {
				deleteVendorsWhere(`id='${vendor.id}'`);
			} catch {
				/* best-effort */
			}
		}
	});
});
