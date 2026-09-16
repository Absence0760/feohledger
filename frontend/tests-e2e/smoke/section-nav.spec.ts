import { clickSectionTab, expect, sectionTabHrefs, test } from '../fixtures/helpers';

/**
 * Grouped sidebar navigation + the per-page section sub-tab bar.
 *
 * Lower-traffic routes are folded behind group rows (Procurement / Billing /
 * Insights / Automation / Governance / Settings — see `$lib/nav`). A group row
 * lands on its first child; the page then shows the group's children as a
 * section tab bar that round-trips navigation and tracks the active route. Run
 * as admin (sees every group).
 *
 * The bar folds whatever does not fit into a More menu (`decisions §174`), and
 * Billing's 9 children overflow the default 1280px viewport — so a "which tabs
 * does this group have" assertion goes through `sectionTabHrefs`, which reads
 * the row AND the menu. Asserting the row alone would only pin what fits.
 */
test.describe('grouped sidebar navigation', () => {
	test('admin sidebar shows the expected top-level entries', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		const labels = await page
			.locator('nav.nav-main a.nav-item .nav-label')
			.allInnerTexts();
		expect(labels).toEqual([
			'Dashboard',
			'Invoices',
			'Payments',
			'Vendors',
			'Screening',
			// Vendor bank/tax change-approval queue — the dual-control BEC gate's
			// only UI. admin | ap_manager, mirroring the backend list gate.
			'Bank Changes',
			'Exceptions',
			'Procurement',
			'Billing',
			'Insights',
			'Automation',
			'Governance',
			'Settings'
		]);
	});

	test('a group row lands on its first child and shows the section tabs', async ({ page }) => {
		await page.goto('/');
		await page.locator('aside.sidebar a.nav-item', { hasText: 'Billing' }).click();

		// Billing → Contracts (first child).
		await expect(page).toHaveURL(/\/contracts$/);
		// Row + More menu, in nav order — all nine are offered whether or not
		// they fit (Billing is the widest group and does not, at 1280px).
		expect(await sectionTabHrefs(page)).toEqual([
			'/contracts',
			'/expenses',
			'/credit-memos',
			'/discounts',
			'/recurring',
			'/vendor-statements',
			'/positive-pay',
			'/bank-reconciliation',
			'/billing'
		]);
		// The landing tab is the active one.
		await expect(page.locator('.section-tab.active')).toHaveText('Contracts');
		// The sidebar group row reflects the active section.
		await expect(page.locator('aside.sidebar a.nav-item.active')).toHaveText('Billing');
	});

	test('section tabs navigate and the active tab + group follow the route', async ({ page }) => {
		await page.goto('/contracts');
		await clickSectionTab(page, 'Discounts');

		await expect(page).toHaveURL(/\/discounts$/);
		await expect(page.locator('.section-tab.active')).toHaveText('Discounts');
		// Still inside the Billing group → sidebar Billing row stays active.
		await expect(page.locator('aside.sidebar a.nav-item.active')).toHaveText('Billing');
	});

	test('deep-linking straight to a group child restores the right active tab', async ({ page }) => {
		await page.goto('/catalogs');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expect(page.locator('.section-tab.active')).toHaveText('Catalogs');
		await expect(page.locator('aside.sidebar a.nav-item.active')).toHaveText('Procurement');
	});

	test('top-level routes render no section tab bar', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expect(page.locator('.section-tabs')).toHaveCount(0);
	});
});
