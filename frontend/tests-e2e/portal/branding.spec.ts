import {
	API_BASE,
	acceptConsent,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * Supplier-portal white-label theming.
 *
 * The portal (`/portal/*`) is a SEPARATE surface (VendorUser auth, its own
 * `portal_auth_token` key). It must carry the SAME per-tenant brand the main app
 * does — accent colors + logo + product name + <title> — including on the
 * UNAUTHENTICATED login page. The brand source is the public-by-design
 * `GET /api/portal/branding` (resolved by the tenant header/Host, returns only
 * the non-sensitive BrandConfig fields), applied by `portalBrand` in the portal
 * layout.
 *
 * This spec sets a known brand via the admin `PUT /api/organization/branding`
 * (the employee write path) and then asserts the portal applies it. The portal
 * uses its own token, so an employee session on the page is irrelevant to the
 * portal's auth — we drive the portal login (anon to the portal) directly.
 *
 * Cleanup restores the brand to empty in `afterAll` so the worker's tenant is
 * left as found and other specs aren't tinted.
 */

const ACCENT = '#a1b2c3';
const ACCENT_STRONG = '#102030';
const PRODUCT_NAME = 'E2E Portal Brand';
const LOGO_URL = 'https://cdn.e2e.test/portal-logo.png';

const EMPTY_BRAND = {
	product_name: '',
	logo_url: '',
	accent_color: '',
	accent_strong_color: '',
	support_url: '',
	legal_url: ''
};

async function putBranding(
	page: import('@playwright/test').Page,
	brand: Record<string, string>
): Promise<void> {
	const resp = await page.request.put(`${API_BASE}/api/organization/branding`, {
		headers: await authedTenantHeaders(page),
		data: brand
	});
	expect(resp.ok()).toBeTruthy();
}

test.describe('supplier-portal white-label theming', () => {
	test.beforeEach(async ({ page }) => {
		// Sign in as the worker's tenant admin so we hold a token to write the
		// brand. (The portal reads it back over the PUBLIC endpoint regardless.)
		await signInAndWait(page);
		await putBranding(page, {
			product_name: PRODUCT_NAME,
			logo_url: LOGO_URL,
			accent_color: ACCENT,
			accent_strong_color: ACCENT_STRONG,
			support_url: '',
			legal_url: ''
		});
	});

	test.afterEach(async ({ page }) => {
		// Best-effort restore. The page is already authed from beforeEach.
		await putBranding(page, EMPTY_BRAND).catch(() => {});
	});

	test('portal login applies the tenant accent + logo + product name', async ({ page }) => {
		// Portal login is anon to the PORTAL (its own token key), so it must theme
		// purely from the public brand read.
		await page.goto('/portal/login');

		// The accent CSS custom property is written onto <html> by portalBrand.
		await expect
			.poll(async () =>
				page.evaluate(() =>
					// #a1b2c3 reads as text on the dark palette unchanged (§477).
					document.documentElement.style.getPropertyValue('--brand-accent-dark').trim()
				)
			)
			.toBe(ACCENT);
		expect(
			await page.evaluate(() =>
				document.documentElement.style.getPropertyValue('--accent-strong').trim()
			)
		).toBe(ACCENT_STRONG);

		// The product name is the login card heading + drives <title>.
		await expect(page.getByRole('heading', { name: PRODUCT_NAME })).toBeVisible();
		await expect(page).toHaveTitle(new RegExp(PRODUCT_NAME));

		// The tenant logo renders.
		await expect(page.locator(`img[src="${LOGO_URL}"]`).first()).toBeVisible();
	});
});

/**
 * Support / legal footer (persona-supplier audit finding, issue #328).
 *
 * The portal already fetched `support_url`/`legal_url` off `GET
 * /api/portal/branding` (via `portalBrand`) but never rendered either,
 * leaving a stuck vendor with no "who do I contact" affordance anywhere in
 * the authed shell. The footer in `routes/portal/+layout.svelte` renders
 * Support/Legal links when the branding carries them, and fails soft
 * (renders nothing) when it doesn't — matching how the rest of
 * white-labeling degrades.
 */

const SUPPORT_URL = 'https://support.e2e.test/help';
const LEGAL_URL = 'https://legal.e2e.test/terms';

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

async function portalSignIn(page: import('@playwright/test').Page): Promise<void> {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	// Sign-in lands on the portal HOME. The footer under test is rendered by the
	// portal LAYOUT, so it is on screen there just as it is on any child page.
	await page.waitForURL(/\/portal\/?$/);
}

test.describe('supplier-portal support/legal footer', () => {
	// Opt out of the worker-admin storage state: these tests drive the portal
	// login UI directly with a separate VendorUser identity/token.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.afterEach(async ({ page }) => {
		// Restore via the employee admin path (needs its own sign-in — the
		// portal test above left no employee session on this fresh context).
		await signInAndWait(page);
		await putBranding(page, EMPTY_BRAND).catch(() => {});
	});

	test('renders Support + Legal links when the tenant configures them', async ({ page }) => {
		await signInAndWait(page);
		await putBranding(page, { ...EMPTY_BRAND, support_url: SUPPORT_URL, legal_url: LEGAL_URL });

		await portalSignIn(page);

		const support = page.getByRole('link', { name: 'Support' });
		const legal = page.getByRole('link', { name: 'Legal' });
		await expect(support).toBeVisible();
		await expect(support).toHaveAttribute('href', SUPPORT_URL);
		await expect(legal).toBeVisible();
		await expect(legal).toHaveAttribute('href', LEGAL_URL);
	});

	test('renders no footer when the tenant configures neither URL (fail-soft)', async ({
		page
	}) => {
		await signInAndWait(page);
		await putBranding(page, EMPTY_BRAND);

		await portalSignIn(page);

		await expect(page.getByRole('link', { name: 'Support' })).toHaveCount(0);
		await expect(page.getByRole('link', { name: 'Legal' })).toHaveCount(0);
	});
});
