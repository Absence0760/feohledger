import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * The legal documents, axe-checked at a width where their tables overflow.
 *
 * `a11y/axe.spec.ts` already covers these routes at the default viewport. This
 * file exists because that was not enough: the sub-processor register alone
 * carries twelve tables, each wrapped in an `overflow-x: auto` scroller to keep
 * the DOCUMENT from widening (WCAG 1.4.10 Reflow) — and a scroller only fires
 * `scrollable-region-focusable` once its content actually overflows.
 *
 * At 1280px the tables fit, so the rule never ran and the suite was green. In
 * CI the same page rendered slightly wider, the table overflowed, and a real
 * WCAG 2.1.1 violation appeared on a surface that had passed locally every
 * time. Solving reflow by hiding overflow behind a mouse-pannable region is
 * precisely the trade that creates a keyboard trap, so the two criteria have to
 * be tested together, at a width where both are live.
 *
 * 600px is chosen to sit below the tables' 32rem (512px) min-width plus the
 * page gutter, so every scroller is genuinely scrolling.
 */

const PATHS = [
	'/legal',
	'/legal/privacy',
	'/legal/terms',
	'/legal/dpa',
	'/legal/sub-processors',
	'/legal/accessibility',
	'/legal/cookies'
];

/**
 * The contents list, located by its `<nav>`'s accessible name. The `<summary>`
 * that discloses it has no role Playwright's ARIA mapping computes, so it is
 * addressed by element rather than by role.
 */
const CONTENTS = 'nav[aria-label="Sections of this document"]';

type ScrollWatch = { __scrollSettled?: Promise<void> };

/**
 * Start watching for the next document scroll to finish. Resolves on the
 * browser's `scrollend`, or — when the action that follows scrolls nothing at
 * all — once three frames have passed with no `scroll` event, since a smooth
 * scroll emits one on its first frame.
 */
async function armScrollSettled(page: Page): Promise<void> {
	await page.evaluate(() => {
		(window as ScrollWatch).__scrollSettled = new Promise<void>((resolve) => {
			let scrolled = false;
			addEventListener('scroll', () => (scrolled = true), { once: true });
			addEventListener('scrollend', () => resolve(), { once: true });
			let frames = 0;
			const idle = () => {
				if (scrolled) return;
				if (++frames >= 3) resolve();
				else requestAnimationFrame(idle);
			};
			// Counted from the next input, not from now: the first frame starts
			// once `keyboard.press` has been dispatched.
			addEventListener('keydown', () => requestAnimationFrame(idle), { once: true });
		});
	});
}

async function scrollSettled(page: Page): Promise<void> {
	await page.evaluate(() => (window as ScrollWatch).__scrollSettled);
}

test.describe('legal documents — accessibility where the tables overflow', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	for (const path of PATHS) {
		test(`${path} has no axe violations at 600px`, async ({ page }) => {
			await page.setViewportSize({ width: 600, height: 900 });
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}
});

test.describe('legal documents — the contents list on a narrow viewport', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('the collapsed contents opens and navigates from the keyboard alone at 320px', async ({
		page
	}) => {
		// 320px is the width 1.4.10 names, and below the rail's breakpoint the
		// contents list is the one piece of chrome this change put between a
		// reader and the document. So both halves have to hold with no pointer
		// at all: opening it, and jumping from it.
		await page.setViewportSize({ width: 320, height: 720 });
		await page.goto('/legal/dpa');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

		const summary = page.locator(`${CONTENTS} summary`);
		const firstEntry = page.locator(`${CONTENTS} a`).first();

		// Positive assertion FIRST. The nav is derived after mount, so a bare
		// `toBeHidden()` would pass against a document that has not rendered it
		// yet and assert nothing (`frontend/CLAUDE.md` § `networkidle`).
		await expect(summary).toBeVisible();
		await expect(firstEntry).toBeHidden();

		// The next tab stop after the document's own back link, rather than a
		// count of Tabs from the top of the page — that count would go stale the
		// moment anything is added above it, and this stays true.
		await page.getByRole('link', { name: '← All legal documents' }).focus();
		await page.keyboard.press('Tab');
		await expect(summary).toBeFocused();

		await page.keyboard.press('Enter');
		await expect(firstEntry).toBeVisible();

		// Twenty-one wrapped section titles, expanded, at 320px. The existing
		// sweep in `legal/pages.spec.ts` measures these documents with the list
		// CLOSED, so this is the only place the open one is measured — and a
		// `white-space: nowrap` on an entry is exactly the mistake that once
		// pushed this document 163px past the viewport.
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		);
		expect(
			overflow,
			`the open contents overflows the viewport by ${overflow}px (WCAG 1.4.10)`
		).toBeLessThanOrEqual(0);

		// Tabbing onto the first entry SCROLLS the page — at 320×720 the consent
		// banner reserves ~460px of `scroll-padding-bottom`, so the entry sits
		// behind it until focus brings it up (2.4.11) — and under the page's
		// `scroll-behavior: smooth` that scroll is a ~500ms animation. Pressing
		// Enter while it is still running races two smooth scrolls in Chromium:
		// about one run in six the fragment jump is dropped and the focus scroll
		// finishes instead, leaving `#scope` a full screen below the viewport
		// (CI run 37287438662; 5 of 24 locally, 0 of 40 once the wait below was
		// added). A reader sees the page move and acts when it stops, so wait for
		// that: the browser's own `scrollend`, armed before the Tab so a scroll
		// that finishes quickly cannot be missed.
		await armScrollSettled(page);
		await page.keyboard.press('Tab');
		await expect(firstEntry).toBeFocused();
		await scrollSettled(page);
		await page.keyboard.press('Enter');
		await expect(page).toHaveURL(/\/legal\/dpa#scope$/);
		await expect(page.locator('.legal-page h2#scope')).toBeInViewport();
	});

	test('the EXPANDED contents has no axe violations at 600px', async ({ page }) => {
		// The sweep above measures every document with the list closed, so it
		// never sees the entries at all. Their contrast, the `aria-current`
		// marker's tint/on-tint pair and the 24px target floor (SC 2.5.8) are
		// only live once it is open.
		await page.setViewportSize({ width: 600, height: 900 });
		await page.goto('/legal/dpa');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

		await page.locator(`${CONTENTS} summary`).click();
		await expect(page.locator(`${CONTENTS} a`).first()).toBeVisible();
		await expectNoA11yViolations(page);
	});
});
