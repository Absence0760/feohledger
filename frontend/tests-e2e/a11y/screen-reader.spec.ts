import { expect, test } from '../fixtures/helpers';

/**
 * Screen-reader navigability guard (the automatable leg of the manual AT pass).
 *
 * axe-core (axe.spec.ts) catches rule violations; this spec asserts the things a
 * screen-reader / keyboard user actually relies on to MOVE through the core
 * invoice → approve → pay flow:
 *   - a skip link and named landmarks to jump by (WCAG 2.4.1 / 1.3.1)
 *   - exactly one page <h1> (2.4.6)
 *   - no positive tabindex hijacking the tab order (2.4.3)
 *   - dialogs trap focus on open and restore it to the trigger on Esc
 *     (2.1.2 No Keyboard Trap / 2.4.3 Focus Order) — exercises the shared
 *     `$lib/actions/focusTrap` action
 *
 * The literal device pass (VoiceOver / NVDA / TalkBack) still has to be run by a
 * human against the checklist in docs/accessibility-screen-reader-checklist.md;
 * this spec locks the programmatic semantics that pass depends on so they can't
 * silently regress.
 *
 * Reuses the per-worker `e2e<N>` tenant + admin storage-state fixtures exactly
 * like axe.spec.ts.
 */

test.describe('screen-reader navigability — core flow', () => {
	test('app shell exposes a skip link, named landmarks, and a single h1', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar').first()).toBeVisible();

		// Skip link (2.4.1 Bypass Blocks) — present and pointing at the main region.
		const skip = page.getByRole('link', { name: /skip to main/i });
		await expect(skip).toHaveCount(1);
		await expect(skip).toHaveAttribute('href', '#main-content');

		// Named landmarks (1.3.1) the AT user navigates by.
		await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
		await expect(page.locator('main#main-content')).toBeVisible();

		// Exactly one top-level heading (2.4.6 / 1.3.1).
		await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
	});

	test('no element uses a positive tabindex (2.4.3)', async ({ page }) => {
		for (const path of ['/', '/invoices', '/vendors', '/payments']) {
			await page.goto(path);
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			const positives = await page.evaluate(() =>
				Array.from(document.querySelectorAll('[tabindex]'))
					.map((el) => Number(el.getAttribute('tabindex')))
					.filter((n) => Number.isFinite(n) && n > 0)
			);
			expect(positives, `positive tabindex values found on ${path}`).toEqual([]);
		}
	});

	// The 320px reflow guard (WCAG 1.4.10) used to live here, checking five
	// hand-listed paths. It is now `a11y/reflow.spec.ts`, which enumerates
	// `src/routes` instead so a new page is covered the day it lands — the five
	// it named were green while six other routes, `/organization` among them,
	// were failing the same criterion.

	test('invoice dialog moves focus in on open and restores it to the trigger on Esc', async ({
		page,
	}) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		// The row-open control is reachable by its accessible name (RowLink
		// "Edit invoice …"), the way an AT user finds it.
		const opener = page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' });
		await opener.focus();
		await expect(opener).toBeFocused();
		await opener.click();

		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();

		// Focus moved INTO the dialog (2.1.2 / 2.4.3, focusTrap action).
		const focusInside = await page.evaluate(() => {
			const dlg = document.querySelector('div.modal[role="dialog"]');
			return !!dlg && !!document.activeElement && dlg.contains(document.activeElement);
		});
		expect(focusInside).toBe(true);

		// Esc closes the dialog and returns focus to the element that opened it.
		await page.keyboard.press('Escape');
		await expect(modal).toBeHidden();
		await expect(opener).toBeFocused();
	});
});
