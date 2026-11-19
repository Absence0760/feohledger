import { expect, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * `/find-workspace` on a workspace host, signed in (the default storage
 * state). The root layout sends it to `/` — a workspace host already is a
 * workspace. It used to classify the page as an app route and wrap its
 * `AuthShell` in the app shell for the beat before the redirect, so the
 * document briefly held two `<main>` landmarks (the shell's and the
 * AuthShell's). On a workspace host the layout now renders nothing for it.
 *
 * A late assertion would only see the settled dashboard, so a mutation
 * observer installed before any script runs records the most `<main>`
 * elements the document ever held.
 */
test('find-your-workspace never nests in the app shell on a workspace host', async ({ page }) => {
	await page.addInitScript(() => {
		const w = window as unknown as { __maxMains: number };
		w.__maxMains = 0;
		new MutationObserver(() => {
			w.__maxMains = Math.max(w.__maxMains, document.querySelectorAll('main').length);
		}).observe(document, { childList: true, subtree: true });
	});

	await page.goto('/find-workspace');
	await page.waitForURL(TENANT_ROOT_URL);
	// The app shell's own <main> is up: the redirect landed on the dashboard.
	await expect(page.locator('main#main-content')).toBeVisible();

	expect(await page.evaluate(() => (window as unknown as { __maxMains: number }).__maxMains)).toBe(1);
});
