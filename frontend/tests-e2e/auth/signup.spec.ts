import type { PublicConfig } from '$lib/types/publicConfig';
import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';


// Start unauthenticated — this spec tests the public signup flow on the no-tenant origin.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * Self-service signup — anonymous-tenant route at /signup. Renders only
 * on the root domain (no tenant subdomain), so we override baseURL to
 * the no-tenant origin.
 */

test.describe('/signup (no tenant)', () => {
	test.use({ baseURL: NO_TENANT_BASE });

	test('renders the create-workspace form', async ({ page }) => {
		await page.goto('/signup');

		await expect(
			page.getByRole('heading', { name: 'Create your workspace' })
		).toBeVisible();
		await expect(page.getByPlaceholder('acme')).toBeVisible();
		await expect(
			page.getByRole('button', { name: 'Send verification email' })
		).toBeVisible();
	});

	test('slug-check rejects an already-taken slug', async ({ page }) => {
		await page.goto('/signup');

		// `acme` is seeded — slug-check should mark it unavailable.
		// The page shows a `.hint.bad` with the rejection reason; the
		// submit button also goes disabled when slugStatus === 'bad'.
		await page.getByPlaceholder('acme').fill('acme');

		await expect(page.locator('small.hint.bad')).toBeVisible({ timeout: 5_000 });
		await expect(
			page.getByRole('button', { name: 'Send verification email' })
		).toBeDisabled();
	});

	test('slug-check accepts a fresh slug', async ({ page }) => {
		await page.goto('/signup');

		// Time-suffixed slug ensures it's never been used (avoids
		// inter-run pollution if the DB persists between local dev runs).
		const fresh = `e2e${Date.now().toString().slice(-8)}`;
		await page.getByPlaceholder('acme').fill(fresh);

		// `.hint.ok` ("Available") appears once the debounced check
		// returns 200 from /api/signup/slug-check.
		await expect(page.locator('small.hint.ok')).toBeVisible({ timeout: 5_000 });
	});

	test('a deployment with signup closed says so instead of rendering the form', async ({
		page
	}) => {
		// `FEOH_SIGNUP_ENABLED=false` reaches the SPA only through
		// `/api/public-config`. Everything else in the real answer is kept, so
		// the one field under test is the only thing this stub changes.
		await page.route('**/api/public-config', async (route) => {
			const upstream = await route.fetch();
			const real = (await upstream.json()) as PublicConfig;
			await route.fulfill({
				response: upstream,
				json: { ...real, signup_enabled: false } satisfies PublicConfig
			});
		});

		await page.goto('/signup');

		await expect(page.getByRole('heading', { name: 'Signup is closed' })).toBeVisible();
		await expect(page.getByText(/created by invitation only/)).toBeVisible();
		// No form that every submit of would 404 — and no "what happens next"
		// panel describing a flow that is not on offer.
		await expect(page.getByRole('heading', { name: 'Create your workspace' })).toHaveCount(0);
		await expect(page.getByPlaceholder('acme')).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Send verification email' })).toHaveCount(0);
		await expect(page.getByRole('heading', { name: 'What happens next' })).toHaveCount(0);
	});
});
