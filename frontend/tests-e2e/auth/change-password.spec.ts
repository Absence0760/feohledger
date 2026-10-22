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

	test('Submit stays enabled and a submit says what is wrong beside each field', async ({
		page
	}) => {
		// #635 #14: a Submit disabled until valid gave no reason, and the
		// mismatch message could never appear.
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);

			// #635 #15: the forced change asks for the TEMPORARY password — the
			// user has never had a "current" one.
			const current = page.getByLabel('Temporary password (from your email)', { exact: true });
			const newPw = page.getByLabel('New password', { exact: true });
			const confirm = page.getByLabel('Confirm new password', { exact: true });
			await expect(current).toHaveAttribute('autocomplete', 'current-password');

			// The rules checklist describes the new-password field.
			const rulesId = await page.locator('ul.strength').getAttribute('id');
			expect(rulesId).toBeTruthy();
			await expect(newPw).toHaveAttribute('aria-describedby', new RegExp(`\\b${rulesId}\\b`));

			const submit = page.getByRole('button', { name: 'Change password' });
			await expect(submit).toBeEnabled();

			// Nothing typed: every field is marked, the first one gets focus.
			await submit.click();
			await expect(current).toBeFocused();
			await expect(current).toHaveAttribute('aria-invalid', 'true');
			await expect(current).toHaveAccessibleDescription(
				'Enter the temporary password from your email.'
			);
			await expect(newPw).toHaveAttribute('aria-invalid', 'true');
			await expect(confirm).toHaveAttribute('aria-invalid', 'true');

			// A weak new password is the first bad field once the temp is in.
			await current.fill(created.temp_password);
			await newPw.fill('weakpw');
			await submit.click();
			await expect(newPw).toBeFocused();
			await expect(current).not.toHaveAttribute('aria-invalid', 'true');
			await expect(page.getByText('Password does not meet the complexity requirements.')).toBeVisible();

			// The mismatch shows once the confirmation is finished (blurred), not
			// while it is being typed, and clears as soon as the two agree.
			await newPw.fill('Test-Password-1234');
			await confirm.fill('Test-Password-12');
			const mismatch = page.getByText('Passwords do not match.');
			await confirm.press('Tab');
			await expect(mismatch).toBeVisible();
			await expect(confirm).toHaveAttribute('aria-invalid', 'true');
			await confirm.fill('Test-Password-1234');
			await expect(mismatch).toHaveCount(0);
			await expect(confirm).not.toHaveAttribute('aria-invalid', 'true');
		} finally {
			await deleteTestUser(page, created.id);
		}
	});

	test('a wrong temporary password is named beside its field', async ({ page }) => {
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);

			const current = page.getByLabel('Temporary password (from your email)', { exact: true });
			await current.fill('not-the-temp-password');
			await page.getByLabel('New password', { exact: true }).fill('Test-Password-1234');
			await page.getByLabel('Confirm new password', { exact: true }).fill('Test-Password-1234');
			const refused = page.waitForResponse(
				(r) => r.url().includes('/api/auth/change-password') && r.request().method() === 'POST'
			);
			await page.getByRole('button', { name: 'Change password' }).click();
			expect((await refused).status()).toBe(400);

			await expect(current).toBeFocused();
			await expect(current).toHaveAttribute('aria-invalid', 'true');
			await expect(current).toHaveAccessibleDescription(
				"That isn't the temporary password from your email."
			);
		} finally {
			await deleteTestUser(page, created.id);
		}
	});

	test('the show-password toggle reveals the text and keeps the field a password manager knows', async ({
		page
	}) => {
		await signInAndWait(page);
		const created = await createTestUser(page);

		try {
			await signOut(page);
			await signIn(page, { email: created.email, password: created.temp_password });
			await page.waitForURL(/\/change-password$/);

			const current = page.getByLabel('Temporary password (from your email)', { exact: true });
			await current.fill(created.temp_password);
			// One toggle per field, each with the same constant name; the pressed
			// state carries whether the text is showing.
			const toggles = page.getByRole('button', { name: 'Show password', exact: true });
			await expect(toggles).toHaveCount(3);
			const toggle = toggles.first();
			await expect(toggle).toHaveAttribute('aria-pressed', 'false');
			await expect(current).toHaveAttribute('type', 'password');

			await toggle.click();
			await expect(toggle).toHaveAttribute('aria-pressed', 'true');
			await expect(current).toHaveAttribute('type', 'text');
			await expect(current).toHaveValue(created.temp_password);
			// Same field, same autocomplete token, same accessible name.
			await expect(current).toHaveAttribute('autocomplete', 'current-password');
			await expect(toggle).toHaveAccessibleName('Show password');

			// A 24px minimum target (WCAG 2.5.8).
			const box = await toggle.boundingBox();
			expect(box && box.width >= 24 && box.height >= 24).toBe(true);

			await toggle.click();
			await expect(current).toHaveAttribute('type', 'password');
		} finally {
			await deleteTestUser(page, created.id);
		}
	});
});

test.describe('/login show-password', () => {
	test('the sign-in field uses the same toggle, and signing in still works with it on', async ({
		page,
		tenantAdmin
	}) => {
		await page.goto('/login');
		const password = page.getByLabel('Password', { exact: true });
		const toggle = page.getByRole('button', { name: 'Show password', exact: true });
		await page.locator('input[type="email"]').fill(tenantAdmin.email);
		await password.fill(tenantAdmin.password);
		await toggle.click();
		await expect(toggle).toHaveAttribute('aria-pressed', 'true');
		await expect(password).toHaveAttribute('type', 'text');
		await expect(password).toHaveAttribute('autocomplete', 'current-password');

		await page.locator('form button[type="submit"]').click();
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 10_000 });
	});
});
