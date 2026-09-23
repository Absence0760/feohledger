import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` — the section rail.
 *
 * The seven cards used to render on one scroll, which meant every arrival also
 * fetched notification preferences, the passkey list and the signed-in-device
 * list. They are now one panel at a time, addressed by `?section=` through
 * `ui/SettingsRail.svelte`, and each of those three reads waits for its own
 * panel (`docs/decisions.md` §205).
 *
 * The rail is the same primitive `/organization` uses, so the navigation
 * mechanics — fallback, history, `aria-current`, the narrow-viewport disclosure
 * — are covered once, there, in `organization/section-nav.spec.ts`. What is
 * specific to this page is the section list, the default, and the laziness.
 */

/** slug → the panel heading it must show. */
const SECTIONS = [
	['account', 'Account'],
	['password', 'Password'],
	['mfa', 'Two-factor authentication'],
	['passkeys', 'Passkeys'],
	['sessions', 'Signed-in devices'],
	['notifications', 'Notifications'],
	['language', 'Language']
] as const;

test.describe('/profile — section rail', () => {
	test('every section in the rail shows its own panel and only its own', async ({ page }) => {
		for (const [slug, heading] of SECTIONS) {
			await page.goto(`/profile?section=${slug}`);
			await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();

			// A different panel's heading must be gone — otherwise this would pass
			// just as well against the old all-at-once scroll.
			const other = SECTIONS.find(([s]) => s !== slug)![1];
			await expect(page.getByRole('heading', { name: other, exact: true })).toHaveCount(0);
		}
	});

	test('arriving with no section, and with an unknown one, lands on Account', async ({ page }) => {
		await page.goto('/profile');
		await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();

		// A stale bookmark must not render an empty page.
		await page.goto('/profile?section=nonsense');
		await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
	});

	test('the rail marks the active section and moves the mark on navigation', async ({ page }) => {
		await page.goto('/profile?section=account');

		const rail = page.getByRole('navigation', { name: 'Profile section' });
		await expect(rail.locator('[data-section-link="account"]')).toHaveAttribute(
			'aria-current',
			'page'
		);

		await rail.locator('[data-section-link="passkeys"]').click();
		await expect(page.getByRole('heading', { name: 'Passkeys', exact: true })).toBeVisible();
		await expect(page).toHaveURL(/\?section=passkeys/);
		await expect(rail.locator('[data-section-link="passkeys"]')).toHaveAttribute(
			'aria-current',
			'page'
		);
		await expect(rail.locator('[data-section-link="account"]')).not.toHaveAttribute(
			'aria-current',
			'page'
		);
	});

	test('the three panel-scoped reads do not fire until their panel is shown', async ({ page }) => {
		const calls: string[] = [];
		// Record rather than block: the point is which requests the page chose to
		// make, so the panels must still work when they are opened.
		await page.route('**/api/notifications/preferences**', (route) => {
			calls.push('prefs');
			return route.continue();
		});
		await page.route('**/api/auth/mfa/passkey**', (route) => {
			calls.push('passkeys');
			return route.continue();
		});
		await page.route('**/api/auth/sessions**', (route) => {
			calls.push('sessions');
			return route.continue();
		});

		await page.goto('/profile?section=account');
		// A positive assertion first: waiting for an absence against a page that
		// has not hydrated yet passes vacuously (`frontend/CLAUDE.md` § networkidle).
		await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
		expect(calls).toEqual([]);

		const rail = page.getByRole('navigation', { name: 'Profile section' });
		await rail.locator('[data-section-link="sessions"]').click();
		await expect(page.getByRole('heading', { name: 'Signed-in devices', exact: true })).toBeVisible();
		await expect.poll(() => calls).toContain('sessions');
		expect(calls).not.toContain('prefs');
		expect(calls).not.toContain('passkeys');
	});

	test('an unsaved name edit survives a trip to another panel', async ({ page }) => {
		await page.goto('/profile?section=account');

		const nameInput = page
			.locator('section.card', { hasText: 'Account' })
			.locator('input[autocomplete="name"]');
		await nameInput.fill('Unsaved Draft');

		const rail = page.getByRole('navigation', { name: 'Profile section' });
		await rail.locator('[data-section-link="language"]').click();
		await expect(page.getByRole('heading', { name: 'Language', exact: true })).toBeVisible();

		await rail.locator('[data-section-link="account"]').click();
		// Field state lives at page level and only the markup is conditional, so
		// the draft is still here. Moving state into per-panel components would
		// break this, and this assertion is what would catch it. Nothing was
		// saved, so there is nothing to revert.
		await expect(nameInput).toHaveValue('Unsaved Draft');
	});
});
