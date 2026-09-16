import type { Locator, Page } from '@playwright/test';

import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * The marketing landing page (no-tenant origin) — the behaviour its motion
 * design has to guarantee, not how it looks.
 *
 * Every animated element on this page starts hidden or moving, and each of the
 * failure modes below ships a page that looks fine in a screenshot of the fold
 * and is broken further down or for some visitors:
 *
 *  - a scroll reveal that never fires leaves a section at `opacity: 0` —
 *    present in the DOM, so `toBeVisible()` passes (Playwright does not treat
 *    opacity as hidden), and invisible to the reader. Hence the opacity
 *    assertions rather than visibility ones.
 *  - a visitor with `prefers-reduced-motion` must get a page with no hidden
 *    state in it at all, not a page that depends on an observer to un-hide it.
 *  - WCAG 2.2.2 requires the looping animation to be stoppable in-page.
 *  - the count-up must settle on the substantiated figures written in the
 *    markup (`Landing.svelte` § stats), never a rounding of them.
 */

test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

// The consent banner is fixed to the bottom of the viewport and would overlap
// the sections this spec scrolls through. Nothing here is about consent.
test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

const SECTION_HEADINGS = [
	'One platform, from capture to payment.',
	'Invoice to ledger, in three steps.',
	'Built for finance, not marketed at them.',
	'Simple plans. No sales call required.',
	'Spin up your workspace in 30 seconds.'
];

/** The lowest computed opacity on the element or any ancestor — what the reader actually sees. */
async function effectiveOpacity(locator: Locator): Promise<number> {
	return locator.evaluate((el) => {
		let min = 1;
		for (let node: Element | null = el; node; node = node.parentElement) {
			min = Math.min(min, Number(getComputedStyle(node).opacity));
		}
		return min;
	});
}

async function gotoLanding(page: Page) {
	await page.goto('/');
	await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

test.describe('landing — scroll reveal', () => {
	test('every section ends fully opaque once scrolled into view', async ({ page }) => {
		await gotoLanding(page);

		for (const name of SECTION_HEADINGS) {
			const heading = page.getByRole('heading', { level: 2, name });
			await heading.scrollIntoViewIfNeeded();
			// Polled because the reveal is a CSS transition started by an
			// IntersectionObserver: the assertion is on where it SETTLES.
			await expect.poll(() => effectiveOpacity(heading), { message: name }).toBe(1);
		}
	});

	test('reduced motion: nothing is ever put in the hidden state', async ({ page }) => {
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoLanding(page);

		// No scrolling at all: under reduced motion the action must not have
		// hidden anything, so a section far below the fold is already opaque.
		await expect(page.locator('.reveal')).toHaveCount(0);
		for (const name of SECTION_HEADINGS) {
			const heading = page.getByRole('heading', { level: 2, name });
			expect(await effectiveOpacity(heading), name).toBe(1);
		}
	});

	test('the stats settle on the figures written in the markup', async ({ page }) => {
		await gotoLanding(page);
		const values = page.locator('.stat-value');
		await values.first().scrollIntoViewIfNeeded();
		await expect(values).toHaveText(['7', '9', '6', '1–2%']);
	});
});

test.describe('landing — motion control (WCAG 2.2.2)', () => {
	test('the in-page toggle pauses every animation beneath the page root', async ({ page }) => {
		await gotoLanding(page);

		const toggle = page.getByRole('button', { name: 'Pause animation' });
		await expect(toggle).toHaveAttribute('aria-pressed', 'false');

		const card = page.locator('.hero-visual .card');
		await expect.poll(() => card.evaluate((el) => getComputedStyle(el).animationPlayState)).toBe(
			'running'
		);

		await toggle.click();

		// The button now offers the inverse action, and says it is pressed.
		const play = page.getByRole('button', { name: 'Play animation' });
		await expect(play).toHaveAttribute('aria-pressed', 'true');
		await expect(page.locator('.landing')).toHaveAttribute('data-motion', 'paused');
		// The hero card, the adapter rail and the backdrop aurora are three
		// different components; the rule has to reach all of them.
		for (const selector of ['.hero-visual .card', '.rail .track', '.backdrop .aurora-a']) {
			await expect
				.poll(
					() =>
						page
							.locator(selector)
							.first()
							.evaluate((el) => getComputedStyle(el).animationPlayState),
					{ message: selector }
				)
				.toBe('paused');
		}

		await play.click();
		await expect(page.getByRole('button', { name: 'Pause animation' })).toHaveAttribute(
			'aria-pressed',
			'false'
		);
		await expect.poll(() => card.evaluate((el) => getComputedStyle(el).animationPlayState)).toBe(
			'running'
		);
	});
});

test.describe('landing — navigation', () => {
	test('an in-page nav link parks its section below the sticky header', async ({ page }) => {
		// Reduced motion makes the jump instant (app.css forces
		// `scroll-behavior: auto`), so the position can be read once it lands
		// rather than chased through a smooth scroll.
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoLanding(page);

		await page.getByRole('navigation').getByRole('link', { name: 'Features' }).click();
		await expect(page).toHaveURL(/#features$/);

		const header = await page.locator('header.nav').boundingBox();
		const heading = page.getByRole('heading', { level: 2, name: SECTION_HEADINGS[0] });
		await expect
			.poll(async () => (await heading.boundingBox())!.y)
			.toBeGreaterThanOrEqual(header!.y + header!.height);
	});

	test('keyboard focus is never parked under the sticky header (WCAG 2.4.11)', async ({
		page
	}) => {
		// Regression: with no scroll padding, Shift+Tab onto a control sitting
		// just beneath the header left it entirely covered (top 20px, header
		// bottom 74px). This reproduces that exact path — the control parked
		// under the header, focus arriving from the control after it.
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoLanding(page);

		const target = page.getByRole('link', { name: 'View on GitHub' });
		const after = page.getByRole('link', { name: 'Start free', exact: true });
		// Focus first (without scrolling), THEN park the page, so nothing about
		// placing focus can move the scroll position the test depends on.
		await after.evaluate((el) => (el as HTMLElement).focus({ preventScroll: true }));
		const top = await target.evaluate((el) => el.getBoundingClientRect().top + window.scrollY);
		await page.evaluate((y) => window.scrollTo(0, y - 20), top);
		await expect
			.poll(async () => Math.round((await target.boundingBox())!.y))
			.toBe(20);
		await page.keyboard.press('Shift+Tab');

		await expect(target).toBeFocused();
		const header = (await page.locator('header.nav').boundingBox())!;
		await expect
			.poll(async () => (await target.boundingBox())!.y)
			.toBeGreaterThanOrEqual(header.y + header.height);
	});

	test('the header CTA is white on its fill, not the muted link colour', async ({ page }) => {
		// Regression: `.nav-links a` out-specified `.nav-cta` and painted the
		// button's label in --text-muted on --accent-strong (~1.5:1).
		await gotoLanding(page);
		const cta = page.getByRole('link', { name: 'Create workspace' });
		await expect(cta).toHaveCSS('color', 'rgb(255, 255, 255)');
	});
});

test.describe('landing — narrow viewport', () => {
	test.use({ viewport: { width: 390, height: 844 } });

	test('the header CTA fits on one line under its short label', async ({ page }) => {
		await gotoLanding(page);
		// One link, one name: the long label is `display: none` here, which
		// removes it from the accessibility tree as well as from view.
		const cta = page.locator('header.nav a.nav-cta');
		await expect(cta).toHaveAccessibleName('Start free');
		const box = (await cta.boundingBox())!;
		expect(box.height, 'CTA wrapped onto a second line').toBeLessThan(48);
	});

	test('at 320px the header fits, and the home link keeps its name', async ({ page }) => {
		// WCAG 1.4.10 Reflow at the criterion's own width. `.landing` clips
		// horizontal overflow, so an overflowing control is cut off rather than
		// scrollable — which is why this measures the button instead of the
		// document's scroll width.
		await page.setViewportSize({ width: 320, height: 640 });
		await gotoLanding(page);
		const cta = page.locator('header.nav a.nav-cta');
		const box = (await cta.boundingBox())!;
		expect(box.x + box.width).toBeLessThanOrEqual(320);
		// The wordmark is visually hidden at this width; the mark image is
		// decorative, so the clipped text is the link's only accessible name.
		await expect(page.locator('header.nav a.brand')).toHaveAccessibleName('FeohLedger');
	});

	test('the bordered stats panel keeps a gutter inside the viewport', async ({ page }) => {
		// Regression: the panel had inner padding and no outer margin, so on a
		// phone its border ran past both edges of the screen.
		await gotoLanding(page);
		const stats = page.locator('section.stats');
		await stats.scrollIntoViewIfNeeded();
		const box = (await stats.boundingBox())!;
		expect(box.x).toBeGreaterThan(0);
		expect(box.x + box.width).toBeLessThan(390);
	});
});
