import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import AxeBuilder from '@axe-core/playwright';

import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';
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

/**
 * Every `+page.svelte` directory under `src/routes`, as a URL path — split
 * into the static routes and the route PATTERNS with a dynamic segment
 * (`/workflows/[id]`), which have no URL until a record is named.
 */
function discoverRoutes(): { fixed: string[]; dynamic: string[] } {
	const fixed: string[] = [];
	const dynamic: string[] = [];
	const walk = (dir: string, urlPath: string) => {
		const entries = readdirSync(dir, { withFileTypes: true });
		if (entries.some((e) => e.isFile() && e.name === '+page.svelte')) {
			(urlPath.includes('[') ? dynamic : fixed).push(urlPath === '' ? '/' : urlPath);
		}
		for (const entry of entries) {
			if (!entry.isDirectory()) continue;
			walk(join(dir, entry.name), `${urlPath}/${entry.name}`);
		}
	};
	walk(ROUTES_DIR, '');
	return { fixed: fixed.sort(), dynamic: dynamic.sort() };
}

const isAppShell = (path: string) =>
	!NOT_THE_APP_SHELL.some(({ prefix }) => path === prefix || path.startsWith(`${prefix}/`));

const DISCOVERED = discoverRoutes();
const ALL_ROUTES = DISCOVERED.fixed;
const APP_ROUTES = ALL_ROUTES.filter(isAppShell);
const DYNAMIC_APP_ROUTES = DISCOVERED.dynamic.filter(isAppShell);

/**
 * ## And the routes with an id in the path
 *
 * The walk above cannot visit `/workflows/[id]` without naming a record, and
 * it used to skip every dynamic segment outright — so the workflow editor
 * scrolled the document 142px sideways at 320px (one unwrappable step row in
 * the canvas set the editor's minimum width) with nothing here to say so.
 *
 * Each dynamic route in the app shell now needs an entry: how to find a real
 * record to open, and the element that proves the page drew it. The walk
 * test below fails when a new dynamic route lands without one, the same way
 * a new static route is measured the day it exists.
 */
const DYNAMIC_ROUTE_SAMPLES: Record<
	string,
	{
		resolve: (page: import('@playwright/test').Page) => Promise<string>;
		ready: (page: import('@playwright/test').Page) => import('@playwright/test').Locator;
	}
> = {
	'/workflows/[id]': {
		// Workflow ids are generated, not seeded constants: ask the API for the
		// worker tenant's default (it has steps, so the canvas has rows to lay
		// out). The token lives in the app origin's localStorage, so the list
		// page is opened first.
		resolve: async (page) => {
			await page.goto('/workflows');
			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			const resp = await page.request.get(`${API_BASE}/api/workflows`, {
				headers: await authedTenantHeaders(page)
			});
			expect(resp.status()).toBe(200);
			const { items } = (await resp.json()) as {
				items: { id: string; is_default: boolean }[];
			};
			const wf = items.find((w) => w.is_default) ?? items[0];
			expect(wf, 'the worker tenant has no workflow to open').toBeTruthy();
			return `/workflows/${wf.id}`;
		},
		// The step rows are what overflowed; measure once they are drawn.
		ready: (page) => page.locator('.canvas .node').first()
	},
	'/help/guides/[id]': {
		// A guide id is content, not data; this one carries a diagram, the
		// widest thing a guide renders.
		resolve: async () => '/help/guides/run-payments',
		ready: (page) => page.getByRole('heading', { level: 1, name: 'Run a payment run' })
	}
};

/**
 * ## And why `/organization`'s panels are read off disk too
 *
 * A route is normally one page, so one path is one measurement. `/organization`
 * is the exception: it shows ONE of ~16 settings panels at a time, chosen by
 * `?section=<slug>`, so visiting `/organization` measures the Getting-started
 * card and leaves every form on the route unmeasured — on the very page this
 * file's header cites for a measured 137px overflow.
 *
 * So the slugs come off disk the same way the routes do, from the page's own
 * `SECTION_GROUPS`. A hand-kept list here would rot exactly like the five-path
 * list this spec replaced; a panel added to the page is measured at 320px the
 * day it exists, with nothing to remember.
 */
const ORG_PAGE = fileURLToPath(
	new URL('../../src/routes/organization/+page.svelte', import.meta.url)
);

function discoverOrgSections(): string[] {
	const source = readFileSync(ORG_PAGE, 'utf8');
	// `SECTION_GROUPS` is the only place in this file that writes `slug: '…'`,
	// and it is a plain literal array — no template strings, no computation —
	// precisely so it stays readable as data from both sides.
	const start = source.indexOf('const SECTION_GROUPS');
	const end = source.indexOf('const SECTION_SLUGS');
	if (start < 0 || end < 0 || end < start) return [];
	return [...source.slice(start, end).matchAll(/slug:\s*'([a-z0-9-]+)'/g)].map((m) => m[1]);
}

const ORG_SECTIONS = discoverOrgSections();

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
		// Every dynamic route the app shell serves names a sample to open, and
		// every sample still names a route that exists.
		expect(DYNAMIC_APP_ROUTES).toContain('/workflows/[id]');
		expect(Object.keys(DYNAMIC_ROUTE_SAMPLES).sort()).toEqual(DYNAMIC_APP_ROUTES);
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

test.describe('reflow at 320px — routes with an id in the path (WCAG 1.4.10)', () => {
	for (const [pattern, sample] of Object.entries(DYNAMIC_ROUTE_SAMPLES)) {
		test(`${pattern} does not scroll the document sideways`, async ({ page }) => {
			await page.setViewportSize({ width: 320, height: 720 });
			const path = await sample.resolve(page);
			await page.goto(path);

			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			await expect(page).not.toHaveURL(/\/login/);
			await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
			await expect(sample.ready(page)).toBeVisible();

			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow, `horizontal page overflow on ${path} at 320px`).toBeLessThanOrEqual(1);

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

test.describe("reflow at 320px — /organization's panels (WCAG 1.4.10)", () => {
	test('the panel walk found the settings sections', () => {
		// Same reasoning as the route walk: a regex that stopped matching would
		// delete every test below rather than fail one.
		expect(ORG_SECTIONS.length).toBeGreaterThan(10);
		expect(ORG_SECTIONS).toContain('getting-started');
		expect(ORG_SECTIONS).toContain('payments');
	});

	for (const slug of ORG_SECTIONS) {
		test(`/organization?section=${slug} does not scroll the document sideways`, async ({
			page
		}) => {
			await page.setViewportSize({ width: 320, height: 720 });
			await page.goto(`/organization?section=${slug}`);

			await expect(page.locator('aside.sidebar').first()).toBeVisible();
			await expect(page).not.toHaveURL(/\/login/);

			// Readiness, panel-shaped. The route's <h1> renders before
			// `GET /api/organization` lands and every panel is behind that gate, so
			// the <h1> alone would measure an empty column — the panel's own <h2>
			// is the signal that there is something to measure. Exactly one panel
			// renders at a time, hence exactly one <h2> in the stack.
			await expect(page.locator('fieldset.sections h2')).toBeVisible();
			// …and it is the RIGHT panel: a typo'd slug falls back to
			// Getting-started, which would measure a narrow card and pass while
			// covering nothing. `aria-current` is an attribute, so this reads fine
			// through the collapsed (display:none) rail at this width.
			await expect(page.locator(`[data-section-link="${slug}"]`)).toHaveAttribute(
				'aria-current',
				'page'
			);

			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(
				overflow,
				`horizontal page overflow on /organization?section=${slug} at 320px`
			).toBeLessThanOrEqual(1);
		});
	}
});
