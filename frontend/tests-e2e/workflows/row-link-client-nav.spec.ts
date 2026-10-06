import { expect, test } from '../fixtures/helpers';

/**
 * A `<RowLink href>` is a client-side navigation, not a page reload.
 *
 * Its anchor used to call `stopPropagation()` so the row's own click handler
 * would not also fire. SvelteKit's router catches link clicks by delegation
 * higher up the document, so that stopped the router too: every row link
 * (the Workflows list, the Adaptive anomaly rows) reloaded the whole app,
 * which threw away in-memory state and made Back a document navigation the
 * unsaved-changes guard can only hand to the browser's own prompt.
 *
 * A marker set on `window` survives a client-side navigation and is gone
 * after a document load, so it tells the two apart directly.
 */
test('opening a workflow from its row link keeps the same document', async ({ page }) => {
	await page.goto('/workflows');
	const link = page.locator('tbody tr a.row-link').first();
	await expect(link).toBeVisible();
	await page.evaluate(() => ((window as unknown as { __rowLinkMarker: number }).__rowLinkMarker = 1));

	await link.click();
	await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]+/);
	const marker = await page.evaluate(
		() => (window as unknown as { __rowLinkMarker?: number }).__rowLinkMarker
	);
	expect(marker, 'the row link reloaded the page instead of routing client-side').toBe(1);
});
