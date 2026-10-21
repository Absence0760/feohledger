import type { Page } from '@playwright/test';
import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * The light theme (docs/decisions.md §289).
 *
 * Three things a unit test cannot see:
 *
 *  1. The choice made on `/profile` survives a reload, and is on `<html>`
 *     BEFORE the first frame — `src/app.html`'s inline script, not the bundle,
 *     is what puts it there. If it ever moved into a component, a Light user
 *     would get a dark first paint and this is what would say so.
 *  2. The CSS actually switches: System follows the emulated OS through the
 *     media query, an explicit choice overrides it, and `color-scheme` follows
 *     so native controls and scrollbars match.
 *  3. axe, in light mode, on a representative slice of every surface that
 *     follows the theme — the signed-in app, the supplier portal, the auth
 *     shell, the help centre and the legal pages. `tokenPairing.test.ts`
 *     checks every stylesheet's pairs per theme; this checks what the cascade
 *     actually composes, which no static scan can (see `axe.spec.ts`).
 *
 * The OS is emulated DARK in every test that stores Light, so a pass cannot
 * come from System happening to resolve light.
 */

/** Values from `src/app.css`, as `getComputedStyle` reports them. */
const LIGHT_BG = 'rgb(244, 245, 248)';
const DARK_BG = 'rgb(15, 17, 23)';

/** Store a theme choice before any page script runs, as a returning visitor would have. */
async function storeTheme(page: Page, choice: 'light' | 'dark'): Promise<void> {
	await page.addInitScript((value) => {
		try {
			localStorage.setItem('feoh_theme', value);
		} catch {
			// about:blank — ignore
		}
	}, choice);
}

/**
 * Record `data-theme` at the first animation frame — i.e. what the first paint
 * was drawn with. An init script runs before the document's own scripts, so the
 * callback is queued ahead of anything the app bundle could do.
 */
async function recordFirstFrameTheme(page: Page): Promise<void> {
	await page.addInitScript(() => {
		requestAnimationFrame(() => {
			(window as unknown as { __firstFrameTheme: string | null }).__firstFrameTheme =
				document.documentElement.getAttribute('data-theme');
		});
	});
}

async function rootStyle(page: Page) {
	return page.evaluate(() => {
		const style = getComputedStyle(document.body);
		return {
			theme: document.documentElement.getAttribute('data-theme'),
			bg: style.backgroundColor,
			scheme: getComputedStyle(document.documentElement).colorScheme
		};
	});
}

test.describe('light theme — choosing, persisting, first paint', () => {
	test('Light chosen on /profile applies at once, persists, and is on <html> before the first frame', async ({
		page
	}) => {
		await page.emulateMedia({ colorScheme: 'dark' });
		await page.goto('/profile?section=appearance');
		await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();

		const group = page.getByRole('group', { name: 'Theme' });
		// A fresh browser follows the OS.
		await expect(group.getByRole('radio', { name: 'System' })).toBeChecked();
		await expect.poll(async () => (await rootStyle(page)).bg).toBe(DARK_BG);

		await group.getByRole('radio', { name: 'Light' }).check();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await expect.poll(async () => (await rootStyle(page)).bg).toBe(LIGHT_BG);
		expect((await rootStyle(page)).scheme).toBe('light');

		await recordFirstFrameTheme(page);
		await page.reload();
		await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();
		expect(
			await page.evaluate(() => (window as unknown as { __firstFrameTheme: string }).__firstFrameTheme)
		).toBe('light');
		await expect(group.getByRole('radio', { name: 'Light' })).toBeChecked();
		expect(await rootStyle(page)).toEqual({ theme: 'light', bg: LIGHT_BG, scheme: 'light' });

		// Back to System: the OS (dark) wins again, and nothing is left stored.
		await group.getByRole('radio', { name: 'System' }).check();
		await expect.poll(async () => (await rootStyle(page)).bg).toBe(DARK_BG);
		expect(await page.evaluate(() => localStorage.getItem('feoh_theme'))).toBeNull();
	});

	test('the theme picker is a keyboard radio group', async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'dark' });
		await page.goto('/profile?section=appearance');
		const group = page.getByRole('group', { name: 'Theme' });
		await group.getByRole('radio', { name: 'System' }).focus();
		await page.keyboard.press('ArrowRight');
		await expect(group.getByRole('radio', { name: 'Light' })).toBeChecked();
		await expect(group.getByRole('radio', { name: 'Light' })).toBeFocused();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await page.keyboard.press('ArrowRight');
		await expect(group.getByRole('radio', { name: 'Dark' })).toBeChecked();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
	});

	test('System follows the OS in both directions, through CSS alone', async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'light' });
		await page.goto('/profile?section=appearance');
		await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();
		expect(await rootStyle(page)).toEqual({ theme: 'system', bg: LIGHT_BG, scheme: 'light' });

		// The OS flips while the page is open: no reload, no script involved.
		await page.emulateMedia({ colorScheme: 'dark' });
		await expect.poll(async () => (await rootStyle(page)).bg).toBe(DARK_BG);
		expect((await rootStyle(page)).scheme).toBe('dark');
	});

	test('the shared theme cookie wins over this origin’s stored choice, before the first frame', async ({
		page
	}) => {
		// The cookie is how a choice made on another workspace — or on the
		// apex signup page — arrives (docs/decisions.md §290); this origin's
		// localStorage may still hold an older one. Set as a host cookie here:
		// what reaches this page is the same `feoh_theme=…` pair either way,
		// and the Domain derivation is unit-tested (`theme.test.ts`).
		await page.emulateMedia({ colorScheme: 'dark' });
		await storeTheme(page, 'dark');
		await page.addInitScript(() => {
			document.cookie = 'feoh_theme=light; path=/';
		});
		await recordFirstFrameTheme(page);
		await page.goto('/profile?section=appearance');
		await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();
		expect(
			await page.evaluate(() => (window as unknown as { __firstFrameTheme: string }).__firstFrameTheme)
		).toBe('light');
		await expect(page.getByRole('group', { name: 'Theme' }).getByRole('radio', { name: 'Light' })).toBeChecked();
		expect(await rootStyle(page)).toEqual({ theme: 'light', bg: LIGHT_BG, scheme: 'light' });
	});

	test('on localhost the choice stays per origin: no cookie, localStorage only', async ({
		page,
		context
	}) => {
		// Browsers refuse `Domain=localhost`, so there is no platform domain to
		// share across here; the store must not write a cookie that would only
		// pretend to be shared.
		await page.emulateMedia({ colorScheme: 'dark' });
		await page.goto('/profile?section=appearance');
		await page.getByRole('group', { name: 'Theme' }).getByRole('radio', { name: 'Light' }).check();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		expect(await page.evaluate(() => localStorage.getItem('feoh_theme'))).toBe('light');
		expect((await context.cookies()).filter((c) => c.name === 'feoh_theme')).toEqual([]);
	});

	test('an explicit Dark overrides a light OS', async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'light' });
		await storeTheme(page, 'dark');
		await page.goto('/profile?section=appearance');
		await expect(page.getByRole('heading', { name: 'Appearance', exact: true })).toBeVisible();
		expect(await rootStyle(page)).toEqual({ theme: 'dark', bg: DARK_BG, scheme: 'dark' });
	});
});

/**
 * axe in light mode. The routes are a cross-section rather than the whole of
 * `axe.spec.ts`'s list: each one is here for a surface the others do not
 * cover — the dense list shell, status badges, KPI tiles, the exceptions
 * queue's tone map, the settings rail, the help centre's prose and diagrams.
 */
const LIGHT_AUTHED_ROUTES: { path: string; name: string; ready: string }[] = [
	{ path: '/', name: 'dashboard', ready: 'Dashboard' },
	{ path: '/invoices', name: 'invoices list', ready: 'Invoices' },
	{ path: '/vendors', name: 'vendors list', ready: 'Vendors' },
	{ path: '/payments', name: 'payments', ready: 'Payments' },
	{ path: '/exceptions', name: 'exceptions queue', ready: 'Exceptions' },
	{ path: '/admin/api-keys', name: 'admin API keys', ready: 'API Keys' },
	{ path: '/billing', name: 'billing', ready: 'Billing' },
	{ path: '/profile?section=appearance', name: 'profile appearance', ready: 'Appearance' },
	{ path: '/help', name: 'help centre', ready: 'Follow an invoice' },
	{ path: '/help/guides/run-payments', name: 'help guide with a diagram', ready: 'Run a payment run' }
];

test.describe('light theme — axe (WCAG 2.2 AA), signed in', () => {
	test.beforeEach(async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'dark' });
		await storeTheme(page, 'light');
	});

	for (const { path, name, ready } of LIGHT_AUTHED_ROUTES) {
		test(`${name} (${path}) has no axe violations in light mode`, async ({ page }) => {
			await page.goto(path);
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			await expect(page.getByRole('heading', { name: ready, exact: true }).first()).toBeVisible();
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
			await expectNoA11yViolations(page);
		});
	}
});

/**
 * The surfaces with no signed-in user reach light through System here — the
 * media-query path, rather than the attribute path the block above takes.
 */
test.describe('light theme — axe (WCAG 2.2 AA), signed out', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
	});

	test('AP login (/login) has no axe violations in light mode', async ({ page }) => {
		await page.goto('/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		expect((await rootStyle(page)).bg).toBe(LIGHT_BG);
		await expectNoA11yViolations(page);
	});

	test('supplier portal login (/portal/login) has no axe violations in light mode', async ({ page }) => {
		await page.goto('/portal/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		expect((await rootStyle(page)).bg).toBe(LIGHT_BG);
		await expectNoA11yViolations(page);
	});

	test('the auth shell carries a theme picker: it applies at once, persists, and is a keyboard radio group', async ({
		page
	}) => {
		// Signed out there is no /profile to reach, and signup is where a new
		// workspace's first theme is chosen (docs/decisions.md §290).
		await page.goto('/login');
		const group = page.getByRole('group', { name: 'Theme' });
		await expect(group.getByRole('radio', { name: 'System' })).toBeChecked();
		await group.getByRole('radio', { name: 'System' }).focus();
		await page.keyboard.press('ArrowRight');
		await page.keyboard.press('ArrowRight');
		await expect(group.getByRole('radio', { name: 'Dark' })).toBeChecked();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
		await expect.poll(async () => (await rootStyle(page)).bg).toBe(DARK_BG);

		await page.reload();
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(group.getByRole('radio', { name: 'Dark' })).toBeChecked();
		expect((await rootStyle(page)).bg).toBe(DARK_BG);
		await expectNoA11yViolations(page);
	});

	test('the auth shell panel shows the tally render lit for the theme in use', async ({ page }) => {
		// The same two renders as the landing page (lib/marketing/ornament.ts).
		await page.goto('/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		const art = page.locator('.panel-art');
		await expect(art).toHaveAttribute('src', /tally-split-light\.webp$/);
		await page.getByRole('group', { name: 'Theme' }).getByRole('radio', { name: 'Dark' }).check();
		await expect(art).toHaveAttribute('src', /tally-split\.webp$/);
	});

	for (const path of ['/legal', '/legal/privacy']) {
		test(`${path} has no axe violations in light mode`, async ({ page }) => {
			await page.goto(path);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			expect((await rootStyle(page)).bg).toBe(LIGHT_BG);
			await expectNoA11yViolations(page);
		});
	}
});

test.describe('light theme — the public no-tenant origin', () => {
	test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

	test.beforeEach(async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
		await page.addInitScript(() => {
			try {
				localStorage.setItem('feoh_consent_choice', 'accepted');
			} catch {
				// about:blank — ignore
			}
		});
	});

	test('signup (/signup) follows the theme and has no axe violations', async ({ page }) => {
		await page.goto('/signup');
		await expect(page.getByRole('heading', { name: 'Create your workspace' })).toBeVisible();
		expect((await rootStyle(page)).bg).toBe(LIGHT_BG);
		await expectNoA11yViolations(page);
	});

	/*
	 * The marketing landing page follows the theme (docs/decisions.md §312,
	 * superseding §289's dark pin), including its one raster: the tally
	 * ornament has a render lit for each ground.
	 */
	const landingBg = (page: Page) =>
		page.locator('.landing').evaluate((el) => getComputedStyle(el).backgroundColor);
	const ornament = (page: Page) => page.locator('.tally-art img');

	test('the marketing landing follows a light OS and has no axe violations', async ({ page }) => {
		await recordFirstFrameTheme(page);
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(
			await page.evaluate(() => (window as unknown as { __firstFrameTheme: string }).__firstFrameTheme)
		).toBe('system');
		expect(await landingBg(page)).toBe(LIGHT_BG);
		expect((await rootStyle(page)).scheme).toBe('light');
		await expect(ornament(page)).toHaveAttribute('src', /tally-split-light\.webp$/);
		await expectNoA11yViolations(page);
	});

	test('a stored Light choice paints the landing light from the first frame under a dark OS', async ({
		page
	}) => {
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
		await storeTheme(page, 'light');
		await recordFirstFrameTheme(page);
		// The ornament's FIRST src, not its settled one: an <img> that mounted
		// with the dark file and was corrected after hydration would fetch the
		// wrong render and visibly swap. Recorded the moment the element appears.
		await page.addInitScript(() => {
			new MutationObserver((_, observer) => {
				const img = document.querySelector('.tally-art img');
				if (!img) return;
				(window as unknown as { __firstOrnamentSrc: string | null }).__firstOrnamentSrc =
					img.getAttribute('src');
				observer.disconnect();
			}).observe(document, { childList: true, subtree: true });
		});
		const ornamentRequests: string[] = [];
		page.on('request', (r) => {
			if (/\/marketing\/tally-split/.test(r.url())) ornamentRequests.push(r.url());
		});

		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(
			await page.evaluate(() => (window as unknown as { __firstFrameTheme: string }).__firstFrameTheme)
		).toBe('light');
		expect(await landingBg(page)).toBe(LIGHT_BG);
		expect(
			await page.evaluate(
				() => (window as unknown as { __firstOrnamentSrc: string }).__firstOrnamentSrc
			)
		).toMatch(/tally-split-light\.webp$/);

		// It is lazy, but Chromium's lazy margin may already have fetched it:
		// bring it into view and wait until the light file has been requested
		// (collected from the start, so an early fetch is not missed).
		await ornament(page).scrollIntoViewIfNeeded();
		await expect.poll(() => ornamentRequests.some((u) => /tally-split-light\.webp$/.test(u))).toBe(true);
		await expect(ornament(page)).toHaveJSProperty('complete', true);
		expect(ornamentRequests.filter((u) => /tally-split\.webp$/.test(u)), 'the dark render was fetched').toEqual(
			[]
		);
	});

	test('the marketing landing in dark (dark OS) has no axe violations', async ({ page }) => {
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(await landingBg(page)).toBe(DARK_BG);
		await expect(ornament(page)).toHaveAttribute('src', /tally-split\.webp$/);
		await expectNoA11yViolations(page);
	});

	test('at 320px the landing header holds the mark, the CTA and the theme switch without overflow', async ({
		page
	}) => {
		// 1.4.10 at the criterion's own width. Below 760px the theme switch
		// sits in the header's menu with the section links (#635 finding 25),
		// so it is measured with the menu open — the bar beside it holds the
		// mark, the CTA and the menu toggle.
		await page.setViewportSize({ width: 320, height: 800 });
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		const header = page.locator('header');
		await header.getByRole('button', { name: 'Menu' }).click();
		await expect(header.getByRole('group', { name: 'Theme' })).toBeVisible();
		await expect(header.getByRole('link', { name: 'Start free' })).toBeInViewport({ ratio: 1 });
		const last = header.locator('label', { has: page.getByRole('radio', { name: 'Dark' }) });
		const box = await last.boundingBox();
		expect(box!.x + box!.width, 'the last segment ends inside the viewport').toBeLessThanOrEqual(320);
		expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
		await expectNoA11yViolations(page);
	});

	test('the landing header carries a theme switch: a keyboard radio group that applies and persists', async ({
		page
	}) => {
		// An anonymous visitor has no /profile; this is where they choose.
		await page.goto('/');
		const group = page.locator('header').getByRole('group', { name: 'Theme' });
		const system = group.getByRole('radio', { name: 'System' });
		await expect(system).toBeChecked();

		// 2.5.8: the segment is the target, and it clears 24 × 24.
		for (const name of ['System', 'Light', 'Dark']) {
			const box = await group.locator('label', { has: page.getByRole('radio', { name }) }).boundingBox();
			expect(box!.width, `${name} segment width`).toBeGreaterThanOrEqual(24);
			expect(box!.height, `${name} segment height`).toBeGreaterThanOrEqual(24);
		}

		// Reached by Tab like any control, not only by a programmatic focus.
		await page.keyboard.press('Tab');
		for (let i = 0; i < 12 && !(await system.evaluate((el) => el === document.activeElement)); i++) {
			await page.keyboard.press('Tab');
		}
		await expect(system).toBeFocused();
		// The radio is clipped to 1px, so the segment must draw the ring (2.4.7).
		const segment = (name: string) => group.locator('label', { has: page.getByRole('radio', { name }) });
		expect(await segment('System').evaluate((el) => getComputedStyle(el).outlineStyle)).toBe('solid');
		// With the dot hidden, the checked segment needs an edge the others lack
		// (1.4.11 / 1.4.1), not just a fill.
		const edge = (name: string) => segment(name).evaluate((el) => getComputedStyle(el).borderTopColor);
		expect(await edge('System')).not.toBe(await edge('Light'));
		await page.keyboard.press('ArrowRight');
		await page.keyboard.press('ArrowRight');
		await expect(group.getByRole('radio', { name: 'Dark' })).toBeChecked();
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
		await expect.poll(() => landingBg(page)).toBe(DARK_BG);
		await expect(ornament(page)).toHaveAttribute('src', /tally-split\.webp$/);

		await page.reload();
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(group.getByRole('radio', { name: 'Dark' })).toBeChecked();
		expect(await landingBg(page)).toBe(DARK_BG);
		await expectNoA11yViolations(page);

		// By pointer: the radio is visually hidden, so the icon segment (its
		// <label>) is what a mouse user clicks.
		await group.locator('label', { has: page.getByRole('radio', { name: 'Light' }) }).click();
		await expect(group.getByRole('radio', { name: 'Light' })).toBeChecked();
		await expect.poll(() => landingBg(page)).toBe(LIGHT_BG);
		await expect(ornament(page)).toHaveAttribute('src', /tally-split-light\.webp$/);
	});
});
