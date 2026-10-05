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
 *     `#lib/actions/focusTrap` action
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

	test('the sidebar tells assistive tech which page is current (1.3.1 / 4.1.2)', async ({
		page
	}) => {
		// The highlighted nav item was colour alone: nothing told a screen
		// reader which link was "you are here".
		await page.goto('/payments');
		const primary = page.getByRole('navigation', { name: 'Primary' });
		await expect(primary).toBeVisible();
		const current = primary.locator('[aria-current]');
		await expect(current).toHaveCount(1);
		await expect(current).toHaveAttribute('aria-current', 'page');
		await expect(current).toHaveAttribute('href', /\/payments$/);
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

	test('a dialog whose trigger vanished lands focus in <main>, not on <body> (2.4.3)', async ({
		page
	}) => {
		// The other half of focus restoration. Approving a pending bank-change
		// request from its dialog filters the row — and with it the button that
		// opened the dialog — out of the Pending list in the same render flush
		// that closes the dialog. `focusTrap` used to call `.focus()` on that
		// button synchronously, which "worked" and was then lost to <body> when
		// the row unmounted: a keyboard / screen-reader user was thrown back to
		// the top of the document, above the sidebar. Every read is stubbed on
		// its exact pathname (docs/decisions.md §190) so the row exists on any
		// tenant and the approval never touches a real vendor.
		const req = {
			id: '00000000-0000-4000-b000-0000000000f1',
			vendor_id: '00000000-0000-4000-b001-0000000000f1',
			vendor_name: 'Focus Return Supplies',
			change_type: 'bank_details',
			status: 'pending',
			proposed_value: { bank_name: 'Focus Bank', account_last4: '1234' },
			// A portal-submitted request, so the admin is not the proposer and
			// the segregation-of-duties gate leaves Approve enabled.
			requested_by_vendor_user_id: '00000000-0000-4000-b002-0000000000f1',
			requested_by_user_id: null,
			reviewed_by_user_id: null,
			reviewed_at: null,
			review_note: null,
			created_at: '2026-01-01T00:00:00Z'
		};
		let approved = false;
		await page.route(
			(url) =>
				url.pathname === '/api/vendors/change-requests' ||
				url.pathname === '/api/vendors/change-requests/counts' ||
				url.pathname === `/api/vendors/${req.vendor_id}/change-requests` ||
				url.pathname === `/api/vendors/change-requests/${req.id}/approve`,
			(route) => {
				const path = new URL(route.request().url()).pathname;
				if (path.endsWith('/counts')) {
					return route.fulfill({
						json: { pending: approved ? 0 : 1, approved: approved ? 1 : 0, rejected: 0, all: 1 }
					});
				}
				if (path.endsWith('/approve')) {
					approved = true;
					return route.fulfill({
						json: { ...req, status: 'approved', reviewed_at: '2026-01-02T00:00:00Z' }
					});
				}
				if (path === '/api/vendors/change-requests') {
					return route.fulfill({
						json: { items: approved ? [] : [req], total: approved ? 0 : 1, page: 1, page_size: 25 }
					});
				}
				return route.fulfill({ json: [req] });
			}
		);

		await page.goto('/vendors/change-requests');
		const opener = page.getByRole('button', { name: /Focus Return Supplies/ });
		await opener.click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toBeVisible();

		// Armed two-click: the first press arms, the second decides.
		const approve = dialog.getByRole('button', { name: /approve/i });
		await approve.click();
		await approve.click();
		await expect(dialog).toBeHidden();
		await expect(opener).toHaveCount(0);

		await expect(page.locator('main#main-content')).toBeFocused();
	});
});
