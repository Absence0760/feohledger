import { API_BASE, authedTenantHeaders, expect, signIn, signInAndWait, signOut, TENANT_ROOT_URL, test } from '../fixtures/helpers';

// Start unauthenticated — this spec drives its own sign-in / sign-out for the change-password UX.
test.use({ storageState: { cookies: [], origins: [] } });

interface CreatedUser {
	id: string;
	email: string;
	temp_password: string;
}

/**
 * /change-password — strength hints, mismatch handling, and a real
 * round-trip via a freshly-created admin-side user. Each test creates
 * the user, drives the change-password form, and deletes the user in
 * finally. The seed never gets touched.
 */

async function createTestUser(
	page: import('@playwright/test').Page
): Promise<CreatedUser> {
	const email = `e2e-pw-${Date.now()}@test.local`;
	const resp = await page.request.post(`${API_BASE}/api/admin/users`, {
		headers: await authedTenantHeaders(page),
		data: { full_name: 'Password Test', email, role_names: ['ap_clerk'] }
	});
	const body = (await resp.json()) as {
		id: string;
		email: string;
		temporary_password: string;
	};
	return { id: body.id, email: body.email, temp_password: body.temporary_password };
}

async function deleteTestUser(
	page: import('@playwright/test').Page,
	id: string
) {
	// We may have logged out as the admin during the test; sign back in
	// with the worker's admin so our DELETE goes through with admin scope.
	if (!(await page.evaluate(() => localStorage.getItem('auth_token')))) {
		await signInAndWait(page);
	}
	await page.request.delete(`${API_BASE}/api/admin/users/${id}`, {
		headers: await authedTenantHeaders(page)
	});
}

test.describe('/change-password', () => {
	test('strength hints update as the user types', async ({ page }) => {
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			// must_change_password=true sends the user to /change-password.
			await page.waitForURL(/\/change-password$/, { timeout: 10_000 });

			// The form names the account, so a password manager files the new
			// password under this user rather than offering to save it nowhere.
			const accountField = page.locator('form input[autocomplete="username"]');
			await expect(accountField).toHaveValue(created.email);
			await expect(accountField).toBeHidden();

			// Empty state: no hint is satisfied.
			const items = page.locator('ul.strength li');
			await expect(items).toHaveCount(4);
			for (let i = 0; i < 4; i++) {
				await expect(items.nth(i)).not.toHaveClass(/ok/);
			}

			// Type a strong password.
			const newPw = 'Test-Password-1234';
			await page.locator('input[autocomplete="new-password"]').first().fill(newPw);
			// All four hints become satisfied.
			for (let i = 0; i < 4; i++) {
				await expect(items.nth(i)).toHaveClass(/ok/);
			}
		} finally {
			await deleteTestUser(page, created.id);
		}
	});

	test('successful change clears must_change_password and lands on /', async ({ page }) => {
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);

			const newPw = 'Test-Password-1234';
			await page.locator('input[autocomplete="current-password"]').fill(created.temp_password);
			const newPwInputs = page.locator('input[autocomplete="new-password"]');
			await newPwInputs.first().fill(newPw);
			await newPwInputs.last().fill(newPw);

			const changed = page.waitForResponse(
				(r) =>
					r.url().includes('/api/auth/change-password') &&
					r.request().method() === 'POST'
			);
			await page.getByRole('button', { name: 'Change password' }).click();
			const resp = await changed;
			expect(resp.status()).toBe(200);
			const body = (await resp.json()) as { must_change_password: boolean };
			expect(body.must_change_password).toBe(false);

			// Lands on the tenant root.
			await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });

			// Verify new password actually works: sign out, sign back in,
			// must_change_password is now false so we land on / directly.
			await signOut(page);
			await signIn(page, { email: created.email, password: newPw });
			await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });
		} finally {
			await deleteTestUser(page, created.id);
		}
	});

	test('admin reset → temp sign-in is forced to /change-password → the new password works', async ({
		page
	}) => {
		// #566: the reset issues a GENERATED temp the user must change; the Edit
		// modal carries no password input (a browser used to autofill the
		// admin's own password into it and silently reset the target on save).
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			// Replace the invite temp first, so the forced change below can only
			// have come from the reset.
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);
			const ownPw = 'Owner-Chosen-1234';
			await page.locator('input[autocomplete="current-password"]').fill(created.temp_password);
			await page.locator('input[autocomplete="new-password"]').first().fill(ownPw);
			await page.locator('input[autocomplete="new-password"]').last().fill(ownPw);
			await page.getByRole('button', { name: 'Change password' }).click();
			await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });
			await signOut(page);

			// Admin opens the user's Edit modal and resets.
			await signInAndWait(page);
			await page.goto('/admin');
			const row = page.locator('table tbody tr', { hasText: created.email });
			await expect(row).toBeVisible();
			await row.getByRole('button', { name: 'Edit' }).click();
			const modal = page.locator('div.modal[role="dialog"][aria-label="Edit user"]');
			await expect(modal.locator('input[type="email"]')).toHaveValue(created.email);
			await expect(modal.locator('input[type="password"]')).toHaveCount(0);

			// Two-step: the first click only arms it.
			await modal.getByRole('button', { name: 'Reset password' }).click();
			const reset = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/admin/users/${created.id}/reset-password`) &&
					r.request().method() === 'POST'
			);
			await modal.getByRole('button', { name: 'Confirm reset' }).click();
			expect((await reset).status()).toBe(200);

			const cred = page.locator('div.modal[role="dialog"][aria-label="Password reset"]');
			await expect(cred).toBeVisible();
			const temp = (await cred.locator('.credential-value.password').textContent())?.trim() ?? '';
			expect(temp.length).toBeGreaterThanOrEqual(12);
			await cred.getByRole('button', { name: /^Done$/ }).click();

			// The user's own password no longer works; the temp does, and forces a change.
			await signOut(page);
			await signIn(page, { email: created.email, password: temp });
			await page.waitForURL(/\/change-password$/, { timeout: 10_000 });
			const newPw = 'After-Reset-5678';
			await page.locator('input[autocomplete="current-password"]').fill(temp);
			await page.locator('input[autocomplete="new-password"]').first().fill(newPw);
			await page.locator('input[autocomplete="new-password"]').last().fill(newPw);
			await page.getByRole('button', { name: 'Change password' }).click();
			await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });

			await signOut(page);
			await signIn(page, { email: created.email, password: newPw });
			await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });
			await signOut(page);
		} finally {
			await deleteTestUser(page, created.id);
		}
	});

	test('Submit button stays disabled until both passwords match and are strong', async ({
		page
	}) => {
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);

			const submit = page.getByRole('button', { name: 'Change password' });
			await expect(submit).toBeDisabled();

			const newPwInputs = page.locator('input[autocomplete="new-password"]');
			await newPwInputs.first().fill('weakpw');
			// Still disabled — fails strength.
			await expect(submit).toBeDisabled();

			await newPwInputs.first().fill('Test-Password-1234');
			// Disabled — confirm is empty.
			await expect(submit).toBeDisabled();

			await newPwInputs.last().fill('Test-Password-1234');
			await expect(submit).toBeEnabled();
		} finally {
			await deleteTestUser(page, created.id);
		}
	});
});
