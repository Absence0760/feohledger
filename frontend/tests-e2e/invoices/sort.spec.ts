import { expect, test } from '../fixtures/helpers';

/**
 * /invoices column sort (issue #328, power-user/Medium): the five primary
 * lists shipped fixed server order only. Clicking a sortable column header
 * now issues `?sort=<field>&order=asc|desc`, toggles direction on a second
 * click, and persists the choice to the URL (mirrors /expenses' syncUrl()).
 * The seed's 10 acme invoices are used as-is — this only asserts the
 * request/URL contract, not a specific ordering of seed data.
 */

test.describe('/invoices column sort (acme admin)', () => {
	test('clicking Amount sorts ascending, then descending on a second click', async ({
		page
	}) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const ascRequest = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=amount') &&
				r.url().includes('order=asc')
		);
		await page.getByRole('button', { name: /^Amount/ }).click();
		await ascRequest;
		await expect(page).toHaveURL(/sort=amount/);
		await expect(page).toHaveURL(/order=asc/);

		const descRequest = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=amount') &&
				r.url().includes('order=desc')
		);
		await page.getByRole('button', { name: /^Amount/ }).click();
		await descRequest;
		await expect(page).toHaveURL(/order=desc/);
	});

	test('switching to a different column starts it ascending', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		await page.getByRole('button', { name: /^Amount/ }).click();
		await page.waitForResponse((r) => r.url().includes('sort=amount'));

		const vendorAsc = page.waitForResponse(
			(r) =>
				r.url().includes('/api/invoices?') &&
				r.url().includes('sort=vendor_name') &&
				r.url().includes('order=asc')
		);
		await page.getByRole('button', { name: 'Vendor' }).click();
		await vendorAsc;
		await expect(page).toHaveURL(/sort=vendor_name/);
		await expect(page).toHaveURL(/order=asc/);
	});

	test('sort survives a reload via the URL', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		await page.getByRole('button', { name: /^Due Date/ }).click();
		await page.waitForResponse((r) => r.url().includes('sort=due_date'));

		const url = page.url();
		expect(url).toContain('sort=due_date');

		const reloadRequest = page.waitForResponse(
			(r) => r.url().includes('/api/invoices?') && r.url().includes('sort=due_date')
		);
		await page.goto(url);
		await reloadRequest;
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});
});
