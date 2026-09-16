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

	test('key pages reflow without horizontal scroll at 320px (WCAG 1.4.10)', async ({ page }) => {
		// At a 320px viewport the sidebar auto-collapses to its icon rail and the
		// content must reflow — no page-level horizontal scrollbar. Data tables
		// are exempt (they scroll inside their own overflow-x container).
		//
		// `/contracts` is here deliberately: it is the only path in this list
		// that sits inside a NAV *group*, so it is the only one that renders
		// `SectionTabs` at all. Every route this test checked before was a
		// top-level link, where `groupForPath` returns null and the tab bar
		// never mounts — which is why a tab row that scrolled the whole document
		// (Billing 9 tabs, Settings 15 before the Governance / Automation split)
		// went unnoticed here. Billing's 9 tabs make it the widest surviving
		// group, so it is the right one to pin.
		//
		// `/organization` is NOT in this list, and that is a known gap rather
		// than an oversight: its own content overflows 320px independently of
		// the tab bar (docs/known-issues.md § Organization settings).
		await page.setViewportSize({ width: 320, height: 720 });
		for (const path of ['/', '/invoices', '/vendors', '/payments', '/contracts']) {
			await page.goto(path);
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow, `horizontal page overflow on ${path} at 320px`).toBeLessThanOrEqual(1);
		}
	});

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
