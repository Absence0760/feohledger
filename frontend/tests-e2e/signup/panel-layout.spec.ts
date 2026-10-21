import type { Locator } from '@playwright/test';

import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * The auth shell's brand panel must wrap its text, never clip it.
 *
 * Issue #635: at ~1000px wide (just above the 960px single-column
 * breakpoint) signup's "What happens next" panel cut step 3 off mid-word —
 * "…or from the link in your welc" — because the panel is `overflow: hidden`
 * (it has to be: the decorative art is drawn wider than the panel and bleeds
 * off its edge on purpose) and its content was taller or wider than the box.
 * Clipping is right for the art and wrong for text, so this measures every
 * text-bearing element in the panel against the panel's own box.
 */

test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

/** Every text-bearing element in the panel that sits (even partly) outside it. */
async function clippedText(panel: Locator): Promise<string[]> {
	return panel.evaluate((aside) => {
		const box = aside.getBoundingClientRect();
		const out: string[] = [];
		const walker = document.createTreeWalker(aside, NodeFilter.SHOW_TEXT);
		for (let n = walker.nextNode(); n; n = walker.nextNode()) {
			const text = n.textContent?.trim();
			if (!text) continue;
			const range = document.createRange();
			range.selectNodeContents(n);
			for (const r of range.getClientRects()) {
				// A one-pixel slack for sub-pixel rounding only.
				if (r.left < box.left - 1 || r.right > box.right + 1 || r.bottom > box.bottom + 1) {
					out.push(
						`"${text.slice(0, 40)}" [${Math.round(r.left)}..${Math.round(r.right)} × ..${Math.round(r.bottom)}] ` +
							`outside panel [${Math.round(box.left)}..${Math.round(box.right)} × ..${Math.round(box.bottom)}]`
					);
				}
			}
		}
		return out;
	});
}

for (const size of [
	{ width: 1000, height: 700 },
	{ width: 1000, height: 560 },
	{ width: 1280, height: 720 },
	{ width: 961, height: 640 },
	// Below 960px signup keeps the panel (`panelOnMobile`), stacked under the
	// form and at its narrowest — and still clipping. `a11y/reflow.spec.ts`
	// cannot see this: it measures page-level scroll, which a clip hides.
	{ width: 375, height: 812 },
	{ width: 320, height: 720 }
]) {
	test(`the signup panel's text fits inside the panel at ${size.width}×${size.height}`, async ({
		page
	}) => {
		await page.setViewportSize(size);
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.goto('/signup');
		const panel = page.locator('aside.panel');
		await expect(panel.getByText('Set your password and sign in')).toBeVisible();

		expect(await clippedText(panel)).toEqual([]);
		// And the page itself reflows (WCAG 1.4.10): `/signup` is outside
		// `a11y/reflow.spec.ts`'s signed-in route walk, so it is measured here.
		const sideways = await page.evaluate(() => {
			const doc = document.scrollingElement!;
			return doc.scrollWidth - doc.clientWidth;
		});
		expect(sideways).toBeLessThanOrEqual(0);
	});
}

// The copy above wraps at spaces, so it only proves the common case. A word
// with no break opportunity — a German compound, an address, a translation
// longer than English — is what a clip actually eats, so put one in step 3
// and require it to wrap inside the panel too.
for (const width of [1000, 320]) test(`an unbroken word in the panel wraps instead of running under the clip (${width}px)`, async ({ page }) => {
	await page.setViewportSize({ width, height: 720 });
	await page.emulateMedia({ reducedMotion: 'reduce' });
	await page.goto('/signup');
	const panel = page.locator('aside.panel');
	const step3 = panel.locator('.step-text span').last();
	await expect(step3).toBeVisible();
	await step3.evaluate((el) => {
		el.textContent = `Rechnungseingangsbestätigungsbenachrichtigung${'x'.repeat(40)}`;
	});
	expect(await clippedText(panel)).toEqual([]);
});
