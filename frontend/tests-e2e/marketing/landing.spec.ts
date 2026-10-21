import type { Locator, Page } from '@playwright/test';

import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

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
 *  - WCAG 2.2.2: with no pause control on the page, every animation must
 *    finish on its own inside five seconds.
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
	'Set up your workspace in minutes.'
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

/**
 * Every control in the header's bar lies inside the viewport. `.landing` clips
 * horizontal overflow, so a control past the edge is cut off rather than
 * scrollable — the document's scroll width would never show it.
 */
async function expectHeaderFits(page: Page, width: number) {
	const controls = page.locator('header.nav').locator(':scope > a, :scope > .nav-end > *');
	const boxes = await controls.evaluateAll((els) =>
		els
			.filter((el) => getComputedStyle(el).display !== 'none')
			.map((el) => {
				const r = el.getBoundingClientRect();
				return { what: el.className || el.tagName, left: r.left, right: r.right, top: r.top };
			})
	);
	expect(boxes.length, 'the header bar has controls to measure').toBeGreaterThan(1);
	for (const b of boxes) {
		expect(b.left, `${b.what} starts inside the viewport`).toBeGreaterThanOrEqual(0);
		expect(b.right, `${b.what} ends inside the viewport`).toBeLessThanOrEqual(width);
	}
	// One row: nothing wrapped onto a second line of the bar.
	const tops = boxes.map((b) => Math.round(b.top));
	expect(Math.max(...tops) - Math.min(...tops), 'the bar stayed on one line').toBeLessThan(16);
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
		await expect(values).toHaveText(['12', '9', '6', '1–2%']);
	});
});

test.describe('landing — motion ends on its own (WCAG 2.2.2)', () => {
	// The page has no pause control: it meets 2.2.2 by never moving for more
	// than five seconds (decisions §279). This reads what the browser is
	// actually running — every CSS animation and transition, including the
	// per-element `calc()` delays the static guard
	// (`src/lib/a11y/marketingMotion.test.ts`) cannot resolve — and checks each
	// one's active end time against the budget.
	test('every animation on the page is finite and finishes within five seconds', async ({
		page
	}) => {
		await gotoLanding(page);
		// Bring each section in, so the scroll reveals are running too.
		for (const name of SECTION_HEADINGS) {
			await page.getByRole('heading', { level: 2, name }).scrollIntoViewIfNeeded();
		}

		const running = await page.evaluate(() =>
			document.getAnimations().map((a) => {
				const timing = a.effect!.getComputedTiming();
				const target = (a.effect as KeyframeEffect).target as Element | null;
				return {
					name:
						(a as CSSAnimation).animationName ?? (a as CSSTransition).transitionProperty ?? a.id,
					target: target ? `${target.tagName.toLowerCase()}.${target.className}` : '?',
					endMs: Number(timing.endTime),
					iterations: Number(timing.iterations)
				};
			})
		);

		// Not vacuous: the hero, the aurora and the headline sheen all animate.
		const names = running.map((a) => a.name).join(' ');
		for (const expected of ['card-lift', 'drift-a', 'sheen', 'stop-live']) {
			expect(names, expected).toContain(expected);
		}
		expect(running.filter((a) => !Number.isFinite(a.iterations) || a.endMs > 5000)).toEqual([]);
	});

	test('the adapter row is static: every name shown once, nothing scrolling', async ({ page }) => {
		await gotoLanding(page);
		const list = page.locator('.rail .set');
		await expect(list).toHaveCount(1);
		await expect(list.getByText('NetSuite', { exact: true })).toHaveCount(1);
		expect(
			await list.evaluate((el) => el.getAnimations({ subtree: true }).length),
			'the adapter row runs no animation'
		).toBe(0);
	});

	test('there is no pause control left to find', async ({ page }) => {
		await gotoLanding(page);
		await expect(page.getByRole('button', { name: /pause animation|play animation/i })).toHaveCount(0);
		await expect(page.locator('[data-motion]')).toHaveCount(0);
	});
});

test.describe('landing — atmosphere layer', () => {
	test('inherits nothing from the global modal overlay', async ({ page }) => {
		// Regression: the layer's root class was `backdrop`, which app.css also
		// defines globally as the modal overlay — so it silently took a 50% black
		// fill and a backdrop-filter on a fixed full-viewport element. A global
		// class reaches scoped components, so the rename is the fix and this is
		// its pin.
		await gotoLanding(page);
		const layer = page.locator('.atmosphere');
		await expect(layer).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
		await expect(layer).toHaveCSS('backdrop-filter', 'none');
		await expect(layer).toHaveCSS('padding-top', '0px');
	});
});

test.describe('landing — decorative art', () => {
	test('the tally render cannot be dragged off the page', async ({ page }) => {
		// Every `<img>` is draggable by default, so the illustration peeled away
		// under the cursor trailing a ghost image — on the first screen a visitor
		// sees. The attribute stops the drag; `-webkit-user-drag` covers
		// WebKit/Blink's own image-drag path, which the attribute does not, and
		// `user-select: none` stops the highlight that makes a half-started drag
		// read as a misclick.
		//
		// Deliberately NOT asserting `pointer-events: none`: that would also
		// prevent the drag, but by swallowing hit-testing entirely — more than is
		// needed, and it would silently break the art the day it gains a link.
		//
		// The invoice preview in `InvoiceModal` is the opposite case and stays
		// draggable on purpose: that image IS the user's own document.
		await gotoLanding(page);
		const art = page.locator('.tally-art img');
		await expect(art).toHaveAttribute('draggable', 'false');
		await expect(art).toHaveCSS('user-select', 'none');
		expect(await art.evaluate((el: HTMLImageElement) => el.draggable)).toBe(false);
	});
});

test.describe('landing — navigation', () => {
	test('an in-page nav link parks its section below the sticky header', async ({ page }) => {
		// Reduced motion makes the jump instant (app.css forces
		// `scroll-behavior: auto`), so the position can be read once it lands
		// rather than chased through a smooth scroll.
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoLanding(page);

		await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Features' }).click();
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

		// Two adjacent cards' calls to action on the pricing grid: nothing
		// focusable sits between the Free card's link and the next card's.
		const pricing = page.locator('#pricing');
		const target = pricing.locator('[data-plan="free"] a.plan-cta');
		const after = pricing.locator('.plan').nth(1).locator('a.plan-cta');
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
		const cta = page.locator('header.nav').getByRole('link', { name: 'Create workspace' });
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
		// scrollable — which is why this measures the controls instead of the
		// document's scroll width.
		await page.setViewportSize({ width: 320, height: 640 });
		await gotoLanding(page);
		await expectHeaderFits(page, 320);
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


test.describe('landing — signing in from the main domain (#635 finding 4)', () => {
	// The deployment's tenant URL template, stubbed so the redirect target is a
	// host this spec controls rather than whatever the local backend is set to.
	const TEMPLATE = 'https://{slug}.feoh.example';

	async function stubPublicConfig(page: Page, status = 200) {
		await page.route(
			(url) => url.pathname === '/api/public-config',
			(route) =>
				status === 200
					? route.fulfill({
							json: { hcaptcha_sitekey: '', tenant_url_template: TEMPLATE, signup_enabled: true }
						})
					: route.fulfill({ status, json: { detail: 'unavailable' } })
		);
	}

	test('the header and the footer each have a Sign in link to find-your-workspace', async ({ page }) => {
		await gotoLanding(page);
		await expect(page.locator('header.nav').getByRole('link', { name: 'Sign in' })).toHaveAttribute(
			'href',
			'/find-workspace'
		);
		await expect(
			page.getByRole('navigation', { name: 'Footer' }).getByRole('link', { name: 'Sign in' })
		).toHaveAttribute('href', '/find-workspace');
	});

	test("Sign in → the workspace address → that workspace's own sign-in page", async ({ page }) => {
		await stubPublicConfig(page);
		// The workspace host is not ours to load; answer it so the navigation
		// lands somewhere the assertion can read.
		await page.route('https://acme.feoh.example/**', (route) =>
			route.fulfill({ contentType: 'text/html', body: '<title>acme</title>' })
		);
		await gotoLanding(page);
		await page.locator('header.nav').getByRole('link', { name: 'Sign in' }).click();

		await expect(page.getByRole('heading', { level: 1, name: 'Sign in to your workspace' })).toBeVisible();
		await expect(page).toHaveURL(/\/find-workspace$/);
		const field = page.getByLabel('Workspace address');
		// The platform host is shown beside the field, from the template.
		await expect(page.locator('#workspace-suffix')).toHaveText('.feoh.example');

		// A pasted full address is taken down to its workspace name.
		await field.fill('https://Acme.feoh.example/invoices');
		await page.getByRole('button', { name: 'Continue' }).click();
		await page.waitForURL('https://acme.feoh.example/login');
	});

	test('an address that cannot be a workspace is refused in place, with the reason', async ({ page }) => {
		await stubPublicConfig(page);
		await page.goto('/find-workspace');
		const field = page.getByLabel('Workspace address');
		await field.fill('acme_corp');
		await page.getByRole('button', { name: 'Continue' }).click();

		await expect(field).toHaveAttribute('aria-invalid', 'true');
		await expect(field).toHaveAccessibleDescription(/Use only the first part of the address/);
		await expect(field).toBeFocused();
		await expect(page).toHaveURL(/\/find-workspace$/);
		await expectNoA11yViolations(page);
	});

	test("a company's own domain is offered back as a link, not refused or followed", async ({ page }) => {
		// A workspace on a custom domain (docs/white-label.md) sends emails
		// from that host, so that is what its users type.
		await stubPublicConfig(page);
		await page.goto('/find-workspace');
		const field = page.getByLabel('Workspace address');
		await field.fill('ap.acmecorp.com');
		await page.getByRole('button', { name: 'Continue' }).click();

		await expect(page.getByRole('link', { name: 'Sign in at ap.acmecorp.com' })).toHaveAttribute(
			'href',
			'https://ap.acmecorp.com/login'
		);
		await expect(field).not.toHaveAttribute('aria-invalid', 'true');
		await expect(page).toHaveURL(/\/find-workspace$/);
	});

	for (const scheme of ['light', 'dark'] as const) {
		test(`find-your-workspace has no axe violations (${scheme})`, async ({ page }) => {
			await page.emulateMedia({ colorScheme: scheme });
			await stubPublicConfig(page);
			await page.goto('/find-workspace');
			await expect(page.locator('#workspace-suffix')).toHaveText('.feoh.example');
			await expectNoA11yViolations(page);
		});
	}

	test('with no config there is no guess: an error with a way to retry', async ({ page }) => {
		await stubPublicConfig(page, 503);
		await page.goto('/find-workspace');
		await expect(page.getByRole('alert')).toContainText("We couldn't load the sign-in details");
		await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
		await expectNoA11yViolations(page);

		await page.unroute((url) => url.pathname === '/api/public-config');
		await stubPublicConfig(page);
		await page.getByRole('button', { name: 'Try again' }).click();
		await expect(page.locator('#workspace-suffix')).toHaveText('.feoh.example');
		await expect(page.getByRole('alert')).toBeEmpty();
	});

	test('/login on the main domain opens find-your-workspace, not the marketing page', async ({ page }) => {
		await stubPublicConfig(page);
		await page.goto('/login');
		await expect(page.getByRole('heading', { level: 1, name: 'Sign in to your workspace' })).toBeVisible();
		await expect(page).toHaveURL(/\/find-workspace$/);
		await expect(page.locator('.landing')).toHaveCount(0);
	});

	test('find-your-workspace fits a 320px screen', async ({ page }) => {
		await page.setViewportSize({ width: 320, height: 640 });
		await stubPublicConfig(page);
		await page.goto('/find-workspace');
		await expect(page.locator('#workspace-suffix')).toHaveText('.feoh.example');
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		);
		expect(overflow).toBeLessThanOrEqual(0);
		const row = (await page.locator('.address-row').boundingBox())!;
		expect(row.x + row.width).toBeLessThanOrEqual(320);
	});
});

test.describe('landing — phone header (#635 finding 25)', () => {
	for (const width of [320, 375]) {
		test(`at ${width}px the sections, Sign in, signup and the theme stay reachable`, async ({ page }) => {
			await page.setViewportSize({ width, height: 700 });
			await gotoLanding(page);
			await expectHeaderFits(page, width);

			const header = page.locator('header.nav');
			const bar = header.locator('.nav-end');
			// Signup stays in the bar; so does Sign in, except below 360px,
			// where it moves into the menu.
			await expect(bar.getByRole('link', { name: 'Start free' })).toBeVisible();
			const signInInBar = width >= 360;
			await expect(bar.getByRole('link', { name: 'Sign in' })).toHaveCount(signInInBar ? 1 : 0);

			// The section links and the theme switch are behind a disclosure.
			const toggle = header.getByRole('button', { name: 'Menu' });
			await expect(toggle).toHaveAttribute('aria-expanded', 'false');
			await expect(header.getByRole('link', { name: 'Pricing' })).toHaveCount(0);
			await toggle.click();
			await expect(toggle).toHaveAttribute('aria-expanded', 'true');
			const menu = page.locator('#landing-menu');
			await expect(menu.getByRole('navigation', { name: 'Main' })).toBeVisible();
			await expect(menu.getByRole('radiogroup').or(menu.getByRole('group'))).toBeVisible();
			const pricing = menu.getByRole('link', { name: 'Pricing' });
			await expect(pricing).toBeVisible();
			// Exactly one Sign in, wherever it is.
			await expect(header.getByRole('link', { name: 'Sign in' })).toHaveCount(1);
			await expect(menu.getByRole('link', { name: 'Sign in' })).toHaveCount(signInInBar ? 0 : 1);
			// Every row of the panel is inside the viewport too.
			const panel = (await menu.boundingBox())!;
			expect(panel.x + panel.width).toBeLessThanOrEqual(width);

			// The panel follows the toggle in the focus order.
			await toggle.focus();
			await page.keyboard.press('Tab');
			await expect(menu.getByRole('link', { name: 'Features' })).toBeFocused();

			// Escape closes it and puts focus back on the toggle.
			await page.keyboard.press('Escape');
			await expect(toggle).toHaveAttribute('aria-expanded', 'false');
			await expect(toggle).toBeFocused();

			// A section link closes the menu on its way to the section.
			await toggle.click();
			await pricing.click();
			await expect(page).toHaveURL(/#pricing$/);
			await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		});
	}

	test('the open menu never covers what focus moves to (WCAG 2.4.11)', async ({ page }) => {
		// The panel belongs to the sticky header, so left open it would sit
		// over whatever Tab reaches next on the page.
		await page.setViewportSize({ width: 320, height: 640 });
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await gotoLanding(page);
		const toggle = page.getByRole('button', { name: 'Menu' });
		await toggle.click();
		const menu = page.locator('#landing-menu');
		// Tab through the panel and out of the header.
		const theme = menu.getByRole('radio', { name: 'System' });
		await theme.focus();
		await page.keyboard.press('Tab');
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		const focused = page.locator(':focus');
		const header = (await page.locator('header.nav').boundingBox())!;
		const box = (await focused.boundingBox())!;
		expect(box.y, 'focus is below the header, not under it').toBeGreaterThanOrEqual(header.y + header.height);

		// A tap outside closes it too.
		await toggle.click();
		await expect(toggle).toHaveAttribute('aria-expanded', 'true');
		await page.mouse.click(160, 600);
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
	});

	test('Escape from the page closes the menu without pulling focus up to it', async ({ page }) => {
		await page.setViewportSize({ width: 375, height: 700 });
		await gotoLanding(page);
		const toggle = page.getByRole('button', { name: 'Menu' });
		await toggle.click();
		const cta = page.locator('.hero').getByRole('link', { name: 'Create workspace' });
		await cta.evaluate((el) => (el as HTMLElement).focus());
		await page.keyboard.press('Escape');
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await expect(cta).toBeFocused();
	});

	for (const scheme of ['light', 'dark'] as const) {
		test(`the open menu has no axe violations (${scheme})`, async ({ page }) => {
			await page.setViewportSize({ width: 375, height: 700 });
			await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
			await gotoLanding(page);
			await page.getByRole('button', { name: 'Menu' }).click();
			await expect(page.locator('#landing-menu')).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}

	test('the wide header stays on one row from 1025px, in every shipped language', async ({ page }) => {
		// Above the menu breakpoint every control is in the bar; a second row
		// would be a sticky header taller than `scroll-padding-top` allows for.
		for (const width of [1025, 1280]) {
			await page.setViewportSize({ width, height: 700 });
			for (const locale of ['en', 'de', 'fr', 'es', 'pt-BR', 'ja']) {
				await page.addInitScript((loc) => localStorage.setItem('feoh_locale', loc), locale);
				await page.goto('/');
				await expect(page.locator('html')).toHaveAttribute('lang', locale);
				await expect(page.locator('header.nav .nav-wide')).toBeVisible();
				await expectHeaderFits(page, width);
			}
		}
	});

	for (const width of [320, 360, 761, 900, 1024]) {
		test(`the header and the hero fit at ${width}px in every shipped language`, async ({ page }) => {
			// The longer translations are what break a layout that fits in
			// English: German compounds, Spanish and French labels.
			await page.setViewportSize({ width, height: 640 });
			for (const locale of ['en', 'de', 'fr', 'es', 'pt-BR', 'ja']) {
				await page.addInitScript((loc) => localStorage.setItem('feoh_locale', loc), locale);
				await page.goto('/');
				await expect(page.locator('html')).toHaveAttribute('lang', locale);
				await expect(page.locator('header.nav a.nav-cta')).toBeVisible();
				await expectHeaderFits(page, width);
				// No line of the hero's text runs past its box (which `.landing`
				// would clip rather than scroll).
				const overflowing = await page
					.locator('.hero h1, .hero p, .hero a, .hero .eyebrow')
					.evaluateAll((els) =>
						els
							.filter((el) => el.scrollWidth > el.clientWidth + 1 || el.getBoundingClientRect().right > window.innerWidth)
							.map((el) => (el.textContent ?? '').trim().slice(0, 40))
					);
				expect(overflowing, `${locale}: hero text past the edge`).toEqual([]);
			}
		});
	}
});

test.describe('landing — in the visitor’s language (#635 finding 10)', () => {
	test('a German visitor gets the landing page in German, under lang="de"', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
		await page.goto('/');
		// German first (auto-waiting, the real signal that the slice landed),
		// then the English absent — the reverse passes against the empty
		// pre-hydration document.
		await expect(page.getByRole('heading', { level: 1 })).toContainText(
			'AP-Automatisierung, die Ihr Finanzteam'
		);
		await expect(page.locator('html')).toHaveAttribute('lang', 'de');
		await expect(page.getByRole('heading', { level: 2, name: 'Von der Rechnung ins Hauptbuch, in drei Schritten.' })).toBeAttached();
		await expect(page.locator('header.nav').getByRole('link', { name: 'Anmelden' })).toBeVisible();
		await expect(page.getByRole('navigation', { name: 'Hauptnavigation' })).toBeVisible();
		await expect(page.getByText('Built-in integrations')).toHaveCount(0);
		await expect(page.getByText('will actually use.')).toHaveCount(0);
		// The worked rebate example is in German number format.
		await expect(page.getByText(/500\.000\s\$/)).toBeAttached();
	});
});
