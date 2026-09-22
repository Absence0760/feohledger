import { expect, test } from '../fixtures/helpers';

/**
 * /organization → Branding panel, strong-accent contrast advisory.
 *
 * `brandThemeVars` writes `accent_strong_color` straight into the
 * `--accent-strong` custom property, whose one contract is that white text
 * sits on it — every primary button, active filter chip and the skip link read
 * off it. So a tenant picking their logo yellow makes those unreadable app-wide,
 * and the stylesheet token-pairing guard (`src/lib/a11y/tokenPairing.test.ts`)
 * structurally cannot see it: that scan runs over the sources, this override
 * happens at runtime.
 *
 * The advisory is exactly that — advisory. The backend accepts any valid hex
 * and the brand is the tenant's call; what this asserts is that the cost is
 * stated before it's saved. Nothing here saves.
 *
 * Purely client-side (no request is made), so the test types and asserts —
 * no network wait, no timeout. Selectors are the field's placeholder (the
 * `DEFAULT_ACCENT_STRONG` constant the page renders) and the warning's
 * `data-testid`.
 *
 * `?section=branding` because the route shows one panel at a time now. The
 * slug is part of the page's URL contract (`SECTION_GROUPS` in
 * `routes/organization/+page.svelte`); navigating straight to the owning panel
 * keeps this spec about the advisory rather than about the rail, which
 * `section-nav.spec.ts` owns.
 */

test.describe('/organization branding contrast advisory', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization?section=branding');
	});

	test('warns for a colour white text cannot sit on, and clears when it can', async ({
		page
	}) => {
		const card = page.locator('section.card', {
			has: page.getByRole('heading', { name: 'Branding' })
		});
		await expect(card).toBeVisible();

		// The hex text input beside the strong-accent colour picker.
		const strongAccent = card.getByPlaceholder('#3f5fd6');
		await expect(strongAccent).toBeVisible();

		const warning = page.getByTestId('accent-strong-contrast-warning');

		// Nothing typed yet — an empty field is "no colour to judge", not a
		// failure, so the advisory must stay silent.
		await expect(warning).toHaveCount(0);

		// A logo yellow: white on #ffe066 is ~1.3:1.
		await strongAccent.fill('#ffe066');
		await expect(warning).toBeVisible();
		await expect(warning).toContainText('4.5:1');

		// The shipped default clears the bar — the advisory withdraws.
		await strongAccent.fill('#3f5fd6');
		await expect(warning).toHaveCount(0);

		// A half-typed value is not a failure either.
		await strongAccent.fill('#ff');
		await expect(warning).toHaveCount(0);
	});
});
