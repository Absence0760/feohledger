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

		await page.keyboard.press('Tab');
		await expect(firstEntry).toBeFocused();
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
