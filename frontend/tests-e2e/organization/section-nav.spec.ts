import { expect, test } from '../fixtures/helpers';

/**
 * /organization — the panel navigation itself (`ui/SettingsRail.svelte` +
 * the `?section=` contract in `routes/organization/+page.svelte`).
 *
 * Sixteen panels of unrelated settings share this route because they share
 * `PATCH /api/organization`. They used to share one scroll; the route now shows
 * one panel at a time, selected by `?section=<slug>` and picked from a left
 * rail. Every other spec in this directory navigates straight to the panel it
 * is about — this one is the only one that exercises the navigation, so the
 * five things that make it a real address rather than a widget live here:
 *
 *   • an absent or unrecognised slug falls back instead of rendering nothing,
 *   • the URL and the rail's `aria-current` move together,
 *   • the section is genuine history, so Back works,
 *   • the five pre-panel `#org-*` anchors still resolve, and lose to an
 *     explicit `?section=` (they are in bookmarks and docs outside this repo,
 *     so they are load-bearing), and
 *   • the rail collapses behind a disclosure on a phone.
 *
 * Plus one structural claim the page makes deliberately and could lose by
 * accident: field state lives at PAGE level, so an unsaved edit survives a trip
 * to another panel. A future refactor that moved state into per-panel
 * components would silently start discarding typed input, and nothing else
 * would notice.
 *
 * `SettingsRail` is shared with `/profile`, and `auth/profile-section-nav.spec.ts`
 * deliberately does NOT re-test those mechanics — it points here for them and
 * covers only what is specific to that page. So this file is the single home
 * for the rail's behaviour: don't thin it out on the grounds that another spec
 * exercises the same component.
 */

/**
 * The rail itself. `exact` matters: `/organization` also carries the route-level
 * `SectionTabs` bar, whose accessible name is "Settings section**s**" — a
 * substring match resolves to both and fails strict mode.
 */
function rail(page: import('@playwright/test').Page) {
	return page.getByRole('navigation', { name: 'Settings section', exact: true });
}

/** The rail link for a slug. Its `aria-current` is the active marker. */
function railLink(page: import('@playwright/test').Page, slug: string) {
	return page.locator(`[data-section-link="${slug}"]`);
}

function heading(page: import('@playwright/test').Page, name: string) {
	return page.getByRole('heading', { name, exact: true });
}

test.describe('/organization section navigation', () => {
	test('no ?section= lands on Getting started', async ({ page }) => {
		await page.goto('/organization');

		await expect(heading(page, 'Getting started')).toBeVisible();
		await expect(railLink(page, 'getting-started')).toHaveAttribute('aria-current', 'page');

		// The rail is a landmark with a name, so a screen-reader user can jump to
		// it and know what it is. It is `<nav>` + `aria-current`, not a tablist:
		// each destination is an address.
		await expect(rail(page)).toBeVisible();
	});

	test('an unrecognised ?section= falls back to Getting started', async ({ page }) => {
		await page.goto('/organization?section=nonsense');

		// Not an empty page, and not a broken rail: the fallback is a real panel
		// with a real active marker, the same treatment `/admin` gives a stale
		// `?tab=`.
		await expect(heading(page, 'Getting started')).toBeVisible();
		await expect(railLink(page, 'getting-started')).toHaveAttribute('aria-current', 'page');
		await expect(heading(page, 'Company Profile')).toHaveCount(0);
	});

	test('a rail link changes the panel, the URL and aria-current together', async ({ page }) => {
		await page.goto('/organization');
		await expect(heading(page, 'Getting started')).toBeVisible();

		await railLink(page, 'erp').click();

		await expect(heading(page, 'ERP Integration')).toBeVisible();
		await expect(page).toHaveURL(/\?section=erp$/);
		await expect(railLink(page, 'erp')).toHaveAttribute('aria-current', 'page');
		// The marker MOVED — two panels both claiming to be current is the
		// failure this catches.
		await expect(railLink(page, 'getting-started')).not.toHaveAttribute('aria-current', 'page');
		await expect(heading(page, 'Getting started')).toHaveCount(0);
	});

	test('Back returns to the previous panel', async ({ page }) => {
		await page.goto('/organization?section=company');
		await expect(heading(page, 'Company Profile')).toBeVisible();

		await railLink(page, 'plan').click();
		await expect(heading(page, 'Plan')).toBeVisible();
		await expect(page).toHaveURL(/\?section=plan$/);

		// A section is a history entry, not a widget's local state — so the
		// browser's own Back button has to work, and a reader who lands here
		// from a link is not trapped.
		await page.goBack();
		await expect(heading(page, 'Company Profile')).toBeVisible();
		await expect(page).toHaveURL(/\?section=company$/);
		await expect(railLink(page, 'company')).toHaveAttribute('aria-current', 'page');

		await page.goForward();
		await expect(heading(page, 'Plan')).toBeVisible();
		await expect(railLink(page, 'plan')).toHaveAttribute('aria-current', 'page');
	});

	/**
	 * The five anchors the page was navigated by before it had panels. They
	 * resolve to the panel that now owns the content — by **derivation**, in the
	 * `section` rune itself, not by rewriting the URL: `replaceState` throws if
	 * it runs before the SvelteKit router has initialised, and an effect on
	 * first hydration is exactly that window. So the URL keeps its `#org-*`
	 * hash and the panel is right anyway; the hash clears itself on the first
	 * rail click, whose hrefs are query-only.
	 *
	 * These are the load-bearing half of the URL contract: unlike a `?section=`
	 * slug, an `#org-*` anchor is a URL this page no longer produces anywhere,
	 * so nothing but this test would notice it breaking.
	 */
	const LEGACY_ANCHORS = [
		['org-company', 'company', 'Company Profile'],
		['org-defaults', 'defaults', 'Invoice Defaults'],
		['org-branding', 'branding', 'Branding'],
		['org-email-intake', 'email-intake', 'Email Intake'],
		['org-payments', 'payments', 'Payments (ACH / Wire / RTP)']
	] as const;

	for (const [anchor, slug, panelHeading] of LEGACY_ANCHORS) {
		test(`the legacy #${anchor} anchor still opens the ${panelHeading} panel`, async ({
			page
		}) => {
			await page.goto(`/organization#${anchor}`);

			// The whole claim: a saved link lands its reader on the panel that
			// owns what they bookmarked…
			await expect(heading(page, panelHeading)).toBeVisible();
			// …and the rail agrees, so they are oriented rather than dropped into
			// a panel the navigation says they are not in.
			await expect(railLink(page, slug)).toHaveAttribute('aria-current', 'page');

			// One rail click and the stale hash is gone — the rail's hrefs are
			// query-only, so nothing carries the anchor forward.
			await railLink(page, 'plan').click();
			await expect(page).toHaveURL(/\?section=plan$/);
		});
	}

	test('an explicit ?section= wins over a legacy anchor riding along', async ({ page }) => {
		// Precedence matters for a link that was built by pasting a slug onto a
		// URL that still had an anchor on it. Query first, hash only as the
		// fallback.
		await page.goto('/organization?section=plan#org-company');

		await expect(heading(page, 'Plan')).toBeVisible();
		await expect(railLink(page, 'plan')).toHaveAttribute('aria-current', 'page');
		await expect(heading(page, 'Company Profile')).toHaveCount(0);
	});

	test('an unsaved edit survives a trip to another panel', async ({ page }) => {
		await page.goto('/organization?section=company');

		const phone = page
			.locator('section.card', { has: page.getByRole('heading', { name: 'Company Profile' }) })
			.locator('input[type="tel"]');
		// The panel is inside the page's `{#if org}` gate, so a visible field
		// means `loadOrg()` has already assigned it — no late response can
		// overwrite the edit after we type, and no wait beyond this is needed.
		await expect(phone).toBeVisible();
		const typed = `+1-555-unsaved-${Date.now() % 100000}`;
		await phone.fill(typed);

		// Away…
		await railLink(page, 'defaults').click();
		await expect(heading(page, 'Invoice Defaults')).toBeVisible();
		await expect(phone).toHaveCount(0);

		// …and back. Only the MARKUP is conditional: the field state lives at
		// page level, so the edit — and its still-unpressed Save button — are
		// where they were left.
		await railLink(page, 'company').click();
		await expect(heading(page, 'Company Profile')).toBeVisible();
		await expect(phone).toHaveValue(typed);

		// Nothing was saved, so there is nothing to restore: the tenant's phone
		// number is untouched and a reload discards the edit.
		await page.reload();
		await expect(phone).toBeVisible();
		await expect(phone).not.toHaveValue(typed);
	});

	test('on a 320px viewport the rail is behind its toggle', async ({ page }) => {
		// 320px is the width WCAG 1.4.10 names, and the rail's own breakpoint is
		// 60rem — so this is the layout a phone gets: the panel, not a 16-row
		// menu above it.
		await page.setViewportSize({ width: 320, height: 720 });
		await page.goto('/organization');
		await expect(heading(page, 'Getting started')).toBeVisible();

		const toggle = rail(page).getByRole('button');
		await expect(toggle).toBeVisible();
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		// It names where the reader is, so the collapsed rail still answers
		// "which section is this?".
		await expect(toggle).toContainText('Getting started');

		// `aria-controls` has to actually resolve, or the disclosure lies to a
		// screen reader about what it opens. The id is per-instance
		// (`$props.id()`), so the contract is the RELATIONSHIP — the toggle names
		// the element that holds the links — never the literal id.
		const listId = await toggle.getAttribute('aria-controls');
		expect(listId, 'the toggle must name the list it controls').toBeTruthy();
		await expect(page.locator(`[id="${listId}"] [data-section-link="security"]`)).toHaveCount(1);

		// The link EXISTS (so the assertion below is not vacuous) and is not
		// reachable: the list is `display: none` until the disclosure is opened.
		const link = railLink(page, 'security');
		await expect(link).toHaveCount(1);
		await expect(link).toBeHidden();

		await toggle.click();
		await expect(toggle).toHaveAttribute('aria-expanded', 'true');
		await expect(link).toBeVisible();

		await link.click();
		await expect(heading(page, 'Security')).toBeVisible();
		await expect(page).toHaveURL(/\?section=security$/);
		// Choosing a section closes the picker behind you — on a phone the panel
		// is what you came for.
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await expect(link).toBeHidden();
	});
});
