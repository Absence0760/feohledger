import { expect, signInAndWait, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * The help centre (docs/decisions.md §244, frontend/docs/help-centre.md).
 *
 * What the unit tests can't see: that the pages render in the app shell, that
 * the role a reader holds picks their starting path, that a guide only offers
 * to open a page the reader's role can open, that every page header links to
 * its help, and that an ⓘ tip works from the keyboard. Content integrity —
 * every reference resolving — is `src/lib/help/content.test.ts`'s job.
 */

test.describe('help centre', () => {
	test('the sidebar opens the help centre, whose start path follows the reader’s role', async ({ page }) => {
		await page.goto('/invoices');
		await page.getByTestId('sidebar-help').click();
		await expect(page).toHaveURL(/\/help$/);
		await expect(page.getByRole('heading', { level: 1 })).toContainText('works');
		await expect(page.getByTestId('sidebar-help')).toHaveAttribute('aria-current', 'page');

		// The default identity is the tenant admin: their own path is preselected.
		const admin = page.getByRole('button', { name: /^Admin/ });
		await expect(admin).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByTestId('help-start-card')).toHaveAttribute('href', '/help/guides/start-admin');

		// Another role's path is one click away, and the choice is in the URL.
		await page.getByRole('button', { name: /^AP Clerk/ }).click();
		await expect(page).toHaveURL(/\?role=ap_clerk$/);
		await expect(page.getByTestId('help-start-card')).toHaveAttribute('href', '/help/guides/start-ap-clerk');
	});

	test('the lifecycle walkthrough moves by arrow key and shows the status badges', async ({ page }) => {
		await page.goto('/help');
		const walk = page.getByTestId('invoice-lifecycle');
		const tabs = walk.getByRole('tab');
		await expect(tabs.first()).toHaveAttribute('aria-selected', 'true');
		await tabs.first().focus();
		await page.keyboard.press('ArrowRight');
		await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
		await expect(tabs.nth(1)).toBeFocused();
		await expect(walk.getByRole('tabpanel')).toContainText('Extracting');
		await page.keyboard.press('End');
		await expect(walk.getByRole('tabpanel')).toContainText('Paid');
	});

	test('a guide offers to open its page only to a role that can open it', async ({ page, tenantClerk }) => {
		await page.goto('/help/guides/run-payments');
		await expect(page.getByTestId('help-guide')).toBeVisible();
		await expect(page.getByTestId('help-open-page')).toHaveAttribute('href', '/payments');

		// An AP clerk can't open Payments (nav.ts), so the button isn't offered.
		await signInAndWait(page, tenantClerk);
		await page.goto('/help/guides/run-payments');
		await expect(page.getByTestId('help-guide')).toBeVisible();
		await expect(page.getByTestId('help-open-page')).toHaveCount(0);
	});

	test('a page header links to how that page works', async ({ page }) => {
		await page.goto('/invoices');
		const link = page.getByTestId('page-help-link');
		await expect(link).toHaveText('How this page works');
		await link.click();
		await expect(page).toHaveURL(/\/help\/guides\/[a-z-]+$/);
		await expect(page.getByTestId('help-guide')).toBeVisible();
	});

	test('search finds guides, pages and terms, and follows the URL', async ({ page }) => {
		await page.goto('/help');
		await page.getByRole('searchbox', { name: 'Search help' }).fill('three-way');
		await expect(page).toHaveURL(/\/help\/search\?q=three-way/);
		await expect(page.getByTestId('help-search-hit').first()).toBeVisible();
		await expect(page.getByTestId('help-search-status')).toContainText('three-way');

		await page.goto('/help/search?q=zzqqxxnothing');
		await expect(page.getByTestId('help-search-status')).toContainText('Nothing matches');
		await expect(page.getByTestId('help-search-hit')).toHaveCount(0);
	});

	test('search keeps what is typed, spaces and all, key by key', async ({ page }) => {
		// `fill` sets the value in one go and could never catch this: the box used
		// to be re-synced from the (trimmed, one-keystroke-behind) URL while typing,
		// which swallowed the space in "three way".
		await page.goto('/help');
		const box = page.getByRole('searchbox', { name: 'Search help' });
		await box.pressSequentially('three way match');
		await expect(box).toHaveValue('three way match');
		await expect(page).toHaveURL(/\/help\/search\?q=three%20way%20match$/);
		await expect(page.getByTestId('help-search-hit').first()).toBeVisible();

		// One history entry for the whole search: Back leaves search.
		await page.goBack();
		await expect(page).toHaveURL(/\/help$/);
	});

	test('the glossary jumps to and highlights a linked term', async ({ page }) => {
		await page.goto('/help/glossary#three-way-match');
		const entry = page.locator('#three-way-match');
		await expect(entry).toHaveClass(/target/);
		await expect(entry).toBeInViewport();
	});

	test('a page the reader cannot open is listed but not linked', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/help/pages');
		const payments = page.locator('[id="%2Fpayments"]');
		await expect(payments).toContainText('Not available to your role');
		await expect(payments.getByRole('link', { name: 'Payments', exact: true })).toHaveCount(0);
	});
});

test.describe('help centre in another language', () => {
	test.beforeEach(async ({ page }) => {
		await page.addInitScript(() => localStorage.setItem('feoh_locale', 'de'));
	});

	test('guide prose is marked English, and the UI labels inside it are not', async ({ page }) => {
		await page.goto('/help/guides/approve-invoices');
		await expect(page.getByTestId('help-guide')).toBeVisible();
		// The translated notice says the prose is English…
		await expect(page.getByTestId('help-english-notice')).toBeVisible();
		await expect(page.getByTestId('help-english-notice')).not.toContainText('Guides are written in English');
		// …which carries lang="en" (WCAG 3.1.2), while every `{ui:}` label inside
		// it is the reader's language and says so.
		const body = page.locator('.body');
		await expect(body).toHaveAttribute('lang', 'en');
		const label = body.locator('.ui-label').first();
		await expect(label).toHaveAttribute('lang', 'de');
		// The chrome around it is German.
		await expect(page.getByRole('navigation', { name: 'Auf dieser Seite' })).toBeVisible();
	});
});

test.describe('HelpTip', () => {
	test('Escape inside a dialog closes the tip, not the dialog', async ({ page }) => {
		await page.goto('/vendors');
		await page.locator('table tbody tr').first().locator('td.vendor-name .row-link').click();
		const modal = page.getByRole('dialog', { name: 'Vendor screening and risk' });
		await expect(modal).toBeVisible();
		const tip = modal.getByTestId('help-tip').first();
		await tip.click();
		await expect(tip).toHaveAttribute('aria-expanded', 'true');
		await page.keyboard.press('Escape');
		await expect(tip).toHaveAttribute('aria-expanded', 'false');
		await expect(modal).toBeVisible();
	});

	test('a click inside the open bubble keeps it open and its links work', async ({ page }) => {
		await page.goto('/payments');
		const tip = page.getByTestId('help-tip').first();
		await tip.click();
		const bubble = page.locator(`#${await tip.getAttribute('aria-controls')}`);
		await bubble.locator('.short').click();
		await expect(tip).toHaveAttribute('aria-expanded', 'true');
		await bubble.getByRole('link', { name: 'More in the glossary' }).click();
		await expect(page).toHaveURL(/\/help\/glossary#[a-z-]+$/);
	});

	test('an open tip leaves the page free of axe violations', async ({ page }) => {
		await page.goto('/payments');
		const tip = page.getByTestId('help-tip').first();
		await tip.click();
		await expect(page.getByText('Further reading')).toBeVisible();
		await expectNoA11yViolations(page);
	});

	test('opens and closes from the keyboard, and links to further reading', async ({ page }) => {
		await page.goto('/payments');
		const tip = page.getByTestId('help-tip').first();
		await expect(tip).toBeVisible();
		await tip.focus();
		await page.keyboard.press('Enter');
		await expect(tip).toHaveAttribute('aria-expanded', 'true');
		const bubble = page.locator(`#${await tip.getAttribute('aria-controls')}`);
		await expect(bubble.getByRole('link', { name: 'More in the glossary' })).toBeVisible();
		await expect(bubble.getByText('Further reading')).toBeVisible();
		await page.keyboard.press('Escape');
		await expect(tip).toHaveAttribute('aria-expanded', 'false');
		await expect(tip).toBeFocused();
	});
});
