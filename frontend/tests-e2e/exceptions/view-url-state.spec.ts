import { expect, test } from '../fixtures/helpers';

/**
 * The exceptions Queue/AI-Agents tab is URL-backed.
 *
 * The code claimed (in a comment) the view was persisted in the URL but never
 * synced it, so switching to AI Agents and reloading dropped you back to
 * Queue. The active view now lives in ?view=agents and survives a reload.
 */
test.describe('exceptions view URL state', () => {
	test('switching to AI Agents writes ?view and survives a reload', async ({ page }) => {
		await page.goto('/exceptions');
		// Default is Queue — no view param.
		await expect(page).not.toHaveURL(/view=agents/);

		const agentsTab = page.getByRole('tab', { name: /agent/i });
		await agentsTab.click();
		await expect(page).toHaveURL(/view=agents/);
		await expect(agentsTab).toHaveAttribute('aria-selected', 'true');

		// Reload — the URL keeps ?view=agents, and the AI-Agents tab stays active
		// (previously it reset to Queue).
		await page.reload();
		await expect(page).toHaveURL(/view=agents/);
		await expect(page.getByRole('tab', { name: /agent/i })).toHaveAttribute(
			'aria-selected',
			'true'
		);

		// Switching back to Queue clears the param.
		await page.getByRole('tab', { name: /queue/i }).click();
		await expect(page).not.toHaveURL(/view=agents/);
	});

	/**
	 * A navigation that reuses the mounted page re-reads the URL. The filters
	 * were read once at mount, so clicking the sidebar's Exceptions row while
	 * on `?status=resolved` (or `?view=agents`) changed the address bar to the
	 * bare route and nothing else, and Back/Forward between the two entries
	 * left the view and the URL disagreeing.
	 */
	test('a same-route navigation and back/forward re-apply status and view', async ({ page }) => {
		const isList = (r: import('@playwright/test').Response) =>
			new URL(r.url()).pathname === '/api/exceptions' && r.request().method() === 'GET';
		const statusRow = page.locator('[aria-pressed]', { hasText: /^Resolved/ });
		const openChip = page.locator('[aria-pressed]', { hasText: /^Open/ });

		const resolved = page.waitForResponse(
			(r) => isList(r) && new URL(r.url()).searchParams.get('status') === 'resolved'
		);
		await page.goto('/exceptions?status=resolved');
		await resolved;
		await expect(statusRow).toHaveAttribute('aria-pressed', 'true');

		// Sidebar row → bare /exceptions, whose default is the Open queue.
		const open = page.waitForResponse(
			(r) => isList(r) && new URL(r.url()).searchParams.get('status') === 'open'
		);
		await page.getByRole('link', { name: 'Exceptions', exact: true }).click();
		await open;
		await expect(page).toHaveURL(/\/exceptions$/);
		await expect(openChip).toHaveAttribute('aria-pressed', 'true');
		await expect(statusRow).toHaveAttribute('aria-pressed', 'false');

		const back = page.waitForResponse(
			(r) => isList(r) && new URL(r.url()).searchParams.get('status') === 'resolved'
		);
		await page.goBack();
		await back;
		await expect(statusRow).toHaveAttribute('aria-pressed', 'true');

		// The tab follows the URL the same way.
		await page.getByRole('tab', { name: /agent/i }).click();
		await expect(page).toHaveURL(/view=agents/);
		await page.getByRole('link', { name: 'Exceptions', exact: true }).click();
		await expect(page).toHaveURL(/\/exceptions$/);
		await expect(page.getByRole('tab', { name: /queue/i })).toHaveAttribute(
			'aria-selected',
			'true'
		);
	});
});
