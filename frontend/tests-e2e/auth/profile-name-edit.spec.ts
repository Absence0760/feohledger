import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` → Account card.
 *
 * **Full name.** The field is seeded from `auth.user.full_name` by a `$effect`.
 * That effect used to read `fullName` as a tracked dependency *and* write it, so
 * the moment the user backspaced the input to empty the effect re-fired and
 * re-filled it with the stored name — clearing the field to retype was
 * impossible. The seed now reads `fullName` through `untrack`, so it depends on
 * `auth.user` alone.
 *
 * **Roles.** The same card printed the raw slugs (`admin, ap_manager`) where
 * `/admin` rendered `Admin, AP Manager` for the same roles. It now reads
 * `types/admin.ts::ROLE_LABEL_KEYS` and joins with `formatList`, so the
 * assertion is on the label — and it is exact, because `admin` is a substring
 * of nothing in `Admin` and a loose match would pass against the regression.
 */
test.describe('/profile — full-name field', () => {
	test('clearing the field leaves it empty (the seed effect must not fight the user)', async ({
		page
	}) => {
		await page.goto('/profile?section=account');

		const nameInput = page
			.locator('section.card', { hasText: 'Account' })
			.locator('input[autocomplete="name"]');

		// Seeded from the signed-in user.
		await expect(nameInput).not.toHaveValue('');
		const seeded = await nameInput.inputValue();
		expect(seeded.length).toBeGreaterThan(0);

		// Clear it the way a user retyping their name does.
		await nameInput.fill('');
		await expect(nameInput).toHaveValue('');

		// Typing a fresh name must stick, not be overwritten by the seed.
		await nameInput.fill('Renamed Person');
		await expect(nameInput).toHaveValue('Renamed Person');

		// Save stays disabled on an empty field (the real guard against a blank
		// name) — re-clearing proves the field is still the user's to empty.
		await nameInput.fill('');
		await expect(nameInput).toHaveValue('');
		await expect(
			page.locator('section.card', { hasText: 'Account' }).getByRole('button', { name: 'Save' })
		).toBeDisabled();
	});

	test('the roles row reads the role LABEL, not the wire slug', async ({ page }) => {
		await page.goto('/profile?section=account');

		// The worker's storage state is that tenant's admin, seeded with exactly
		// one role (`scripts/seed.py::seed_e2e_control_plane`), so the cell is the
		// whole label and can be asserted exactly.
		await expect(page.getByTestId('profile-roles')).toHaveText('Admin');
	});
});
