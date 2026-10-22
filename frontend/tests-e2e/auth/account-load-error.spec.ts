import { expectNoA11yViolations } from '../a11y/axe-helper';
import { expect, test } from '../fixtures/helpers';

/**
 * The root layout draws the app shell only once `GET /api/auth/me` has
 * answered, because the shell needs the account (its roles drive the nav).
 * Before `layout/AccountLoadState.svelte`, a `/me` that failed for any reason
 * other than an ended session — a 5xx, a timeout, the network — left the
 * store signed in with no account, and the layout rendered nothing at all: a
 * blank page with no way forward (issue #635, finding 5).
 *
 * Runs on the worker's signed-in admin storage state; only `/me` is stubbed,
 * on its exact pathname (tests-e2e/README.md § Stubbing an API route).
 */

const isMe = (url: URL) => url.pathname === '/api/auth/me';

test.describe('account load failure', () => {
	test('a failing /me shows Retry and Log out, and Retry recovers the app', async ({ page }) => {
		let failing = true;
		let meCalls = 0;
		await page.route(isMe, async (route) => {
			meCalls += 1;
			if (failing) {
				await route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
				return;
			}
			await route.fallback();
		});

		await page.goto('/invoices');

		const heading = page.getByRole('heading', { name: "We couldn't load your account" });
		await expect(heading).toBeVisible();
		// The panel replaces the page, so focus is moved onto its heading and
		// a screen reader announces what went wrong.
		await expect(heading).toBeFocused();
		await expect(page.getByRole('button', { name: 'Log Out' })).toBeVisible();
		// Still on the page the user asked for — not bounced to /login.
		await expect(page).toHaveURL(/\/invoices$/);

		// The failure does not loop: the panel waits for the user, so the
		// next /me is exactly the one Retry sends. (The page fixture lands on
		// the tenant root before the spec's own goto, so the count before the
		// panel is not a fixed number.)
		const beforeRetry = meCalls;
		failing = false;
		await page.getByRole('button', { name: 'Try again' }).click();

		// The app shell renders on the same page once /me answers.
		await expect(page.locator('#main-content')).toBeVisible();
		expect(meCalls).toBe(beforeRetry + 1);
		await expect(heading).toHaveCount(0);
		await expect(page).toHaveURL(/\/invoices$/);
	});

	test('a Retry that fails again keeps the panel and returns focus to its heading', async ({
		page
	}) => {
		let release!: () => void;
		let hold: Promise<void> | null = null;
		await page.route(isMe, async (route) => {
			if (hold) await hold;
			await route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
		});
		await page.goto('/');
		const heading = page.getByRole('heading', { name: "We couldn't load your account" });
		await expect(heading).toBeFocused();

		hold = new Promise<void>((resolve) => (release = resolve));
		const retry = page.getByRole('button', { name: 'Try again' });
		await retry.focus();
		await page.keyboard.press('Enter');
		// In flight: the panel stays, the button says so and keeps focus
		// (aria-disabled, not disabled, which would drop focus to <body>).
		const retrying = page.getByRole('button', { name: 'Retrying…' });
		await expect(retrying).toBeFocused();
		await expect(retrying).toHaveAttribute('aria-disabled', 'true');
		await expect(page.getByRole('status')).toHaveText('Retrying…');

		release();
		await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
		await expect(heading).toBeFocused();
	});

	for (const colorScheme of ['dark', 'light'] as const) {
		test(`the failure panel has no axe violations (${colorScheme}) and reflows at 320px`, async ({
			page
		}) => {
			await page.emulateMedia({ colorScheme });
			await page.route(isMe, (route) =>
				route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } })
			);
			await page.goto('/');
			await expect(page.getByRole('heading', { name: "We couldn't load your account" })).toBeVisible();
			await expectNoA11yViolations(page);

			await page.setViewportSize({ width: 320, height: 640 });
			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow).toBeLessThanOrEqual(0);
		});
	}

	test('Log out from the failure panel signs the user out', async ({ page }) => {
		await page.route(isMe, (route) =>
			route.fulfill({ status: 503, json: { detail: 'Service Unavailable' } })
		);
		await page.route(
			(url) => url.pathname === '/api/auth/logout',
			(route) => route.fulfill({ status: 204, body: '' })
		);

		await page.goto('/');
		await page.getByRole('button', { name: 'Log Out' }).click();

		await expect(page).toHaveURL(/\/login$/);
		await expect(page.locator('input[type="password"]')).toBeVisible();
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();
	});

	test('a slow /me shows a loading state, never a blank page', async ({ page }) => {
		let release!: () => void;
		const held = new Promise<void>((resolve) => (release = resolve));
		await page.route(isMe, async (route) => {
			await held;
			await route.fallback();
		});

		await page.goto('/');
		await expect(page.getByRole('status').filter({ hasText: 'Loading your workspace…' })).toBeVisible();

		release();
		await expect(page.locator('#main-content')).toBeVisible();
		await expect(page.getByText('Loading your workspace…')).toHaveCount(0);
	});
});
