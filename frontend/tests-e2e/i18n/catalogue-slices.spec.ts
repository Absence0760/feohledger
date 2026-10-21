import { expect, test } from '../fixtures/helpers';

/**
 * The route-scoped catalogue slices (decisions §300): the settings screens'
 * strings and the supplier portal's ship as their own lazy chunk per locale,
 * English included, and each route's `+layout.ts` awaits its slice before the
 * route renders. `src/lib/i18n/slices.test.ts` proves statically that no key of
 * a slice is rendered from a route that does not load it; these prove the load
 * itself works in a real browser — that the slice arrives in the reader's
 * language, and that a client-side navigation never paints the page before it
 * has (a raw key or an English heading would be the visible failure).
 *
 * Strings are read from `locales/settings/de.ts` and `locales/portal/ja.ts`.
 */

test.describe('route-scoped catalogue slices', () => {
	test('a stored locale reaches the settings slice on a cold load', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
		await page.goto('/organization');
		// `org.title` lives only in the settings slice.
		await expect(page.getByRole('heading', { level: 1, name: 'Organisation' })).toBeVisible();
	});

	test('navigating into a settings route paints its heading in the reader language, never raw or English', async ({
		page
	}) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
		await page.goto('/invoices');
		// The main catalogue is German — the app is hydrated and the locale applied.
		await expect(page.getByRole('heading', { level: 1, name: 'Rechnungen' })).toBeVisible();

		// Make the settings slice slow to arrive. On localhost its chunk lands
		// within the same frame as the route's own code, so without this a route
		// that rendered before its slice had loaded would pass by luck; with it,
		// only a load that really waits paints the German heading first. The
		// chunk names are content hashes, so the slice is recognised by a key
		// only it holds.
		await page.route(
			(url) => url.pathname.startsWith('/_app/immutable/chunks/') && url.pathname.endsWith('.js'),
			async (route) => {
				const response = await route.fetch();
				const body = await response.text();
				if (body.includes('"org.title"')) await new Promise((resolve) => setTimeout(resolve, 750));
				await route.fulfill({ response, body });
			}
		);

		// Record every <h1> text the page shows from here on, so a frame that
		// rendered before the slice landed is caught even if it is replaced
		// before an assertion could look.
		await page.evaluate(() => {
			const seen: string[] = [];
			(window as unknown as { __h1s: string[] }).__h1s = seen;
			const record = () => document.querySelectorAll('h1').forEach((h) => seen.push(h.textContent?.trim() ?? ''));
			new MutationObserver(record).observe(document.body, { subtree: true, childList: true, characterData: true });
		});
		// A same-origin anchor click is a SvelteKit client-side navigation, so
		// the route's `load` (not a full page load) is what gates the render.
		await page.evaluate(() => {
			const a = document.createElement('a');
			a.href = '/organization';
			document.body.append(a);
			a.click();
		});
		await expect(page.getByRole('heading', { level: 1, name: 'Organisation' })).toBeVisible();

		const seen = await page.evaluate(() => (window as unknown as { __h1s: string[] }).__h1s);
		expect(seen, 'the settings heading never showed its raw key').not.toContain('org.title');
		expect(seen, 'the settings heading never flashed in English').not.toContain('Organization');
	});

	test('the supplier portal loads its slice in the reader language', async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'ja'));
		await page.goto('/portal/login');
		// `portal.login.subtitle` and `portal.login.signIn` live only in the portal slice.
		await expect(page.getByText('サインインして請求書を送信し、支払いを確認します')).toBeVisible();
		await expect(page.getByRole('button', { name: 'サインイン', exact: true })).toBeVisible();
	});
});
