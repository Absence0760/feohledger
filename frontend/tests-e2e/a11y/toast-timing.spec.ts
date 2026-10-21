import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';

/**
 * `ui/Toast.svelte` in a real page (decisions §307, WCAG 2.2.1 / 2.4.3 /
 * 2.4.11). The queue's rules are unit-tested in `utils/toastQueue.test.ts`;
 * this pins the WIRING those tests cannot see — the hover/focus pause reaching
 * the queue, an error outliving the old five seconds, focus going back where it
 * came from, the region's name, the dismiss target, and the region clearing
 * the bulk-action bar.
 *
 * Time is moved with Playwright's clock (`page.clock`), never a sleep. The
 * toasts come from /vendors' "Sync from ERP": the org's ERP is stubbed to the
 * mock adapter (so the button is offered) and the sync endpoint is stubbed, so
 * nothing is written.
 */

async function offerSync(page: Page, sync: { status: number; json: unknown }) {
	await page.route(
		(url) => url.pathname === '/api/organization',
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const real = await route.fetch();
			const body = await real.json();
			await route.fulfill({
				response: real,
				json: { ...body, settings: { ...body.settings, erp: { type: 'mock', integration_method: 'direct' } } }
			});
		}
	);
	await page.route(
		(url) => url.pathname === '/api/vendors/sync-erp',
		(route) => route.fulfill(sync)
	);
}

const region = (page: Page) => page.getByRole('region', { name: 'Status messages' });

test.describe('toast timing and focus', () => {
	test('an error toast stays until dismissed; focus returns to where it came from', async ({
		page
	}) => {
		await page.clock.install();
		await offerSync(page, { status: 500, json: { detail: 'Sync exploded' } });
		await page.goto('/vendors');

		const sync = page.getByRole('button', { name: 'Sync from ERP' });
		await sync.click();
		const toast = region(page).getByTestId('toast').filter({ hasText: 'Sync exploded' });
		await expect(toast).toBeVisible();

		await page.clock.fastForward(60_000);
		await expect(toast).toBeVisible();

		const dismiss = toast.getByRole('button', { name: 'Dismiss' });
		const box = await dismiss.boundingBox();
		expect(box!.width).toBeGreaterThanOrEqual(24);
		expect(box!.height).toBeGreaterThanOrEqual(24);

		// Keyboard: into the toast from the Sync button, then dismiss.
		await sync.focus();
		await dismiss.focus();
		await page.keyboard.press('Enter');
		await expect(toast).toHaveCount(0);
		await expect(sync).toBeFocused();
	});

	test('a success toast pauses while hovered and resumes after', async ({ page }) => {
		await page.clock.install();
		await offerSync(page, {
			status: 200,
			json: { created: 1, updated: 0, unchanged: 0 }
		});
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Sync from ERP' }).click();

		const toast = region(page).getByTestId('toast').filter({ hasText: /Synced 1 new/ });
		await expect(toast).toBeVisible();
		await toast.hover();
		await page.clock.fastForward(30_000);
		await expect(toast).toBeVisible();

		await page.mouse.move(5, 5);
		await page.clock.fastForward(6_000);
		await expect(toast).toHaveCount(0);
	});

	test('a toast pauses while focus is inside the region', async ({ page }) => {
		await page.clock.install();
		await offerSync(page, { status: 200, json: { created: 1, updated: 0, unchanged: 0 } });
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Sync from ERP' }).click();

		const toast = region(page).getByTestId('toast').filter({ hasText: /Synced 1 new/ });
		await toast.getByRole('button', { name: 'Dismiss' }).focus();
		await page.clock.fastForward(30_000);
		await expect(toast).toBeVisible();
	});

	test('the region rises above the bulk-action bar', async ({ page }) => {
		await offerSync(page, { status: 500, json: { detail: 'Sync exploded' } });
		await page.goto('/vendors');
		await page.locator('table tbody input[type="checkbox"]').first().check();
		const bar = page.getByRole('toolbar');
		await expect(bar).toBeVisible();

		await page.getByRole('button', { name: 'Sync from ERP' }).click();
		const toast = region(page).getByTestId('toast').filter({ hasText: 'Sync exploded' });
		await expect(toast).toBeVisible();

		await expect
			.poll(async () => {
				const t = await toast.boundingBox();
				const b = await bar.boundingBox();
				return t && b ? t.y + t.height <= b.y : false;
			})
			.toBe(true);
	});
});

/**
 * #635 G: on a phone an open dialog's footer sits where the toast corner is,
 * and an error toast no longer times out — so it stayed over the button the
 * reader needed to retry with. `utils/toastPlacement.ts` (unit-tested) moves
 * the stack clear of the dialog's actions; this pins the wiring at the two
 * widths the issue names, with a real error from a real dialog. The bank-
 * details dialog is the tall case: at phone heights it fills 90vh and its
 * footer lands on the corner.
 */
test.describe('toasts clear an open dialog’s actions', () => {
	const detail =
		'The bank change could not be staged because the processor rejected the counterparty.';

	for (const size of [
		{ width: 375, height: 667 },
		{ width: 320, height: 568 }
	]) {
		test(`at ${size.width}px an error toast leaves the dialog's Save uncovered`, async ({
			page
		}) => {
			await page.setViewportSize(size);
			await page.route(
				(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
				(route) => route.fulfill({ status: 500, json: { detail } })
			);
			await page.goto('/vendors');
			await page
				.getByRole('button', { name: /^Bank/ })
				.first()
				.click();
			const dialog = page.getByRole('dialog', { name: 'Vendor bank counterparty' });
			const save = dialog.getByRole('button', { name: 'Save', exact: true });
			await save.click();

			const toast = region(page).getByTestId('toast').filter({ hasText: detail });
			await expect(toast).toBeVisible();
			// The dialog stays open on a failed save: the reader retries from it.
			await expect(dialog).toBeVisible();
			// Measure the toast at rest, not mid slide-in (its transform moves the box).
			await settle(toast);

			await expect
				.poll(async () => {
					const t = await toast.boundingBox();
					const c = await save.boundingBox();
					if (!t || !c) return 'unmeasured';
					const overlap =
						t.x < c.x + c.width && c.x < t.x + t.width && t.y < c.y + c.height && c.y < t.y + t.height;
					return overlap ? 'covered' : 'clear';
				})
				.toBe('clear');

			// The dialog itself reflows: its paired fields used to be wider than
			// it at 320px, so the click on Save scrolled the form sideways.
			expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
			// The toast is on screen, not pushed off it.
			const t = (await toast.boundingBox())!;
			expect(t.y).toBeGreaterThanOrEqual(0);
			expect(t.y + t.height).toBeLessThanOrEqual(size.height);

			// Both operable: Save takes a real pointer hit (trial runs
			// Playwright's "receives events" check — a toast on top fails it),
			// and the toast's 24px dismiss closes it without closing the dialog.
			await save.click({ trial: true });
			const dismiss = toast.getByRole('button', { name: 'Dismiss' });
			const d = (await dismiss.boundingBox())!;
			expect(d.width).toBeGreaterThanOrEqual(24);
			expect(d.height).toBeGreaterThanOrEqual(24);
			await dismiss.click();
			await expect(toast).toHaveCount(0);
			await expect(dialog).toBeVisible();
			await save.click();
			await expect(toast).toBeVisible();
		});
	}
});

/**
 * A persistent error toast while a modal dialog is open (WCAG 2.1.1, 2.4.11,
 * 4.1.3). The dialog's focus trap used to keep Tab inside the dialog while the
 * toast region sat outside it, so an error that never times out could not be
 * reached or dismissed by keyboard until the dialog closed — and `aria-modal`
 * hides content outside the dialog from assistive tech. `ui/Toast.svelte` now
 * hosts the region inside the topmost modal dialog, and `actions/focusTrap`
 * puts its dismiss buttons after the dialog's own controls in the Tab cycle
 * (unit-tested in `actions/focusTrap.test.ts`). Pinned here in a real browser
 * for `ui/Modal` (the bank dialog, with a `.modal-footer`) and the hand-rolled
 * `InvoiceModal` (no `.modal-footer`), at desktop and phone widths.
 */
const SIZES = [
	{ width: 1280, height: 800 },
	{ width: 375, height: 667 },
	{ width: 320, height: 568 }
];

function overlapping(
	a: { x: number; y: number; width: number; height: number },
	b: { x: number; y: number; width: number; height: number }
) {
	return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** True when the focused element is inside `dialog` (the page behind never). */
async function focusInDialog(page: Page, dialogName: RegExp | string) {
	const want =
		typeof dialogName === 'string' ? { exact: dialogName } : { pattern: dialogName.source };
	return page.evaluate(({ exact, pattern }) => {
		const el = document.activeElement;
		const d = el?.closest('[role="dialog"][aria-modal="true"]');
		if (!d) return false;
		const label = d.getAttribute('aria-label') ?? '';
		return exact !== undefined ? label === exact : new RegExp(pattern!).test(label);
	}, want as { exact?: string; pattern?: string });
}

/** Press Tab until a toast's dismiss button has focus, never leaving the dialog. */
async function tabToDismiss(page: Page, dialogName: RegExp | string) {
	for (let i = 0; i < 80; i++) {
		await page.keyboard.press('Tab');
		expect(await focusInDialog(page, dialogName)).toBe(true);
		if (await page.evaluate(() => document.activeElement?.classList.contains('toast-dismiss'))) return;
	}
	throw new Error('Tab never reached a toast dismiss button');
}

async function expectKeyboardDismissal(page: Page, dialogName: RegExp | string, text: string) {
	const toast = region(page).getByTestId('toast').filter({ hasText: text });
	const dismiss = toast.getByRole('button', { name: 'Dismiss' });

	// The region is inside the modal dialog, so aria-modal does not hide it.
	expect(
		await region(page).evaluate((el) => !!el.closest('[role="dialog"][aria-modal="true"]'))
	).toBe(true);

	await tabToDismiss(page, dialogName);
	await expect(dismiss).toBeFocused();
	// Shift+Tab goes back into the dialog's own controls; Tab returns.
	await page.keyboard.press('Shift+Tab');
	expect(await focusInDialog(page, dialogName)).toBe(true);
	expect(await page.evaluate(() => !document.activeElement?.closest('.toast-region'))).toBe(true);
	await page.keyboard.press('Tab');
	await expect(dismiss).toBeFocused();

	await page.keyboard.press('Enter');
	await expect(toast).toHaveCount(0);
	// Focus is handed back into the dialog, never to the page behind it.
	expect(await focusInDialog(page, dialogName)).toBe(true);
	expect(await page.evaluate(() => document.activeElement !== document.body)).toBe(true);
}

/** Measure the toast at rest: its slide-in transform moves the box. A toast
 *  that arrived while the region was moving into the dialog restarts its
 *  animation (the first one is cancelled), so wait for none to be running
 *  rather than awaiting the first one's `finished`. */
async function settle(toast: ReturnType<Page['locator']>) {
	await expect.poll(() => toast.evaluate((el) => el.getAnimations().length)).toBe(0);
}

test.describe('a toast inside an open dialog is reachable by keyboard', () => {
	const detail = 'The bank change could not be staged: the processor refused it.';

	for (const size of SIZES) {
		test(`ui/Modal (bank dialog) at ${size.width}px`, async ({ page }) => {
			await page.setViewportSize(size);
			await page.route(
				(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
				(route) => route.fulfill({ status: 500, json: { detail } })
			);
			await page.goto('/vendors');
			await page.getByRole('button', { name: /^Bank/ }).first().click();
			const name = 'Vendor bank counterparty';
			const dialog = page.getByRole('dialog', { name });
			const save = dialog.getByRole('button', { name: 'Save', exact: true });
			await save.click();
			const toast = region(page).getByTestId('toast').filter({ hasText: detail });
			await expect(toast).toBeVisible();
			await settle(toast);

			// The primary button is not covered.
			await expect
				.poll(async () => overlapping((await toast.boundingBox())!, (await save.boundingBox())!))
				.toBe(false);

			// 2.4.11: the last field above the footer, focused, is not under the stack.
			const lastField = dialog.locator('input').last();
			await lastField.focus();
			await expect
				.poll(async () => overlapping((await toast.boundingBox())!, (await lastField.boundingBox())!))
				.toBe(false);
			await save.click({ trial: true });

			// Hosted in the dialog it is still in the viewport corner: the
			// backdrop it now lives under is a fixed, full-viewport box.
			const t = (await toast.boundingBox())!;
			expect(Math.abs(t.x + t.width - (size.width - 16))).toBeLessThanOrEqual(1);

			await expectKeyboardDismissal(page, name, detail);
			await expect(dialog).toBeVisible();
		});

		test(`InvoiceModal (no .modal-footer) at ${size.width}px`, async ({ page }) => {
			await page.setViewportSize(size);
			const failure = 'The invoice could not be saved: the ledger is closed.';
			await page.route(
				(url) => /^\/api\/invoices\/[^/]+$/.test(url.pathname),
				(route) =>
					route.request().method() === 'PATCH'
						? route.fulfill({ status: 500, json: { detail: failure } })
						: route.fallback()
			);
			await page.goto('/invoices?status=new');
			// By keyboard, as row-click.spec does: at phone widths the row's
			// link sits in the table's horizontal scroll.
			const open = page
				.locator('table tbody tr')
				.first()
				.getByRole('button', { name: /^Edit invoice/ });
			await open.focus();
			await open.press('Enter');
			const dialog = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
			await expect(dialog).toBeVisible();
			await expect(dialog).toHaveAttribute('aria-modal', 'true');
			const save = dialog.getByRole('button', { name: 'Save', exact: true });
			await save.click();
			const toast = region(page).getByTestId('toast').filter({ hasText: failure });
			await expect(toast).toBeVisible();
			await settle(toast);
			await expect(dialog).toBeVisible();
			await expect
				.poll(async () => overlapping((await toast.boundingBox())!, (await save.boundingBox())!))
				.toBe(false);

			await expectKeyboardDismissal(page, /^Edit invoice/, failure);
			await expect(dialog).toBeVisible();
		});
	}

	test('a capped, scrolling stack: every dismiss is reachable and scrolled into view', async ({
		page
	}) => {
		await page.setViewportSize({ width: 320, height: 568 });
		// Three distinct long errors (a repeat would replace, not stack) — far
		// taller together than the room beside the bank dialog's footer.
		let n = 0;
		const long =
			'the payment processor rejected the counterparty because the account and routing details do not match its records for this vendor, so nothing was staged';
		await page.route(
			(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
			(route) => route.fulfill({ status: 500, json: { detail: `Attempt ${++n}: ${long}` } })
		);
		await page.goto('/vendors');
		await page.getByRole('button', { name: /^Bank/ }).first().click();
		const name = 'Vendor bank counterparty';
		const dialog = page.getByRole('dialog', { name });
		const save = dialog.getByRole('button', { name: 'Save', exact: true });
		for (let i = 1; i <= 3; i++) {
			await save.click();
			await expect(region(page).getByTestId('toast').filter({ hasText: `Attempt ${i}:` })).toBeVisible();
		}
		const stack = region(page);
		await expect(stack).toHaveClass(/capped/);
		expect(await stack.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
		await expect
			.poll(async () => overlapping((await stack.boundingBox())!, (await save.boundingBox())!))
			.toBe(false);

		// Tab through all three dismiss buttons; each is scrolled into the
		// capped region's visible box when it takes focus.
		await tabToDismiss(page, name);
		for (let i = 0; i < 3; i++) {
			const visible = await page.evaluate(() => {
				const btn = document.activeElement as HTMLElement;
				const r = btn.getBoundingClientRect();
				const c = btn.closest('.toast-region')!.getBoundingClientRect();
				return (
					btn.classList.contains('toast-dismiss') &&
					r.top >= c.top - 1 &&
					r.bottom <= c.bottom + 1 &&
					r.bottom <= window.innerHeight
				);
			});
			expect(visible).toBe(true);
			if (i < 2) await page.keyboard.press('Tab');
		}
		// Past the last toast, Tab wraps to the dialog's first control.
		await page.keyboard.press('Tab');
		expect(await focusInDialog(page, name)).toBe(true);
		expect(await page.evaluate(() => !document.activeElement?.closest('.toast-region'))).toBe(true);

		// Back into the stack, then Enter three times: each dismissal hands focus
		// to the next toast, and the last hands it back into the dialog.
		await page.keyboard.press('Shift+Tab');
		for (let left = 2; left >= 0; left--) {
			await page.keyboard.press('Enter');
			await expect(stack.getByTestId('toast')).toHaveCount(left);
			expect(await focusInDialog(page, name)).toBe(true);
		}
		expect(await page.evaluate(() => !document.activeElement?.closest('.toast-region'))).toBe(true);
	});

	test('a toast already showing moves into a dialog opened after it', async ({ page }) => {
		await offerSync(page, { status: 500, json: { detail: 'Sync exploded' } });
		await page.goto('/vendors');
		await page.getByRole('button', { name: 'Sync from ERP' }).click();
		const toast = region(page).getByTestId('toast').filter({ hasText: 'Sync exploded' });
		await expect(toast).toBeVisible();

		await page.getByRole('button', { name: /^Bank/ }).first().click();
		const name = 'Vendor bank counterparty';
		await expect(page.getByRole('dialog', { name })).toBeVisible();
		// The dialog takes focus on its first control, never the toast.
		expect(await focusInDialog(page, name)).toBe(true);
		expect(await page.evaluate(() => !document.activeElement?.closest('.toast-region'))).toBe(true);
		await expect
			.poll(() =>
				region(page).evaluate((el) => !!el.closest('[role="dialog"][aria-modal="true"]'))
			)
			.toBe(true);
		await expectKeyboardDismissal(page, name, 'Sync exploded');
	});

	test('a toast raised as its dialog closes is inserted where it is announced', async ({
		page
	}) => {
		// "Submitted" + close in one update. Whatever order that update runs in,
		// the toast's element must be inserted into the region while the region
		// is connected and outside the closed dialog: live regions announce
		// insertions, not a subtree re-attached with the toast already in it (a
		// detached insertion is invisible to this observer, so it fails here).
		await page.route(
			(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
			(route) => route.fulfill({ status: 200, json: { id: 'cr-1', status: 'pending' } })
		);
		await page.goto('/vendors');
		await page.getByRole('button', { name: /^Bank/ }).first().click();
		const dialog = page.getByRole('dialog', { name: 'Vendor bank counterparty' });
		await expect(dialog).toBeVisible();
		await expect
			.poll(() => region(page).evaluate((el) => !!el.closest('[role="dialog"]')))
			.toBe(true);
		await page.evaluate(() => {
			const w = window as unknown as { __heard: boolean[] };
			w.__heard = [];
			new MutationObserver((records) => {
				for (const r of records)
					for (const n of r.addedNodes)
						if (n instanceof HTMLElement && n.matches('[data-testid="toast"]'))
							w.__heard.push(n.isConnected && !n.closest('[role="dialog"]'));
			}).observe(document.body, { childList: true, subtree: true });
		});
		await dialog.getByRole('button', { name: 'Save', exact: true }).click();
		await expect(dialog).toHaveCount(0);
		const toast = region(page).getByTestId('toast').filter({ hasText: /submitted/i });
		await expect(toast).toBeVisible();
		await expect
			.poll(() => page.evaluate(() => (window as unknown as { __heard: boolean[] }).__heard))
			.toContain(true);
	});

	test('Escape on a focused toast dismisses the toast, not the dialog', async ({ page }) => {
		await page.route(
			(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
			(route) => route.fulfill({ status: 500, json: { detail } })
		);
		await page.goto('/vendors');
		await page.getByRole('button', { name: /^Bank/ }).first().click();
		const name = 'Vendor bank counterparty';
		const dialog = page.getByRole('dialog', { name });
		await dialog.getByRole('button', { name: 'Save', exact: true }).click();
		const toast = region(page).getByTestId('toast').filter({ hasText: detail });
		await expect(toast).toBeVisible();
		await tabToDismiss(page, name);
		await page.keyboard.press('Escape');
		await expect(toast).toHaveCount(0);
		await expect(dialog).toBeVisible();
		expect(await focusInDialog(page, name)).toBe(true);
	});

	test('the region goes back to the page when the dialog closes', async ({ page }) => {
		await page.route(
			(url) => /^\/api\/vendors\/[^/]+\/bank-change$/.test(url.pathname),
			(route) => route.fulfill({ status: 500, json: { detail } })
		);
		await page.goto('/vendors');
		const trigger = page.getByRole('button', { name: /^Bank/ }).first();
		await trigger.click();
		const dialog = page.getByRole('dialog', { name: 'Vendor bank counterparty' });
		await dialog.getByRole('button', { name: 'Save', exact: true }).click();
		const toast = region(page).getByTestId('toast').filter({ hasText: detail });
		await expect(toast).toBeVisible();
		// Save was disabled while it ran, which drops focus: the trap brings it
		// back into the dialog, so Escape (handled on the dialog) still closes it.
		expect(await focusInDialog(page, 'Vendor bank counterparty')).toBe(true);
		await page.keyboard.press('Escape');
		await expect(dialog).toHaveCount(0);
		// Still showing (an error persists), now outside any dialog, and focus
		// went back to the trigger rather than to the toast.
		await expect(toast).toBeVisible();
		expect(await region(page).evaluate((el) => !el.closest('[role="dialog"]'))).toBe(true);
		await expect(trigger).toBeFocused();
	});
});
