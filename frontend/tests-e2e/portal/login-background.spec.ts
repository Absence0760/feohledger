import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * Issue #422 — the supplier-portal login was the one pre-auth surface #429
 * left on a flat `background: var(--bg)` fill.
 *
 * The fix is a decorative backdrop local to `routes/portal/login/+page.svelte`
 * (`.backdrop` / `.blob-a` / `.blob-b`) rather than the employee `AuthShell` +
 * `Atmosphere` pair: that pair hardcodes literal platform rgba() colours and a
 * "FeohLedger" mark/tagline, which is exactly the platform branding a
 * white-labeled portal must not show (`docs/white-label.md` § Supplier-portal
 * theming). The portal's blobs use `--accent-glow` / `--accent-wash`
 * (`color-mix()` off `--accent` in `app.css`), so they must re-theme with the
 * tenant's own accent instead.
 *
 * Three things this spec pins:
 *  1. the backdrop is decorative and does not disturb tab order or field
 *     position (the issue's "must not shift the form" constraint);
 *  2. it derives its colour from the tenant's `--accent`, not a hardcoded
 *     indigo — the whole point of the fix;
 *  3. it is held still — no animation for any visitor, matching the `still`
 *     that `AuthShell` passes `Atmosphere` on every employee pre-auth page, so
 *     the page owes no WCAG 2.2.2 pause control.
 *
 * Draggable-logo coverage (#435) already lives in the static vitest guard
 * (`src/lib/a11y/imageDragging.test.ts`) and the tenant-brand-applies coverage
 * already lives in `portal/branding.spec.ts`; this spec is scoped to the new
 * backdrop only, not a re-test of either.
 */

const ACCENT = '#2fae5c';

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

async function blobBackgroundImage(page: import('@playwright/test').Page): Promise<string> {
	return page.locator('.blob-a').evaluate((el) => getComputedStyle(el).backgroundImage);
}

test.describe('/portal/login — decorative backdrop (#422)', () => {
	// Anonymous to the portal — login is unauthenticated by design.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('sits behind the card without moving focus, tab order, or the fields', async ({
		page
	}) => {
		await page.goto('/portal/login');

		const backdrop = page.locator('.backdrop');
		await expect(backdrop).toBeAttached();
		await expect(backdrop).toHaveAttribute('aria-hidden', 'true');
		// Decorative only: nothing inside it is focusable.
		await expect(backdrop.locator('button, a, input, [tabindex]')).toHaveCount(0);

		// Tab order still starts at the email field and proceeds in document
		// order — the backdrop is absolutely positioned out of flow, so it must
		// not have inserted itself (or shifted anything) into that order.
		const email = page.locator('input[type="email"]');
		const password = page.locator('input[type="password"]');
		await expect(email).toBeVisible();
		await email.focus();
		await expect(email).toBeFocused();
		await page.keyboard.press('Tab');
		await expect(password).toBeFocused();
	});

	test('derives its colour from the tenant accent, not a hardcoded indigo', async ({ page }) => {
		await page.goto('/portal/login');
		const defaultBackground = await blobBackgroundImage(page);

		await signInAndWait(page);
		await putBranding(page, { ...EMPTY_BRAND, accent_color: ACCENT });
		try {
			await page.goto('/portal/login');
			// The brand store writes --accent onto <html> asynchronously after
			// the public branding read resolves.
			await expect
				.poll(async () =>
					page.evaluate(() => document.documentElement.style.getPropertyValue('--accent').trim())
				)
				.toBe(ACCENT);

			const brandedBackground = await blobBackgroundImage(page);
			expect(brandedBackground, 'backdrop must re-theme with the tenant accent').not.toBe(
				defaultBackground
			);
		} finally {
			await putBranding(page, EMPTY_BRAND).catch(() => {});
		}
	});

	test('is held still, so it owes no WCAG 2.2.2 pause control', async ({ page }) => {
		// Not a reduced-motion test: the backdrop carries no animation for ANY
		// visitor, which is the stronger property. `AuthShell` passes `still` to
		// `Atmosphere` on every employee pre-auth page for this reason —
		// continuous ambient motion running past five seconds owes a pause
		// control (WCAG 2.2.2), and a pause button on a sign-in form is clutter.
		// Asserting it under `reducedMotion: 'reduce'` alone would pass
		// vacuously via app.css's global collapse and would not catch a drift
		// animation being reintroduced for everyone else.
		await page.goto('/portal/login');

		const names = await page
			.locator('.blob-a, .blob-b')
			.evaluateAll((els) => els.map((el) => getComputedStyle(el).animationName));
		expect(names.length).toBeGreaterThan(0);
		for (const name of names) {
			expect(name, 'the portal sign-in backdrop must not animate').toBe('none');
		}
	});
});
