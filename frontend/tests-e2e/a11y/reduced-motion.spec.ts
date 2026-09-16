import type { Page } from '@playwright/test';

import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * Under `prefers-reduced-motion: reduce`, the public pages must be complete on
 * the FIRST frame — no text waiting on an animation delay to appear.
 *
 * `app.css` collapses animation durations for that preference, and for a long
 * time that was assumed to be enough. It is not: an entrance written with a
 * delay and `backwards` fill holds its first keyframe — typically opacity 0 —
 * for the whole delay, and a collapsed duration does nothing about the delay.
 * The auth shell's staggered fields and panel (up to 0.3s) and the landing hero's
 * build sequence (up to 3.6s, the "Approved · Paid" stamp) were all invisible to
 * a reduced-motion visitor for that long. The fix is `animation-delay: 0s` in
 * the same global rule; this spec is its guard.
 *
 * Measured the instant the page's `<h1>` exists, deliberately with no polling
 * and no settle: the claim under test is "nothing is late", so waiting for it
 * to become true would test the opposite.
 *
 * `toBeVisible()` cannot express this — Playwright does not treat opacity 0 as
 * hidden — so the check reads computed opacity along each text node's ancestor
 * chain directly. Text-bearing elements only: a decorative layer that RESTS at
 * opacity 0 (the hero's extraction sweep) is correct, not late.
 */

test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

test.beforeEach(async ({ page }) => {
	await page.emulateMedia({ reducedMotion: 'reduce' });
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

/** Text-bearing elements under `root` whose effective opacity is below 1. */
async function fadedText(page: Page, root: string): Promise<string[]> {
	return page.evaluate((rootSelector) => {
		const out: string[] = [];
		for (const scope of document.querySelectorAll(rootSelector)) {
			for (const el of scope.querySelectorAll('*')) {
				const text = [...el.childNodes]
					.filter((n) => n.nodeType === Node.TEXT_NODE)
					.map((n) => n.textContent ?? '')
					.join('')
					.trim();
				if (!text) continue;
				const rect = el.getBoundingClientRect();
				if (!rect.width || !rect.height) continue;
				let opacity = 1;
				for (let node: Element | null = el; node; node = node.parentElement) {
					opacity = Math.min(opacity, Number(getComputedStyle(node).opacity));
				}
				if (opacity < 1) out.push(`${el.tagName.toLowerCase()} "${text.slice(0, 40)}" @ ${opacity}`);
			}
		}
		return out;
	}, root);
}

test.describe('reduced motion — complete on first frame', () => {
	test('landing hero: the finished card, not the start of its build', async ({ page }) => {
		await page.goto('/');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(await fadedText(page, '.hero')).toEqual([]);
	});

	test('signup: every field and the brand panel', async ({ page }) => {
		await page.goto('/signup');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(await fadedText(page, '.auth-shell')).toEqual([]);
	});

	test('verify: the error and its way out', async ({ page }) => {
		await page.goto('/verify');
		await expect(page.getByRole('heading', { level: 1, name: 'Something went wrong' })).toBeVisible();
		expect(await fadedText(page, '.auth-shell')).toEqual([]);
	});
});
