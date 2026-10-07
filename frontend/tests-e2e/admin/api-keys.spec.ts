import type { Route } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	controlPsql,
	currentTenantSlug,
	expect,
	signInAndWait,
	TENANT_ROOT_URL,
	test
} from '../fixtures/helpers';

/**
 * /admin/api-keys — Developer-API key management (admin only).
 *
 * Surfaces the existing backend endpoints (`backend/app/api/api_keys.py`):
 *  - POST   /api/api-keys           → mint (plaintext key returned EXACTLY once)
 *  - GET    /api/api-keys           → list (prefix + metadata only)
 *  - DELETE /api/api-keys/{id}      → soft-revoke (idempotent)
 *  - GET    /api/api-keys/{id}/usage → per-key totals + per-day breakdown
 *
 * The plaintext key is shown once in a copy-able reveal and never echoed after.
 * Login model mirrors the suite: the per-worker storage state signs the admin
 * in (the only role the endpoints allow), so the page loads without a redirect.
 */

async function apiHeaders(page: import('@playwright/test').Page) {
	return {
		...(await authedTenantHeaders(page)),
		'Content-Type': 'application/json'
	};
}

interface ApiKeyResponse {
	id: string;
	name: string;
	key_prefix: string;
	scopes: string[];
	revoked_at: string | null;
}

/** Best-effort cleanup: revoke any key we minted in a test. */
async function revoke(page: import('@playwright/test').Page, id: string) {
	const headers = await apiHeaders(page);
	await page.request.delete(`${API_BASE}/api/api-keys/${id}`, { headers });
}

/**
 * Remove a minted key from the control plane entirely.
 *
 * `DELETE /api/api-keys/{id}` is a SOFT revoke by design — the row survives so
 * the key's history stays auditable — which means revoking is not teardown.
 * Every run of this file was leaving two or three permanently-`Revoked` rows in
 * a control plane shared by every tenant, and they render on `/admin/api-keys`
 * forever; 20 of them had accumulated on this machine. `api_key_usage` is
 * `ON DELETE CASCADE`, so one statement clears the meter rows with it.
 *
 * The predicate is the prefix AND the worker's own organization, which is what
 * makes it both self-healing (a run clears what earlier runs stranded) and safe
 * under parallel workers. A bare `name LIKE 'e2e-%'` sweep — the shape used in
 * a per-worker TENANT database, where it can only reach that worker's rows —
 * would here delete a concurrently-running worker's in-flight key, because the
 * control plane is shared across workers as well as tenants. The id is passed
 * so a failure names the key the caller meant.
 */
function purgeKey(id: string) {
	controlPsql(
		`DELETE FROM api_keys WHERE (id = '${id}' OR name LIKE 'e2e-%') ` +
			`AND organization_id = (SELECT id FROM organizations WHERE slug = '${currentTenantSlug()}')`
	);
}

test.describe('/admin/api-keys (admin)', () => {
	// Deterministic explicit sign-in (don't lean on the shared storage cache) so
	// the gated page is reliably authed before each test.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('mint shows the plaintext key once + lists the new key', async ({ page }) => {
		await page.goto('/admin/api-keys');
		await expect(page.getByRole('heading', { name: 'API Keys' })).toBeVisible();

		const name = `e2e-key-${Date.now()}`;

		// Open create modal + submit.
		await page.getByRole('button', { name: '+ Create key' }).click();
		const createModal = page.getByRole('dialog', { name: 'Create API key' });
		await expect(createModal).toBeVisible();
		await createModal.getByRole('textbox').first().fill(name);
		await createModal.getByRole('button', { name: 'Create' }).click();

		// The one-time reveal modal shows the FULL plaintext key (feoh_live_… / a
		// long token), warns it's shown once, and offers a Copy button.
		const reveal = page.getByRole('dialog', { name: 'API key created' });
		await expect(reveal).toBeVisible({ timeout: 10_000 });
		const minted = reveal.getByTestId('minted-key');
		await expect(minted).toBeVisible();
		const plaintext = (await minted.textContent())?.trim() ?? '';
		// The plaintext is a real, long, prefixed key — not just the stored prefix.
		expect(plaintext.length).toBeGreaterThan(12);
		expect(plaintext.startsWith('feoh_')).toBe(true);
		await expect(reveal.getByText(/shown only once/i)).toBeVisible();
		await expect(reveal.getByRole('button', { name: 'Copy' })).toBeVisible();

		// Dismiss the reveal — the plaintext must be gone (the surface never
		// re-shows it).
		await reveal.getByRole('button', { name: 'Done' }).click();
		await expect(reveal).toBeHidden();
		await expect(page.getByTestId('minted-key')).toHaveCount(0);

		// The new key is listed with its name + Active status + an Active row, but
		// NOT the plaintext (only the prefix).
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();
		await expect(row.getByText('Active')).toBeVisible();
		await expect(page.getByText(plaintext)).toHaveCount(0);

		// Cleanup via the API (resolve the id from the list).
		const headers = await apiHeaders(page);
		const list = (await (
			await page.request.get(`${API_BASE}/api/api-keys`, { headers })
		).json()) as ApiKeyResponse[];
		const created = list.find((k) => k.name === name);
		if (created) {
			await revoke(page, created.id);
			purgeKey(created.id);
		}
	});

	test('revoke disables the key (idempotent) and the row flips to Revoked', async ({ page }) => {
		const headers = await apiHeaders(page);
		const name = `e2e-revoke-${Date.now()}`;
		const created = (await (
			await page.request.post(`${API_BASE}/api/api-keys`, {
				headers,
				data: { name }
			})
		).json()) as { api_key: ApiKeyResponse; key: string };
		const id = created.api_key.id;

		await page.goto('/admin/api-keys');
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();
		await expect(row.getByText('Active')).toBeVisible();

		// Two-click armed revoke: first click arms ("Confirm"), second commits.
		// `exact` so the "View usage for e2e-revoke-…" row link (which contains
		// the substring "revoke") doesn't also match.
		await row.getByRole('button', { name: 'Revoke', exact: true }).click();
		await row.getByRole('button', { name: 'Confirm', exact: true }).click();

		// Row flips to Revoked; the Revoke action is gone for a revoked key.
		await expect(row.getByText('Revoked')).toBeVisible({ timeout: 10_000 });
		await expect(row.getByRole('button', { name: 'Revoke', exact: true })).toHaveCount(0);

		// Server-side: the key is revoked, and a repeat DELETE is idempotent (200,
		// no error).
		const after = (await (
			await page.request.get(`${API_BASE}/api/api-keys`, { headers })
		).json()) as ApiKeyResponse[];
		expect(after.find((k) => k.id === id)?.revoked_at).not.toBeNull();
		const repeat = await page.request.delete(`${API_BASE}/api/api-keys/${id}`, { headers });
		expect(repeat.ok()).toBe(true);

		// Revocation is what this test asserts, so the purge comes after the last
		// assertion that needs the row to still exist.
		purgeKey(id);
	});

	/**
	 * The METER, not just the panel.
	 *
	 * The test below only ever opens the usage view on a brand-new key, so it
	 * asserts the panel renders — it cannot tell a working meter from one that
	 * counts nothing, which is exactly what round 24's request-identity fix
	 * changed. This one drives real `/api/v1` traffic with the minted key and
	 * asserts the number the panel reports.
	 *
	 * It needs no waiting and no polling: `get_api_key_principal` upserts the
	 * `api_key_usage` row and COMMITS it on the request's own control session
	 * before the response is returned, so the count is durable the moment the
	 * last call resolves. Anything that looked like a race here would be a real
	 * defect, not something to sleep through.
	 *
	 * The meter fires on successful AUTHENTICATION, ahead of the `public_api`
	 * entitlement gate and the per-key rate limit — so a tenant whose plan does
	 * not include the public API still meters its 402s, and the assertion is on
	 * the count rather than on a 200. A 401 is the one status that means nothing
	 * was counted (the key did not authenticate), so it is named rather than
	 * left to surface as a confusing "expected 3, received 0".
	 *
	 * Every `e2eN` worker is seeded on `scale` (decisions §254 — minting this
	 * key is itself plan-gated now), so these calls come back 200; the
	 * assertion stays on the count rather than the status because the count is
	 * what this test is about, and it would hold just the same for a 402. Do
	 * NOT move the worker's org onto another plan for the duration to exercise
	 * the refusal: the subscription outlives a crashed test and would put the
	 * tenant's billing surface somewhere the billing specs do not expect. The
	 * 402 side is pinned in `backend/tests/test_public_api_keys.py` and
	 * `test_plan_feature_gates.py`.
	 */
	test('the usage panel counts the /api/v1 traffic made with the key', async ({ page }) => {
		const headers = await apiHeaders(page);
		const name = `e2e-usage-meter-${Date.now()}`;
		const created = (await (
			await page.request.post(`${API_BASE}/api/api-keys`, { headers, data: { name } })
		).json()) as { api_key: ApiKeyResponse; key: string };

		try {
			// A fresh key has never been used, so the expected total is exactly
			// what this test sends — no baseline arithmetic, nothing to drift.
			const CALLS = 3;
			const statuses: number[] = [];
			for (let i = 0; i < CALLS; i++) {
				const res = await page.request.get(`${API_BASE}/api/v1/invoices?page_size=1`, {
					headers: { 'X-API-Key': created.key }
				});
				statuses.push(res.status());
			}
			expect(
				statuses.filter((s) => s === 401),
				`the key did not authenticate, so nothing was metered (statuses: ${statuses.join(', ')})`
			).toEqual([]);

			await page.goto('/admin/api-keys');
			const row = page.locator('tr', { hasText: name });
			await expect(row).toBeVisible();
			await row.getByRole('button', { name: `View usage for ${name}` }).click();

			const usageModal = page.getByRole('dialog', { name: 'API key usage' });
			await expect(usageModal.getByTestId('usage-totals')).toBeVisible({ timeout: 10_000 });

			// Both totals: all-time, and the trailing window the panel labels.
			// Today's calls are inside any window, so the two must agree — a
			// window lagging the total would mean the day bucket landed on the
			// wrong date.
			const stat = (label: string | RegExp) =>
				usageModal.locator('.usage-stat', { hasText: label }).locator('.usage-num');
			await expect(stat('Total requests')).toHaveText(String(CALLS));
			await expect(stat(/Last \d+ days/)).toHaveText(String(CALLS));

			// The per-day breakdown is the same figure, not a second source of
			// truth: one day bucket, carrying all of them.
			const dayRows = usageModal.locator('table tbody tr');
			await expect(dayRows).toHaveCount(1);
			await expect(dayRows.first().locator('td.num-col')).toHaveText(String(CALLS));

			// "No requests yet" is a claim, and it must not be the one on screen.
			await expect(usageModal.getByText('No requests yet.')).toHaveCount(0);

			await usageModal.getByRole('button', { name: 'Close' }).click();
			await expect(usageModal).toBeHidden();
		} finally {
			await revoke(page, created.api_key.id);
			purgeKey(created.api_key.id);
		}
	});

	test('the per-key usage view renders totals + recent activity', async ({ page }) => {
		const headers = await apiHeaders(page);
		const name = `e2e-usage-${Date.now()}`;
		const created = (await (
			await page.request.post(`${API_BASE}/api/api-keys`, {
				headers,
				data: { name }
			})
		).json()) as { api_key: ApiKeyResponse };

		await page.goto('/admin/api-keys');
		const row = page.locator('tr', { hasText: name });
		await expect(row).toBeVisible();

		// Click the row's name link to open the usage modal.
		await row.getByRole('button', { name: `View usage for ${name}` }).click();

		const usageModal = page.getByRole('dialog', { name: 'API key usage' });
		await expect(usageModal).toBeVisible();
		await expect(usageModal.getByRole('heading', { name: `Usage — ${name}` })).toBeVisible();
		// Totals block renders (a brand-new key has 0 requests, but the totals
		// cards still show).
		await expect(usageModal.getByTestId('usage-totals')).toBeVisible({ timeout: 10_000 });
		await expect(usageModal.getByText('Total requests')).toBeVisible();
		await expect(usageModal.getByText(/Last 30 days/)).toBeVisible();

		await usageModal.getByRole('button', { name: 'Close' }).click();
		await expect(usageModal).toBeHidden();

		await revoke(page, created.api_key.id);
		purgeKey(created.api_key.id);
	});

	/**
	 * The panel must answer about the key it NAMES.
	 *
	 * `GET /api/api-keys/{id}/usage` is a re-issuable fetch keyed on the row
	 * clicked: open key A's usage, close it, open key B's, and A's response can
	 * still resolve afterwards. `usageSequence` (a `createRequestSequencer`) is
	 * what stops it committing — but that guard shipped with nothing driving it,
	 * and a guard nothing drives is a guard nobody notices removing.
	 *
	 * It matters here for the reason it does on `/audit` and `/experiments`: the
	 * heading comes from the CLICK and the figures come from the RESPONSE, so a
	 * mismatch renders as ordinary data. "This key has made 9,111 requests" under
	 * the wrong key's name is how a live integration gets revoked by mistake.
	 *
	 * Both responses are stubbed and their ORDER is controlled — key A's is parked
	 * on a promise released by hand. No sleeps and no inflated timeouts: the race
	 * is driven by a real readiness gate. Stubbing the list too means this test
	 * mints nothing, so it leaves no rows behind in the control plane.
	 */
	test("a late usage response cannot land under a second key's name", async ({ page }) => {
		const KEY_A = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa';
		const KEY_B = 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb';
		const NAME_A = 'usage-identity-alpha';
		const NAME_B = 'usage-identity-bravo';

		const keyList = [
			{
				id: KEY_A,
				name: NAME_A,
				key_prefix: 'feoh_live_aaaa',
				scopes: ['read'],
				created_at: '2024-01-01T00:00:00Z',
				last_used_at: '2024-06-01T00:00:00Z',
				revoked_at: null
			},
			{
				id: KEY_B,
				name: NAME_B,
				key_prefix: 'feoh_live_bbbb',
				scopes: ['read'],
				created_at: '2024-02-02T00:00:00Z',
				last_used_at: '2024-06-02T00:00:00Z',
				revoked_at: null
			}
		];

		/** Distinct totals, so which response is on screen is unambiguous. */
		const usageFor = (id: string, prefix: string, total: number, day: string) => ({
			api_key_id: id,
			key_prefix: prefix,
			total_requests: total,
			window_days: 30,
			window_requests: total,
			last_used_at: '2024-06-03T00:00:00Z',
			daily: [{ usage_date: day, request_count: total }]
		});

		/** The key id a usage request is for, else null. */
		const usageKeyId = (url: string): string | null =>
			new URL(url).pathname.match(/^\/api\/api-keys\/([^/]+)\/usage$/)?.[1] ?? null;

		let releaseA!: () => void;
		const heldA = new Promise<void>((resolve) => {
			releaseA = resolve;
		});
		let aRequested = false;

		await page.route('**/api/api-keys**', async (route: Route) => {
			const request = route.request();
			if (new URL(request.url()).pathname === '/api/api-keys' && request.method() === 'GET') {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(keyList)
				});
				return;
			}
			const keyId = usageKeyId(request.url());
			if (keyId === KEY_A) {
				aRequested = true;
				await heldA;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(usageFor(KEY_A, 'feoh_live_aaaa', 9111, '2024-06-01'))
				});
				return;
			}
			if (keyId === KEY_B) {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(usageFor(KEY_B, 'feoh_live_bbbb', 2222, '2024-06-02'))
				});
				return;
			}
			await route.continue();
		});

		try {
			await page.goto('/admin/api-keys');

			const rowA = page.getByRole('button', { name: `View usage for ${NAME_A}` });
			const rowB = page.getByRole('button', { name: `View usage for ${NAME_B}` });
			await expect(rowA).toBeVisible({ timeout: 15_000 });

			const dialog = page.getByRole('dialog', { name: 'API key usage' });
			const panel = page.getByTestId('api-key-usage');

			// 1. Open A's usage — its request is issued and then held.
			await rowA.click();
			await expect(panel).toHaveAttribute('data-key-id', KEY_A);
			await expect(page.getByTestId('usage-loading')).toBeVisible();
			await expect.poll(() => aRequested).toBe(true);

			// 2. Close it and open B's. B's response resolves normally.
			await dialog.getByRole('button', { name: 'Close' }).click();
			await expect(dialog).toBeHidden();
			await rowB.click();
			await expect(panel).toHaveAttribute('data-key-id', KEY_B);
			await expect(panel).toHaveAttribute('data-usage-for', KEY_B, { timeout: 15_000 });
			await expect(panel).toContainText('2,222');

			// 3. Release A's response LAST and wait for it to actually arrive.
			const aLanded = page.waitForResponse((r) => usageKeyId(r.url()) === KEY_A, {
				timeout: 15_000
			});
			releaseA();
			await aLanded;

			// An ordering barrier that touches the page but NOT the usage state: a
			// page-initiated round trip to the (stubbed) list endpoint whose result
			// nothing consumes. A full round trip is many event-loop turns, so by the
			// time it resolves, anything A's `.then` was going to write has already
			// been written — `waitForResponse` alone only proves the bytes arrived,
			// not that the continuation ran.
			await page.evaluate(async () => {
				await fetch('/api/api-keys').catch(() => {});
			});

			// The panel is still B's, in both the name it shows and the figures.
			await expect(panel).toHaveAttribute('data-key-id', KEY_B);
			await expect(panel).toHaveAttribute('data-usage-for', KEY_B);
			await expect(panel).toContainText('2,222');
			await expect(panel).not.toContainText('9,111');
		} finally {
			releaseA();
			await page.unroute('**/api/api-keys**').catch(() => {});
		}
	});
});

test.describe('/admin/api-keys (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/api-keys');
		// admin-only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'API Keys' })).toHaveCount(0);

		// The API itself 403s a non-admin.
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/api-keys`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
	});
});
