import { expect, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * `/find-workspace` on a workspace host, signed in (the default storage
 * state). The page redirects to `/` on mount — a workspace host already is a
 * workspace — but for the beat before that the root layout used to classify
 * it as an app route and wrap its `AuthShell` in the app shell, so the
 * document briefly held two `<main>` landmarks (the shell's and the
 * AuthShell's). The layout now renders it standalone on every host.
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
