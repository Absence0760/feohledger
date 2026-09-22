import { expect, test } from '../fixtures/helpers';

/**
 * `<DataTable>`'s scroll container is a keyboard stop, and the stop works.
 *
 * `reflow.spec.ts` sweeps every route at 320px for a region axe calls
 * unreachable (`scrollable-region-focusable`). That proves the attribute is
 * there; it does not prove the remedy does the job — that a keyboard user who
 * lands on the region can actually see the columns past the right edge. This
 * drives it: focus arrives on the named region, and the arrow key pans it.
 *
 * `/gl-accounts` is used because both seeds give every tenant at least one
 * account (the lean seed's `6000`, the full seed's chart), so the table always
 * has a row to be wide with.
 */
test('a narrow DataTable can be panned from the keyboard (WCAG 2.1.1)', async ({ page }) => {
	await page.setViewportSize({ width: 320, height: 720 });
	await page.goto('/gl-accounts');
	await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

	const region = page.getByRole('region', { name: 'Data table' });
	await expect(region).toHaveAttribute('tabindex', '0');
	// The table must be at least one row deep, or there is nothing to pan.
	await expect(region.locator('tbody tr').first()).toBeVisible();

	// The precondition that makes this test meaningful: at 320px the table IS
	// wider than its container. Without it the assertions below would pass on
	// a region that never needed panning.
	const overflow = await region.evaluate((el) => el.scrollWidth - el.clientWidth);
	expect(overflow, 'the table must overflow its container at 320px').toBeGreaterThan(0);

	await region.focus();
	await expect(region).toBeFocused();
	expect(await region.evaluate((el) => el.scrollLeft)).toBe(0);

	await page.keyboard.press('ArrowRight');
	await expect
		.poll(() => region.evaluate((el) => el.scrollLeft), {
			message: 'ArrowRight on the focused region should scroll it sideways'
		})
		.toBeGreaterThan(0);
});
