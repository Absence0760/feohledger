import { expect, test } from '../fixtures/helpers';

/**
 * WCAG 2.2 SC 4.1.3 Status Messages — results that appear in response to an
 * action are announced, not just painted.
 *
 * The static half of this guard is `src/lib/a11y/liveRegionAudit.test.ts`, which
 * catches the `{#if msg}<div class="message">` idiom by its class names. The
 * Organization settings "Test connection" result does not match that idiom —
 * it is a `.test-result` span coloured green or red by a class directive — and
 * it was rendered by `{#if result}` with no live region, so a screen-reader user
 * pressed Test connection and heard nothing: neither the success nor the
 * credential failure. AI Extraction and Payments share the page's markup; the
 * ERP panel is its own component (`organization/ErpConnectionPanel.svelte`)
 * with one always-mounted `role="status"` region (`erp-status`) for its test,
 * save-validation and OAuth results. The ERP panel is exercised here because
 * its result has the most sources.
 *
 * The endpoint is stubbed on its exact pathname (docs/decisions.md §190): what
 * is under test is the page's announcement, not whether a mock ERP connects.
 */

for (const { success, message } of [
	{ success: true, message: 'Connected to quickbooks successfully' },
	{ success: false, message: 'Connection failed — check your credentials' }
]) {
	test(`the ERP connection-test result is announced (${success ? 'success' : 'failure'})`, async ({
		page
	}) => {
		await page.route(
			(url) => url.pathname === '/api/organization/test-erp',
			(route) => route.fulfill({ json: { success, message } })
		);
		await page.goto('/organization?section=erp');
		await expect(page.getByRole('heading', { name: 'ERP Integration', exact: true })).toBeVisible();

		// The live region exists BEFORE the result arrives — a region inserted
		// together with its text is not announced by every screen reader, so
		// "it has role=status once visible" would not be enough.
		const status = page.getByTestId('erp-status');
		await expect(status).toHaveRole('status');
		await expect(status).toHaveText('');

		// Test Connection needs an ERP chosen first (any credentials ERP; the
		// endpoint is stubbed, so the fields can stay empty).
		await page.getByTestId('erp-provider-select').selectOption('netsuite');
		await page.getByRole('button', { name: 'Test Connection' }).click();
		await expect(status).toHaveText(message);
	});
}
