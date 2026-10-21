import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	deleteVendorsWhere,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';
import type { Page } from '@playwright/test';

/**
 * /vendors/screening — sanctions-screening REVIEW QUEUE.
 *
 * The queue lists vendors the screening engine flagged `match` / `review`
 * awaiting a human decision (`GET /api/vendors/screening/review-queue`).
 * The reviewer can open a vendor's screening history and block / unblock its
 * payments — but block/unblock is gated on the GRANULAR permission
 * `vendor.block` via the auth store, NOT a role check. This spec proves:
 *
 *   1. A flagged vendor appears on the queue with its screening pill.
 *   2. Opening a queue row shows the screening-history timeline.
 *   3. A permission HOLDER (admin) sees the Block-payments control.
 *   4. A NON-holder (cfo — can read the queue, lacks `vendor.block`) does NOT
 *      see the block/unblock control; the backend enforces it regardless.
 *
 * Setup flips a freshly-created vendor's screening_status straight in the
 * tenant DB (the mock screener only flags a fixed fixture-name set; a direct
 * UPDATE keeps the vendor name unique + avoids cross-run collisions) and
 * seeds one `sanctions_checks` history row.
 */

let H: Record<string, string>;
let SLUG: string;

function slugFromPage(page: Page): string {
	return new URL(page.url()).hostname.split('.')[0];
}

async function createVendor(page: Page, name: string): Promise<{ id: string }> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: H,
		data: { name }
	});
	expect(resp.status(), `create vendor ${name}`).toBe(201);
	return (await resp.json()) as { id: string };
}

/** Flip a vendor to a `match` screening verdict + seed one history row so it
 *  lands on the review queue with a visible history timeline. */
function flagForReview(vendorId: string): void {
	tenantPsql(
		`UPDATE vendors SET screening_status='match', last_screened_at=now(), ` +
			`risk_level='high', risk_score=90 WHERE id='${vendorId}'`,
		SLUG
	);
	tenantPsql(
		`INSERT INTO sanctions_checks ` +
			`(id, vendor_id, organization_id, provider, check_type, result, ` +
			`risk_score, matched_list, checked_at) ` +
			`SELECT gen_random_uuid(), v.id, v.organization_id, 'mock', 'manual', ` +
			`'match', 90, 'MOCK_TEST_SDN', now() ` +
			`FROM vendors v WHERE v.id='${vendorId}'`,
		SLUG
	);
}

function deleteVendorCascade(vendorId: string): void {
	try {
		deleteVendorsWhere(`id='${vendorId}'`, SLUG);
	} catch {
		/* best-effort */
	}
}

test.describe('/vendors/screening review queue (admin — cached session)', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendors');
		SLUG = slugFromPage(page);
		H = await authedTenantHeaders(page, SLUG);
	});

	test('a flagged vendor shows on the queue with its screening pill', async ({ page }) => {
		const name = `Screen-Queue Co ${Date.now()}`;
		const vendor = await createVendor(page, name);
		try {
			flagForReview(vendor.id);

			await page.goto('/vendors/screening');

			await expect(page.getByRole('columnheader', { name: 'Vendor' })).toBeVisible();
			// KPI summary renders the match tally.
			await expect(page.getByText('Sanctions matches')).toBeVisible();

			const row = page.locator('table tbody tr', { hasText: name });
			await expect(row).toBeVisible();
			// The row carries at least one screening pill (Match / High risk).
			await expect(row.locator('.screen-badge').first()).toBeVisible();
		} finally {
			deleteVendorCascade(vendor.id);
		}
	});

	test('opening a queue row shows the screening-history timeline', async ({ page }) => {
		const name = `Screen-History Co ${Date.now()}`;
		const vendor = await createVendor(page, name);
		try {
			flagForReview(vendor.id);

			await page.goto('/vendors/screening');

			const row = page.locator('table tbody tr', { hasText: name });
			await row.locator('.row-link').click();

			const modal = page.getByRole('dialog', { name: 'Vendor screening review' });
			await expect(modal).toBeVisible();
			await expect(modal.getByRole('heading', { name: 'Screening history' })).toBeVisible();
			// The seeded sanctions_checks row surfaces its matched-list name in the
			// history timeline. Scope to `ul.history` — the summary "Matched list"
			// field above renders the same matched-list name, so an unscoped
			// getByText resolves to two legitimate elements (strict-mode violation).
			await expect(modal.locator('ul.history').getByText('MOCK_TEST_SDN')).toBeVisible();
		} finally {
			deleteVendorCascade(vendor.id);
		}
	});

	test('a vendor.block HOLDER (admin) sees the Block-payments control', async ({ page }) => {
		const name = `Screen-Block Co ${Date.now()}`;
		const vendor = await createVendor(page, name);
		try {
			flagForReview(vendor.id);

			await page.goto('/vendors/screening');

			const row = page.locator('table tbody tr', { hasText: name });
			await row.locator('.row-link').click();

			const modal = page.getByRole('dialog', { name: 'Vendor screening review' });
			await expect(modal).toBeVisible();
			// Admin holds vendor.block AND is a manager → both controls present.
			await expect(modal.getByRole('button', { name: 'Block payments' })).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Re-screen now' })).toBeVisible();
		} finally {
			deleteVendorCascade(vendor.id);
		}
	});

	// The shared limiter's 429 is a CODED refusal (`backend/app/services/
	// rate_limit.py`): its `detail` is `{code, message, params}`. The toast used
	// to be built from `e.detail ?? e.message`, so it printed "[object Object]";
	// it now reads `ApiError.message`, which the transport already localized
	// (`utils/apiError.ts::errorMessage`). Stubbed: tripping the real limiter
	// would throttle every other spec on the tenant, and the body is exactly
	// what the limiter serializes (pinned in backend/tests/test_rate_limit_security.py).
	test('a rate-limited re-screen is toasted in German, never "[object Object]"', async ({
		page
	}) => {
		const name = `Screen-Throttle Co ${Date.now()}`;
		const vendor = await createVendor(page, name);
		try {
			flagForReview(vendor.id);
			const screenPath = `/api/vendors/${vendor.id}/screen`;
			let refused = false;
			await page.route(
				(url) => url.pathname === screenPath,
				async (route) => {
					if (route.request().method() !== 'POST') return route.fallback();
					refused = true;
					await route.fulfill({
						status: 429,
						headers: { 'Retry-After': '45' },
						json: {
							detail: {
								code: 'rate_limited',
								message: 'Too many requests. Please wait 45 seconds before trying again.',
								params: { retry_after_seconds: 45 }
							}
						}
					});
				}
			);
			await page.addInitScript(() => {
				localStorage.setItem('feoh_locale', 'de');
			});

			await page.goto('/vendors/screening');
			const row = page.locator('table tbody tr', { hasText: name });
			await row.locator('.row-link').click();
			const modal = page.getByRole('dialog', { name: 'Lieferanten-Screening-Prüfung' });
			await modal.getByRole('button', { name: 'Jetzt erneut prüfen' }).click();

			await expect.poll(() => refused).toBe(true);
			const toast = page.locator('.toast.error .toast-text');
			await expect(toast).toHaveText(
				'Zu viele Versuche. Warten Sie 45 Sekunden und versuchen Sie es dann erneut.'
			);
			await expect(page.locator('body')).not.toContainText('[object Object]');
			await expect(page.locator('body')).not.toContainText('Too many requests');
		} finally {
			deleteVendorCascade(vendor.id);
		}
	});
});

test.describe('/vendors/screening review queue (cfo — non-holder)', () => {
	// cfo can READ the review queue but does NOT hold `vendor.block` (nor is a
	// manager), so the block/unblock + re-screen controls must be hidden.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a NON-holder (cfo) can view the queue but not block/unblock', async ({
		page,
		tenantAdmin,
		tenantCfo
	}) => {
		SLUG = currentTenantSlug();
		// Seed the flagged vendor with an ADMIN token (create is `vendor.manage`,
		// which the cfo lacks) via a direct login-API call — no UI, no storage
		// state. Only the safe screening flip runs through psql afterwards.
		const login = await page.request.post(`${API_BASE}/api/auth/login`, {
			headers: { 'X-Tenant-Slug': SLUG },
			data: { email: tenantAdmin.email, password: tenantAdmin.password }
		});
		expect(login.status(), 'admin login for seeding').toBe(200);
		const adminToken = ((await login.json()) as { access_token: string }).access_token;
		const adminH = { Authorization: `Bearer ${adminToken}`, 'X-Tenant-Slug': SLUG };

		const name = `Screen-NoPerm Co ${Date.now()}`;
		const create = await page.request.post(`${API_BASE}/api/vendors`, {
			headers: adminH,
			data: { name }
		});
		expect(create.status(), 'seed vendor as admin').toBe(201);
		const vendorId = ((await create.json()) as { id: string }).id;
		try {
			flagForReview(vendorId);

			// Now sign in as the cfo in the browser for the UI assertions.
			await signInAndWait(page, tenantCfo);

			await page.goto('/vendors/screening');

			const row = page.locator('table tbody tr', { hasText: name });
			await expect(row).toBeVisible();
			await row.locator('.row-link').click();

			const modal = page.getByRole('dialog', { name: 'Vendor screening review' });
			await expect(modal).toBeVisible();
			// History is still readable by the cfo…
			await expect(modal.getByRole('heading', { name: 'Screening history' })).toBeVisible();
			// …but the permission-gated controls are absent.
			await expect(modal.getByRole('button', { name: 'Block payments' })).toHaveCount(0);
			await expect(modal.getByRole('button', { name: 'Unblock payments' })).toHaveCount(0);
			await expect(modal.getByRole('button', { name: 'Re-screen now' })).toHaveCount(0);
			await expect(modal.getByText(/don't have permission/i)).toBeVisible();
		} finally {
			deleteVendorCascade(vendorId);
		}
	});
});
