import { expect, test } from '../fixtures/helpers';

/**
 * /organization — first-time-admin "Getting started" wayfinding strip.
 *
 * It is the route's DEFAULT panel now (`?section=` absent or unrecognised lands
 * here), and its links are the page's own section navigation rather than
 * anchors into one long scroll: four `?section=` links plus `/admin` and
 * `/workflows`, which leave the page entirely. So this spec is a navigation test — it asserts that
 * activating each link actually shows the panel it names, not merely that the
 * href reads right.
 *
 * The five `#org-*` anchors these links used to carry still resolve, because
 * they exist in bookmarks and docs outside this repo; `section-nav.spec.ts`
 * owns that leg.
 */

/** The four links that stay on the page, with the panel each one names. */
const PANEL_LINKS = [
	['Company profile', 'company', 'Company Profile'],
	['Invoice defaults', 'defaults', 'Invoice Defaults'],
	// The Payments panel's one threshold is the CFO sign-off for a payment RUN,
	// so that is what this link is called. It used to be labelled "Approval
	// thresholds", which sent an admin looking for invoice approval limits to
	// the payment rails (#635).
	['CFO sign-off for payment runs', 'payments', 'Payments (ACH / Wire / RTP)'],
	['Branding', 'branding', 'Branding']
] as const;

function strip(page: import('@playwright/test').Page) {
	return page.locator('section.getting-started');
}

test.describe('/organization getting-started strip', () => {
	test('is the default panel, and offers the six first-run destinations', async ({ page }) => {
		await page.goto('/organization');

		await expect(strip(page).getByRole('heading', { name: 'Getting started' })).toBeVisible();

		const links = strip(page).getByRole('link');
		await expect(links).toHaveCount(6);

		// Each panel link names its section; the relative href keeps any other
		// query parameter the page is carrying.
		for (const [label, slug] of PANEL_LINKS) {
			await expect(strip(page).getByRole('link', { name: label })).toHaveAttribute(
				'href',
				`?section=${slug}`
			);
		}
		// Users & roles is the one that leaves the route — it is a page, not a
		// panel, and always was.
		await expect(strip(page).getByRole('link', { name: 'Users & roles' })).toHaveAttribute(
			'href',
			'/admin'
		);
		// Invoice approval thresholds are set on the workflow's approval step.
		await expect(strip(page).getByRole('link', { name: 'Approval thresholds' })).toHaveAttribute(
			'href',
			'/workflows'
		);
	});

	for (const [label, slug, heading] of PANEL_LINKS) {
		test(`"${label}" opens the ${heading} panel`, async ({ page }) => {
			await page.goto('/organization');
			await expect(strip(page).getByRole('heading', { name: 'Getting started' })).toBeVisible();

			await strip(page).getByRole('link', { name: label }).click();

			// The link's whole job: the named panel is on screen…
			await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
			// …the section is in the URL, so it can be shared and bookmarked…
			await expect(page).toHaveURL(new RegExp(`\\?section=${slug}$`));
			// …the rail marks it as where the reader is…
			await expect(page.locator(`[data-section-link="${slug}"]`)).toHaveAttribute(
				'aria-current',
				'page'
			);
			// …and the strip it was clicked from has been replaced, not scrolled
			// past. (Asserted last: an absence check leading would pass against a
			// page that had rendered nothing yet.)
			await expect(strip(page)).toHaveCount(0);
		});
	}

	test('"Users & roles" leaves the settings page for /admin', async ({ page }) => {
		await page.goto('/organization');
		await expect(strip(page).getByRole('heading', { name: 'Getting started' })).toBeVisible();

		await strip(page).getByRole('link', { name: 'Users & roles' }).click();

		await expect(page.getByRole('heading', { name: 'Users & Roles', exact: true })).toBeVisible();
		await expect(page).toHaveURL(/\/admin$/);
	});

	test('"Approval thresholds" leaves for /workflows, where invoice thresholds are set', async ({
		page
	}) => {
		await page.goto('/organization');
		await expect(strip(page).getByRole('heading', { name: 'Getting started' })).toBeVisible();

		await strip(page).getByRole('link', { name: 'Approval thresholds' }).click();

		await expect(page.getByRole('heading', { name: 'Workflows', exact: true })).toBeVisible();
		await expect(page).toHaveURL(/\/workflows$/);
	});
});
