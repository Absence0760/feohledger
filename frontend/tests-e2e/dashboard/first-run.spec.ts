import type { Page } from '@playwright/test';
import { expectNoA11yViolations } from '../a11y/axe-helper';
import { API_BASE, expect, signInAndWait, tenantPsql, test } from '../fixtures/helpers';
import { emptyTenantDashboardResponse, onboardingProgress } from './fixture';

/**
 * The dashboard's first run (#635, `docs/decisions.md` §315).
 *
 * - An ADMIN on a workspace with no invoices gets a setup checklist instead of
 *   the lone "Go to invoices" button: the steps the `start-admin` guide
 *   recommends, each ticked only from a fact the server holds, uploading the
 *   first invoice last.
 * - Every other role on an empty workspace gets an empty state fitted to what
 *   it can do — one that cannot add invoices is not handed a button into a
 *   page whose upload would refuse it.
 * - Emptiness is the WORKSPACE's. A subsidiary with nothing in it yet, on a
 *   workspace with years of invoices, says so by name rather than welcoming an
 *   established customer as a new one.
 *
 * The empty-workspace payloads are stubbed (the e2e tenants are seeded with
 * invoices, so no real one is empty); the server's half of each — who gets
 * `onboarding`, and when — is pinned in `backend/tests/test_dashboard_onboarding.py`.
 * The retirement and the entity case run against the real backend.
 */

const isDashboard = (url: URL) => url.pathname === '/api/dashboard';

async function stubDashboard(page: Page, body: unknown) {
	await page.route(isDashboard, (route) => route.fulfill({ json: body }));
}

const STEPS = [
	['company', 'Add your company details', '/organization?section=company'],
	['users', 'Invite your team', '/admin'],
	['security', 'Require two-factor sign-in', '/organization?section=security'],
	['erp', 'Connect your accounting system', '/organization?section=erp'],
	['vendors', 'Add your vendors', '/vendors'],
	['workflow', 'Set up your approval workflow', '/workflows'],
	['first_invoice', 'Upload your first invoice', '/invoices']
] as const;

test.describe('dashboard first run — admin', () => {
	test('an empty workspace shows the setup checklist, ticked from the server', async ({ page }) => {
		await stubDashboard(
			page,
			emptyTenantDashboardResponse(onboardingProgress({ company: true, users: true }))
		);
		await page.goto('/');

		const setup = page.getByTestId('dashboard-setup');
		await expect(setup.getByRole('heading', { name: 'Set up your workspace' })).toBeVisible();

		// Seven steps, in the guide's order, the first invoice last.
		const items = setup.getByTestId('dashboard-setup-steps').locator('li');
		await expect(items).toHaveCount(STEPS.length);
		for (const [i, [id, label, href]] of STEPS.entries()) {
			const item = items.nth(i);
			await expect(item).toHaveAttribute('data-step', id);
			await expect(item.getByRole('link', { name: label })).toHaveAttribute('href', href);
		}

		// Done is the server's word, in text as well as the mark (WCAG 1.4.1).
		for (const id of ['company', 'users']) {
			const item = setup.locator(`li[data-step="${id}"]`);
			await expect(item).toHaveAttribute('data-done', 'true');
			await expect(item.getByText('Done', { exact: true })).toBeVisible();
		}
		for (const id of ['security', 'erp', 'vendors', 'workflow', 'first_invoice']) {
			const item = setup.locator(`li[data-step="${id}"]`);
			await expect(item).toHaveAttribute('data-done', 'false');
			await expect(item.getByText('Done', { exact: true })).toHaveCount(0);
		}
		await expect(page.getByTestId('dashboard-setup-progress')).toHaveText('2 of 7 done');
		await expect(setup.getByRole('link', { name: 'Read the setup guide' })).toHaveAttribute(
			'href',
			'/help/guides/start-admin'
		);

		// The checklist REPLACES the KPI row and the plain empty state — one
		// first-run block, not three. (Absence asserted after the positive one.)
		await expect(page.locator('.kpi-row')).toHaveCount(0);
		await expect(page.getByTestId('dashboard-empty-state')).toHaveCount(0);
	});

	test('a step opens the panel that owns it', async ({ page }) => {
		await stubDashboard(page, emptyTenantDashboardResponse(onboardingProgress()));
		await page.goto('/');

		await page.getByRole('link', { name: 'Connect your accounting system' }).click();

		await expect(page).toHaveURL(/\/organization\?section=erp$/);
		await expect(page.locator('[data-section-link="erp"]')).toHaveAttribute('aria-current', 'page');
	});

	test('the checklist retires once the workspace has invoices (real backend)', async ({ page }) => {
		// The seeded e2e tenant has invoices: the server sends no `onboarding`,
		// and the dashboard is the ordinary one.
		const res = page.waitForResponse((r) => isDashboard(new URL(r.url())));
		await page.goto('/');
		const body = await (await res).json();
		expect(body.organization_has_invoices).toBe(true);
		expect(body.onboarding).toBeNull();

		await expect(page.locator('.kpi-row .kpi[data-kpi-state="value"]').first()).toBeVisible();
		await expect(page.getByTestId('dashboard-setup')).toHaveCount(0);
		await expect(page.getByTestId('dashboard-empty-state')).toHaveCount(0);
	});

	for (const colorScheme of ['dark', 'light'] as const) {
		test(`the checklist has no axe violations and reflows on a phone (${colorScheme})`, async ({
			page
		}) => {
			await page.emulateMedia({ colorScheme });
			await stubDashboard(
				page,
				emptyTenantDashboardResponse(onboardingProgress({ company: true, vendors: true }))
			);
			await page.goto('/');
			await expect(page.getByTestId('dashboard-setup-steps')).toBeVisible();
			await expectNoA11yViolations(page);

			await page.setViewportSize({ width: 390, height: 844 });
			await expect(page.getByTestId('dashboard-setup-steps')).toBeVisible();
			await expectNoA11yViolations(page);
			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - document.documentElement.clientWidth
			);
			expect(overflow).toBeLessThanOrEqual(0);
		});

		test(`the populated dashboard has no axe violations (${colorScheme})`, async ({ page }) => {
			await page.emulateMedia({ colorScheme });
			await page.goto('/');
			await expect(page.locator('.pipeline-bar').first()).toBeVisible();
			await expectNoA11yViolations(page);
		});
	}
});

test.describe('dashboard first run — other roles', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('an AP clerk is sent to add the first invoice', async ({ page, tenantClerk }) => {
		await stubDashboard(page, emptyTenantDashboardResponse());
		await signInAndWait(page, tenantClerk);

		const empty = page.getByTestId('dashboard-empty-state');
		await expect(empty.getByRole('link', { name: 'Go to invoices' })).toHaveAttribute(
			'href',
			'/invoices'
		);
		await expect(page.getByTestId('dashboard-setup')).toHaveCount(0);
	});

	test('a role that cannot add invoices is told what will appear, with no button', async ({
		page,
		tenantClerk
	}) => {
		// Every SYSTEM role may enter invoices (`INVOICE_ENTRY_ROLES`), so the
		// reader this branch is for holds only a custom role. The account is
		// the clerk's with its roles swapped for one, which is the whole of what
		// the page reads to decide.
		await page.route(
			(url) => url.pathname === '/api/auth/me',
			async (route) => {
				const res = await route.fetch();
				const body = await res.json();
				await route.fulfill({ response: res, json: { ...body, roles: ['auditor'] } });
			}
		);
		await stubDashboard(page, emptyTenantDashboardResponse());
		await signInAndWait(page, tenantClerk);

		const empty = page.getByTestId('dashboard-empty-state');
		// `EmptyState` sets its heading as a styled paragraph, not a heading role.
		await expect(empty.getByText('Nothing to report yet', { exact: true })).toBeVisible();
		await expect(empty.getByRole('link')).toHaveCount(0);
		await expect(page.getByTestId('dashboard-setup')).toHaveCount(0);
	});
});

/** Slug prefix for the subsidiary the entity test creates, and cleans up. */
const ENTITY_PREFIX = 'e2e-dashempty';

test.describe('dashboard — an empty entity on an established workspace', () => {
	test.afterEach(() => {
		tenantPsql(`DELETE FROM entities WHERE slug LIKE '${ENTITY_PREFIX}-%'`);
	});

	test('names the entity instead of welcoming a new workspace (real backend)', async ({ page }) => {
		const suffix = Date.now().toString(36);
		const name = `Northwind Sub ${suffix}`;

		await page.goto('/');
		await page.evaluate(
			async ({ api, name, slug }) => {
				const res = await fetch(`${api}/api/entities`, {
					method: 'POST',
					headers: {
						'Content-Type': 'application/json',
						Authorization: `Bearer ${localStorage.getItem('auth_token')}`,
						'X-Tenant-Slug': window.location.hostname.split('.')[0]
					},
					body: JSON.stringify({ name, slug })
				});
				if (!res.ok) throw new Error(`create entity failed: ${res.status}`);
			},
			{ api: API_BASE, name, slug: `${ENTITY_PREFIX}-${suffix}` }
		);
		await page.reload();

		// Selecting it reloads the page scoped to it.
		await page.locator('.entity-btn').click();
		await page.locator('.entity-option', { hasText: name }).click();
		await expect(page.locator('.entity-name')).toHaveText(name);

		const empty = page.getByTestId('dashboard-entity-empty');
		await expect(empty.getByText(`No invoices for ${name} yet`, { exact: true })).toBeVisible();
		// The scoped figures are true zeros and stay on screen; what is gone is
		// the brand-new-workspace welcome, and the checklist that goes with it.
		await expect(page.locator('.kpi-row .kpi[data-kpi-state="value"]').first()).toBeVisible();
		await expect(page.getByTestId('dashboard-empty-state')).toHaveCount(0);
		await expect(page.getByTestId('dashboard-setup')).toHaveCount(0);

		// Accessible in both themes, at desktop and phone width — the sidebar's
		// entity switcher included, with an entity selected. Its "Entity" hint
		// once measured 3.41:1 here in the dark theme: the button animated its
		// background across the theme change and the scan landed mid-fade
		// (`layout/EntitySwitcher.svelte`, #635).
		// Desktop first: a phone width collapses the sidebar to its rail, which
		// has no hint to measure, and widening again does not re-expand it.
		for (const colorScheme of ['dark', 'light'] as const) {
			await page.emulateMedia({ colorScheme });
			await expect(page.locator('.entity-hint')).toBeVisible();
			await expectNoA11yViolations(page);
		}
		await page.setViewportSize({ width: 390, height: 844 });
		for (const colorScheme of ['dark', 'light'] as const) {
			await page.emulateMedia({ colorScheme });
			await expect(empty).toBeVisible();
			await expectNoA11yViolations(page);
		}
		await page.setViewportSize({ width: 1280, height: 720 });

		// The heading never stands in for the name. Hold the entity list back
		// so the dashboard's figures land first: the block under the KPI row
		// stays in its pending state rather than reading "this entity" and
		// then swapping the real name in (#635).
		let release!: () => void;
		const listHeld = new Promise<void>((resolve) => (release = resolve));
		const isEntityList = (url: URL) => url.pathname === '/api/entities';
		await page.route(isEntityList, async (route) => {
			await listHeld;
			await route.fallback();
		});
		await page.reload();
		await expect(page.locator('.kpi-row .kpi[data-kpi-state="value"]').first()).toBeVisible();
		await expect(page.locator('p.loading')).toBeVisible();
		await expect(empty).toHaveCount(0);
		await expect(page.getByText('No invoices for this entity yet')).toHaveCount(0);
		release();
		await expect(empty.getByText(`No invoices for ${name} yet`, { exact: true })).toBeVisible();
		await page.unroute(isEntityList);

		// Back to the consolidated view, so nothing else in this worker runs
		// scoped to an entity this test is about to delete.
		await page.locator('.entity-btn').click();
		await page.locator('.entity-option', { hasText: /^All entities/ }).click();
		await expect(page.locator('.entity-name')).toHaveText('All entities');
	});
});
