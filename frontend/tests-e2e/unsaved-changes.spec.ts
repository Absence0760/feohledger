import type { Page } from '@playwright/test';
import { expect, signInAndWait, test } from './fixtures/helpers';

/**
 * The app-wide unsaved-changes guard (`#lib/stores/unsavedChanges.svelte.ts`).
 *
 * Every page that tracks unsaved edits asks through ONE in-app dialog before
 * an in-app navigation discards them — the workflow editor used to raise a
 * native `confirm()` and the other editors raised nothing at all. "Stay" keeps
 * the reader (and the edits) where they are; "Leave" replays the navigation
 * they asked for, Back included.
 */

const dialog = (page: Page) => page.getByRole('dialog', { name: 'Unsaved changes' });

async function openFirstWorkflow(page: Page) {
	await page.goto('/workflows');
	const openLink = page.getByRole('button', { name: /Edit workflow|Open workflow/i }).first();
	const target = (await openLink.count())
		? openLink
		: page.locator('tbody tr a, tbody tr button').first();
	await target.click();
	await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]+/);
}

async function dirtyWorkflowName(page: Page) {
	await page.getByRole('button', { name: /Edit .*name/i }).click();
	const nameInput = page.getByRole('textbox', { name: /workflow name/i });
	await nameInput.fill('Dirtied Workflow Name');
	await nameInput.blur();
	// Save enabling is the visible proof the edit registered as dirty.
	await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
}

test.describe('unsaved-changes guard', () => {
	test('the workflow editor asks in the app dialog; Stay keeps the edits', async ({ page }) => {
		// A native confirm would be the old behaviour — fail loudly if one appears.
		page.on('dialog', (d) => {
			void d.dismiss();
			throw new Error(`unexpected native dialog: ${d.message()}`);
		});
		await openFirstWorkflow(page);
		await dirtyWorkflowName(page);
		const here = page.url();

		await page.locator('a[href="/invoices"]').first().click();
		await expect(dialog(page)).toBeVisible();
		// The safe answer holds focus.
		await expect(dialog(page).getByRole('button', { name: 'Stay on page' })).toBeFocused();

		await dialog(page).getByRole('button', { name: 'Stay on page' }).click();
		await expect(dialog(page)).toHaveCount(0);
		expect(page.url()).toBe(here);
		await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
	});

	test('Leave continues to where the reader was going', async ({ page }) => {
		await openFirstWorkflow(page);
		await dirtyWorkflowName(page);

		await page.locator('a[href="/invoices"]').first().click();
		await dialog(page).getByRole('button', { name: 'Leave without saving' }).click();
		await expect(page).toHaveURL(/\/invoices(\?|$)/);
		await expect(dialog(page)).toHaveCount(0);
	});

	test('Back is held too, and Leave takes the same history step', async ({ page }) => {
		// /workflows → the editor, so Back has a real in-app step to take.
		await openFirstWorkflow(page);
		await dirtyWorkflowName(page);
		const here = page.url();

		await page.goBack();
		await expect(dialog(page)).toBeVisible();
		await dialog(page).getByRole('button', { name: 'Stay on page' }).click();
		expect(page.url()).toBe(here);

		await page.goBack();
		await dialog(page).getByRole('button', { name: 'Leave without saving' }).click();
		await expect(page).toHaveURL(/\/workflows(\?|$)/);
	});

	test('a page with nothing unsaved navigates without asking', async ({ page }) => {
		await openFirstWorkflow(page);
		await page.locator('a[href="/invoices"]').first().click();
		await expect(page).toHaveURL(/\/invoices(\?|$)/);
		await expect(dialog(page)).toHaveCount(0);
	});
});

test.describe('unsaved-changes guard on /admin/retention', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('an edited window is guarded by the same dialog', async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/admin/retention');
		await expect(page.getByTestId('retention-loading')).toHaveCount(0, { timeout: 10_000 });

		const input = page.getByTestId('retention-input-invoices');
		await input.fill(String(Number(await input.inputValue()) + 1));
		await expect(page.getByRole('button', { name: 'Save changes' })).toBeEnabled();

		await page.locator('a[href="/invoices"]').first().click();
		await expect(dialog(page)).toBeVisible();
		await dialog(page).getByRole('button', { name: 'Stay on page' }).click();
		await expect(page).toHaveURL(/\/admin\/retention/);
		// Nothing was saved: reset the field rather than leave a dirty form behind.
		await page.getByRole('button', { name: 'Reset' }).click();
	});
});
