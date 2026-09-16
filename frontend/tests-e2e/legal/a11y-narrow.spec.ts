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
