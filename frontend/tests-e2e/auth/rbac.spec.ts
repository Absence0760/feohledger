import type { Page } from '@playwright/test';

import { NAV, sidebarHrefs, visibleChildren, type NavGroup } from '$lib/nav';

import {
	API_BASE,
	authedTenantHeaders,
	expect,
	sectionTabHrefs,
	signInAndWait,
	test
} from '../fixtures/helpers';

/**
 * RBAC — the sidebar and the section tabs render what `nav.ts` says, per role.
 *
 * The primary nav (`frontend/src/lib/nav.ts`) keeps high-traffic destinations as
 * top-level links and folds the rest into groups. The sidebar shows ONE row per
 * group, pointing at the first child the role can see; each grouped page then
 * renders the group's visible children as a section sub-tab bar.
 *
 * ## What this spec does NOT restate
 *
 * Which role may see which row is POLICY, and it is pinned exactly once: the
 * literal `NAV_GATES` table in `src/lib/nav.test.ts`, which `pnpm test:unit`
 * runs before anything reaches CI. This spec used to carry its own hand-typed
 * copy of every role's sidebar and tab set — and a one-row nav change in round
 * 31 updated the unit copy, missed this one, and turned an unrelated PR's
 * shard red, because this copy is only ever checked in CI.
 *
 * So the expected sets here are COMPUTED by the same functions the components
 * call (`sidebarHrefs`, `visibleChildren`), applied to the signed-in user's
 * real roles and permissions as `/api/auth/me` reports them. That proves the
 * wiring — the sidebar and the tab bar render the policy for the identity the
 * app actually holds — without re-typing the answer. The one literal kept is
 * each fixture's role, because a comparison against a computed set is only
 * meaningful if the identity really is the role the test is named for.
 *
 * Login budget: the backend caps logins at 10/60s per IP and every e2e login
 * shares localhost, so we sign in ONCE per non-admin role and cover admin on
 * the cached default storage-state — no extra fresh login.
 */

interface Identity {
	roles: string[];
	permissions: string[];
}

async function identity(page: Page): Promise<Identity> {
	const res = await page.request.get(`${API_BASE}/api/auth/me`, {
		headers: await authedTenantHeaders(page)
	});
	expect(res.ok(), `GET /api/auth/me → ${res.status()}`).toBe(true);
	const me = (await res.json()) as Partial<Identity>;
	return { roles: me.roles ?? [], permissions: me.permissions ?? [] };
}

async function renderedSidebar(page: Page): Promise<string[]> {
	const links = page.locator('aside.sidebar a.nav-item');
	return links.evaluateAll((els) =>
		els.map((e) => (e as HTMLAnchorElement).getAttribute('href') ?? '')
	);
}

/**
 * Assert the whole nav for one signed-in identity: the sidebar rows, then the
 * section tabs on every group that identity can reach. Every visible group is
 * visited — not a hand-picked sample — so a group added later is covered the
 * day it lands.
 */
async function expectNavMatchesPolicy(page: Page, expectedRoles: string[]) {
	const me = await identity(page);
	expect([...me.roles].sort(), 'the fixture signed in as the role this test is named for').toEqual(
		[...expectedRoles].sort()
	);
	const has = (...roles: string[]) => roles.some((r) => me.roles.includes(r));
	const can = (perm: string) => me.permissions.includes(perm);

	await page.goto('/');
	await expect(page.locator('aside.sidebar a.nav-item').first()).toBeVisible();
	expect(await renderedSidebar(page), 'sidebar rows, in nav order').toEqual(
		sidebarHrefs(has, can)
	);

	const groups = NAV.filter((e): e is NavGroup => e.kind === 'group');
	for (const group of groups) {
		const tabs = visibleChildren(group, has, can).map((c) => c.href);
		if (tabs.length === 0) continue; // no row, nothing to land on
		await page.goto(tabs[0]);
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		if (tabs.length > 1) {
			// The row PLUS the More menu (`sectionTabHrefs`). Compared as sets:
			// which tabs fold into More depends on the viewport, not the policy.
			expect((await sectionTabHrefs(page)).sort(), `${group.label} section tabs`).toEqual(
				[...tabs].sort()
			);
		} else {
			// A lone destination is not a tab bar — `SectionTabs` renders none.
			await expect(page.locator('.section-tabs'), `${group.label} section tabs`).toHaveCount(0);
		}
	}
}

test.describe('RBAC — non-admin roles (one fresh sign-in each)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('clerk: sidebar and every section bar match the nav policy', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await expectNavMatchesPolicy(page, ['ap_clerk']);
	});

	test('manager: sidebar and every section bar match the nav policy', async ({
		page,
		tenantManager
	}) => {
		await signInAndWait(page, tenantManager);
		await expectNavMatchesPolicy(page, ['ap_manager']);
	});

	test('cfo: sidebar and every section bar match the nav policy', async ({
		page,
		tenantCfo
	}) => {
		await signInAndWait(page, tenantCfo);
		await expectNavMatchesPolicy(page, ['cfo']);
	});
});

test.describe('RBAC — admin (cached session, no extra login)', () => {
	test('admin: sidebar and every section bar match the nav policy', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('aside.sidebar')).toBeVisible();
		await expectNavMatchesPolicy(page, ['admin']);
	});

	test('a top-level route renders no section bar', async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		await expect(page.locator('.section-tabs')).toHaveCount(0);
	});
});
