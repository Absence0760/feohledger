import { expect, test } from '../fixtures/helpers';

/**
 * The consent banner renders in the visitor's stored locale.
 *
 * It shipped with no `m()` call in it at all — title, both category names, both
 * choice buttons, the region's `aria-label` and the Cookie Notice link text
 * were literals — so a `de` / `es` / `fr` / `ja` / `pt-BR` visitor met an
 * English dialog as the FIRST thing on screen and the one surface they had to
 * interact with to dismiss. That is an ePrivacy Art 5(3) problem before it is
 * an i18n one: consent has to be *informed*, and a dialog the reader cannot
 * read is weak evidence that it was given.
 *
 * Why this is an e2e and not another unit test: `untranslatedCopy.test.ts`
 * already proves the component holds no literal, which is the static half. The
 * half only a browser can show is the MOUNT PATH — the banner is rendered by
 * `routes/+layout.svelte` outside the routed slot, before auth and before any
 * page, while `initLocale()` resolves the stored locale through a lazy
 * `import()` of the catalogue chunk. A banner that mounted before that promise
 * settled and never re-rendered would be green in every unit test and English
 * on screen.
 *
 * The assertions are ordered on purpose: the German text is asserted FIRST
 * (auto-waiting, so it is the real signal that the catalogue landed), and only
 * then is the English asserted absent. The reverse order passes vacuously
 * against the pre-hydration document, which carries neither.
 */

/** German values of the `consent.*` keys, from `i18n/locales/de.ts`. */
const DE = {
	ariaLabel: 'Cookie- und Datenschutzeinwilligung',
	title: 'Ihre Datenschutz-Einstellungen',
	accept: 'Alle akzeptieren',
	reject: 'Nicht erforderliche ablehnen',
	manage: 'Verwalten',
	necessaryTerm: 'Unbedingt erforderlich (immer aktiv)'
};

test.describe('consent banner honours the stored locale', () => {
	test('a de visitor gets a German banner, not an English one', async ({ page }) => {
		// Seed both keys before any app code runs: the locale the returning
		// visitor chose, and a cleared consent so the banner is shown at all.
		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
			// The per-worker storage state signs the admin in and may already
			// carry a recorded choice; clearing it here (an init script runs
			// before the page's own scripts, on every navigation) is what makes
			// the banner show at all.
			localStorage.removeItem('feoh_consent_choice');
		});
		await page.goto('/');

		// The accessible name IS a translated string here, so finding the region
		// by its German name proves the `aria-label` was keyed — a screen reader
		// announces this, and it was the one literal a purely visual check misses.
		const banner = page.getByRole('region', { name: DE.ariaLabel });
		await expect(banner).toBeVisible();

		await expect(banner.getByRole('heading', { name: DE.title })).toBeVisible();
		await expect(banner.getByRole('button', { name: DE.accept })).toBeVisible();
		await expect(banner.getByRole('button', { name: DE.reject })).toBeVisible();

		// The details panel is behind the Manage toggle, and its two category
		// names were literals too.
		await banner.getByRole('button', { name: DE.manage }).click();
		await expect(banner.getByText(DE.necessaryTerm)).toBeVisible();

		// Only now, with the German catalogue provably applied, is the absence of
		// the English meaningful.
		await expect(page.getByRole('region', { name: 'Cookie and privacy consent' })).toHaveCount(0);
		await expect(banner.getByRole('button', { name: 'Accept all' })).toHaveCount(0);
		await expect(banner.getByText('Your privacy choices')).toHaveCount(0);
	});
});
