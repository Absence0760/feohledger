import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import AxeBuilder from '@axe-core/playwright';

import { expect, test } from '../fixtures/helpers';
import { formatViolations } from './axe-helper';

/**
 * WCAG 2.2 AA — 1.4.10 Reflow, asserted over the app's WHOLE route surface.
 *
 * At a 320px viewport (the width 1.4.10 names) no page may scroll sideways.
 * Content that genuinely cannot reflow — a wide data table — is allowed to
 * scroll *inside its own container*; the DOCUMENT is not. So the measurement
 * is `documentElement.scrollWidth - clientWidth`, which an internal scroller
 * does not contribute to.
 *
 * ## Why the route list is read off disk
 *
 * This guard used to name five paths by hand (`/`, `/invoices`, `/vendors`,
 * `/payments`, `/contracts`) while the app shipped forty-odd. A hand-kept list
 * only covers the routes someone remembered to add, and the routes nobody
 * remembers are exactly the ones nobody has looked at narrow — which is how
 * `/organization` sat in `docs/known-issues.md` with a measured 137px overflow,
 * and how `/adaptive` (359px), `/expenses` (206px), `/profile` (131px),
 * `/cfo` (81px), `/admin/retention` (9px) and `/reports` (7px) were failing the
 * same criterion with nothing to say so. Enumerating `src/routes` instead means
 * a new page is covered the moment it exists, with no list to remember.
 *
 * ## Why keyboard reachability is asserted here too (WCAG 2.1.1)
 *
 * The remedy this criterion allows — let a table that cannot reflow scroll
 * inside its own container — is exactly what creates a region only a mouse can
 * pan. axe's `scrollable-region-focusable` reports it, but only while the
 * content ACTUALLY overflows, so the default-width sweep in `axe.spec.ts`
 * cannot see it: at 1280px nothing scrolls. 320px is where every such region is
 * live, so each route runs that one rule here, at the same width, in the same
 * visit. A wide table is fine; a wide table the keyboard cannot reach is not.
 *
 * Every exclusion below is a route that is NOT part of the signed-in app shell,
 * and each one says what covers it instead. Do not add an app route here to
 * turn a red run green — fix the layout (root `CLAUDE.md` § Fix bugs at the
 * source).
 */

const ROUTES_DIR = fileURLToPath(new URL('../../src/routes', import.meta.url));

/**
 * Prefixes this spec deliberately does not visit, with the reason. A prefix
 * matches the route itself and everything under it.
 */
const NOT_THE_APP_SHELL: { prefix: string; why: string }[] = [
	{
		prefix: '/legal',
		why: 'published legal text, not the app shell — covered by legal/a11y-narrow.spec.ts'
	},
	{ prefix: '/login', why: 'pre-auth AuthShell; this spec signs in, so it would only redirect' },
	{ prefix: '/signup', why: 'pre-auth AuthShell (self-service tenant signup)' },
	{ prefix: '/verify', why: 'pre-auth AuthShell, and needs a one-time token in the URL' },
	{ prefix: '/change-password', why: 'pre-auth AuthShell (forced password change)' },
	{
		prefix: '/portal',
		why: 'supplier portal — a VendorUser surface, not reachable with an employee JWT'
	}
];

/** Every `+page.svelte` directory under `src/routes`, as a URL path. */
function discoverRoutes(): string[] {
	const found: string[] = [];
	const walk = (dir: string, urlPath: string) => {
		const entries = readdirSync(dir, { withFileTypes: true });
		if (entries.some((e) => e.isFile() && e.name === '+page.svelte')) {
			found.push(urlPath === '' ? '/' : urlPath);
		}
		for (const entry of entries) {
			if (!entry.isDirectory()) continue;
			// A dynamic segment has no id to visit without inventing data; the
			// specs that own those records cover them.
			if (entry.name.includes('[')) continue;
			walk(join(dir, entry.name), `${urlPath}/${entry.name}`);
		}
	};
	walk(ROUTES_DIR, '');
	return found.sort();
}

const ALL_ROUTES = discoverRoutes();
const APP_ROUTES = ALL_ROUTES.filter(
	(path) =>
		!NOT_THE_APP_SHELL.some(({ prefix }) => path === prefix || path.startsWith(`${prefix}/`))
);

test.describe('reflow at 320px (WCAG 1.4.10)', () => {
	// The enumeration is the whole point of this file, so it gets its own
	// assertion: a walk that silently returned nothing would make every test
	// below disappear rather than fail, and the suite would stay green with no
	// coverage at all.
	test('the route walk found the app surface', () => {
		expect(ALL_ROUTES.length).toBeGreaterThan(40);
		expect(APP_ROUTES).toContain('/');
		expect(APP_ROUTES).toContain('/organization');
		expect(APP_ROUTES).not.toContain('/login');
		// An exclusion for a route that no longer exists is dead weight that
		// hides the next person's mistake — each prefix must still name a real
		// page.
		for (const { prefix } of NOT_THE_APP_SHELL) {
			expect(
				ALL_ROUTES.some((p) => p === prefix || p.startsWith(`${prefix}/`)),
				`exclusion "${prefix}" no longer matches any route`
			).toBe(true);
		}
	});

	for (const path of APP_ROUTES) {
		test(`${path} does not scroll the document sideways`, async ({ page }) => {
			await page.setViewportSize({ width: 320, height: 720 });
			await page.goto(path);

			// Readiness, in the two steps the rest of the a11y suite uses: the
			// sidebar proves the authenticated shell hydrated, the page's own
			// <h1> proves the ROUTE rendered rather than a loading frame. No
			// `networkidle` and no sleep — see frontend/CLAUDE.md.
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			await expect(page).not.toHaveURL(/\/login/);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow, `horizontal page overflow on ${path} at 320px`).toBeLessThanOrEqual(1);

			// …and whatever scrolls instead of the document must be reachable by
			// keyboard. One rule, not the full tag set — the full scan is
			// `axe.spec.ts`'s job; this visit exists for the width.
			const { violations } = await new AxeBuilder({ page })
				.withRules(['scrollable-region-focusable'])
				.analyze();
			expect(
				violations,
				`keyboard-unreachable scroll region on ${path} at 320px:\n${formatViolations(violations)}`
			).toEqual([]);
		});
	}
});
