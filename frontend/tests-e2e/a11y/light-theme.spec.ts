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

	test('the marketing landing stays on its dark palette under a light OS', async ({ page }) => {
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		const landingBg = await page
			.locator('.landing')
			.evaluate((el) => getComputedStyle(el).backgroundColor);
		expect(landingBg).toBe(DARK_BG);
		await expectNoA11yViolations(page);
	});
});
