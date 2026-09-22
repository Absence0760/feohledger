import type { Locator, Page } from '@playwright/test';

/**
 * The /exceptions resolve dialogs, found by role AND the form's `data-testid`.
 *
 * Never by accessible name. The name is the dialog's translated title, so a spec
 * keyed on the English string asserts something a translator is free to change
 * and stops matching under every other locale — which is why the specs moved
 * off it before the strings were keyed (GitHub issue #443). `getByRole('dialog')`
 * still proves the element IS the dialog the user sees; the test id only says
 * which one.
 */
export function resolveDialog(page: Page): Locator {
	return page.getByRole('dialog').filter({ has: page.getByTestId('exception-resolve-form') });
}

export function bulkResolveDialog(page: Page): Locator {
	return page
		.getByRole('dialog')
		.filter({ has: page.getByTestId('exception-bulk-resolve-form') });
}
