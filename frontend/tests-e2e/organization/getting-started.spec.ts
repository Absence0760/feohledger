import { expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /organization — first-time-admin "Getting started" wayfinding strip.
 *
 * It is the route's DEFAULT panel now (`?section=` absent or unrecognised lands
 * here), and its links are the page's own section navigation rather than
 * anchors into one long scroll: seven `?section=` links plus `/admin`,
 * `/vendors` and `/workflows`, which leave the page entirely. So this spec is a
 * navigation test — it asserts that activating each link actually shows the
 * panel it names, not merely that the href reads right.
 *
 * Each step that has a fact behind it is ticked from the same server-side
 * derivation as the dashboard's setup checklist (`GET /api/dashboard/onboarding`,
 * `docs/decisions.md` §309); the ones with none — defaults, payment-run sign-off,
 * branding — carry no done state at all.
 *
 * The five `#org-*` anchors these links used to carry still resolve, because
 * they exist in bookmarks and docs outside this repo; `section-nav.spec.ts`
 * owns that leg.
 */

/** The links that stay on the page, with the panel each one names. */
const PANEL_LINKS = [
	['Company profile', 'company', 'Company Profile'],
	['Invoice defaults', 'defaults', 'Invoice Defaults'],
	['Sign-in security', 'security', 'Security'],
	['ERP integration', 'erp', 'ERP Integration'],
	['Email intake', 'email-intake', 'Email Intake'],
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
	test('is the default panel, and offers the ten first-run destinations', async ({ page }) => {
		await page.goto('/organization');

		await expect(strip(page).getByRole('heading', { name: 'Getting started' })).toBeVisible();

		const links = strip(page).getByRole('link');
		await expect(links).toHaveCount(10);

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
		await expect(strip(page).getByRole('link', { name: 'Vendors' })).toHaveAttribute(
			'href',
			'/vendors'
		);
	});

	test("an admin's steps are ticked from the server's facts, and only those", async ({ page }) => {
		const progress = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/dashboard/onboarding' && r.ok()
		);
		await page.goto('/organization');
		const facts = await (await progress).json();

		const steps = page.getByTestId('org-getting-started-steps');
		const TRACKED = ['company', 'users', 'security', 'erp', 'vendors', 'workflow', 'email_intake'];
		for (const id of TRACKED) {
			await expect(steps.locator(`li[data-step="${id}"]`)).toHaveAttribute(
				'data-done',
				String(facts[id])
			);
		}
		// No fact, no done state: never drawn as finished OR unfinished.
		for (const id of ['defaults', 'payments', 'branding']) {
			await expect(steps.locator(`li[data-step="${id}"]`)).toHaveAttribute('data-done', 'untracked');
		}
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

	test('a non-admin gets the same links with no done states', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		let asked = false;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname === '/api/dashboard/onboarding') asked = true;
		});
		await page.goto('/organization');

		const steps = page.getByTestId('org-getting-started-steps');
		await expect(steps.locator('li')).toHaveCount(10);
		await expect(steps.locator('li[data-done="untracked"]')).toHaveCount(10);
		// The progress read is admin-only; a doomed request is not made.
		expect(asked).toBe(false);
	});

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
