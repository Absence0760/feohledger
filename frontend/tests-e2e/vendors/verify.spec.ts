import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

async function findVendorByStatus(
	page: import('@playwright/test').Page,
	wanted: 'active' | 'unverified' | 'rejected'
) {
	const resp = await page.request.get(
		`${API_BASE}/api/vendors?status=${wanted}&page_size=100`,
		{ headers: await authedTenantHeaders(page) }
	);
	const body = (await resp.json()) as {
		items: Array<{ id: string; name: string; status: string }>;
	};
	return body.items.find((v) => v.status === wanted) ?? null;
}

async function setVendorStatus(
	page: import('@playwright/test').Page,
	id: string,
	status: 'active' | 'unverified' | 'rejected'
) {
	await page.request.patch(`${API_BASE}/api/vendors/${id}`, {
		headers: await authedTenantHeaders(page),
		data: { status }
	});
}

/**
 * Vendor verify/reject lifecycle. The seed has at least one
 * `unverified` vendor; the row's actions cell shows Verify + Reject
 * only when the status is `unverified`. Each test mutates state then
 * reverses it via API in finally.
 */

test.describe('/vendors verify/reject', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendors');
	});

	// The row offers what the server's `status_actions` verdict allows (issue
	// #575): Verify only from `unverified`; Reject from `unverified` OR `active`,
	// which `/reject` and bulk status have always accepted — the row used to
	// hide it on an active vendor.
	test('Verify is offered only for unverified; Reject for unverified and active', async ({
		page
	}) => {
		const unverified = await findVendorByStatus(page, 'unverified');
		expect(unverified).toBeTruthy();
		const active = await findVendorByStatus(page, 'active');
		expect(active).toBeTruthy();

		const unverifiedRow = page.locator('table tbody tr', {
			hasText: unverified!.name
		});
		await expect(unverifiedRow.getByRole('button', { name: 'Verify' })).toBeVisible();
		await expect(unverifiedRow.getByRole('button', { name: 'Reject' })).toBeVisible();

		const activeRow = page.locator('table tbody tr', { hasText: active!.name });
		// Positive anchor before the absence check, so it cannot pass against a
		// half-rendered row.
		await expect(activeRow.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(activeRow.getByRole('button', { name: 'Verify' })).toHaveCount(0);
	});

	test('Verify flips an unverified vendor to active', async ({ page }) => {
		const target = await findVendorByStatus(page, 'unverified');
		expect(target).toBeTruthy();

		try {
			const row = page.locator('table tbody tr', { hasText: target!.name });
			const verified = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/vendors/${target!.id}/verify`) &&
					r.request().method() === 'POST'
			);
			await row.getByRole('button', { name: 'Verify' }).click();
			const resp = await verified;
			expect(resp.status()).toBe(200);

			// After fetchVendors, the row's actions cell loses the buttons.
			await expect(
				row.getByRole('button', { name: 'Verify' })
			).toHaveCount(0, { timeout: 5_000 });
		} finally {
			await setVendorStatus(page, target!.id, 'unverified');
		}
	});

	test('Reject flips an unverified vendor to rejected', async ({ page }) => {
		const target = await findVendorByStatus(page, 'unverified');
		expect(target).toBeTruthy();

		try {
			const row = page.locator('table tbody tr', { hasText: target!.name });
			const rejected = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/vendors/${target!.id}/reject`) &&
					r.request().method() === 'POST'
			);
			// Armed two-click: the first click only arms (it would stop an active
			// vendor's payments), the confirm sends it.
			await row.getByRole('button', { name: 'Reject' }).click();
			await row.getByRole('button', { name: 'Confirm reject' }).click();
			const resp = await rejected;
			expect(resp.status()).toBe(200);

			// Row gets the .rejected class once status flips.
			await expect(row).toHaveClass(/rejected/, { timeout: 5_000 });
		} finally {
			await setVendorStatus(page, target!.id, 'unverified');
		}
	});
});
