import { acceptConsent, ACME_ADMIN, ACME_BASE, API_BASE, escapeRegExp, expect, signInAndWait, TECHFLOW_ADMIN, TECHFLOW_BASE, test } from '../fixtures/helpers';


// Start unauthenticated — this spec drives its own ACME/TECHFLOW logins to assert
// cross-tenant read isolation. The per-worker admin storage state would point at
// the wrong tenant.
test.use({ storageState: { cookies: [], origins: [] } });

// Pinned to the acme tenant: this spec uses ACME_*/TECHFLOW_* creds or
// asserts cross-tenant isolation that requires fixed tenant slugs. The
// per-worker baseURL from fixtures/helpers.ts would otherwise route to
// the wrong tenant. Multiple workers may share acme here — keep this
// file's tests read-only or idempotent.
test.use({ baseURL: ACME_BASE });

/**
 * Tenant isolation — a JWT minted for the techflow tenant must not be
 * usable to read acme's data even when the browser is pointed at the
 * acme subdomain. The backend enforces this at two layers:
 *
 *   1. JWT carries the org_id claim. Cross-tenant requests fail at
 *      the auth dependency.
 *   2. Each tenant's data lives in a separate Postgres DB
 *      (`feoh_<slug>`), so even a leaked JWT can't accidentally read
 *      another tenant's rows.
 *
 * Tests below exercise that contract at two layers:
 *   - UI: cross-tenant nav redirects to /login (no data leak in the shell).
 *   - API: a direct fetch with the wrong tenant token returns 401/403
 *     even though the request shape is otherwise valid. This pins the
 *     server-side check so a frontend regression that stops redirecting
 *     can't quietly leak data.
 */

const API_URL = API_BASE;

/** Sign in via the UI and pluck the JWT out of localStorage. */
async function tokenAfterLogin(
	page: import('@playwright/test').Page,
	baseURL: string,
	creds: { email: string; password: string }
): Promise<string> {
	await page.goto(`${baseURL}/login`);
	await page.locator('input[type="email"]').fill(creds.email);
	await page.locator('input[type="password"]').fill(creds.password);
	await page.locator('form button[type="submit"]').click();
	await page.waitForURL(new RegExp(`^${escapeRegExp(baseURL)}/?$`), { timeout: 15_000 });
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	if (!token) throw new Error('expected auth_token after sign-in');
	return token;
}

test.describe('tenant isolation — UI', () => {
	test.use({ baseURL: TECHFLOW_BASE });

	test('techflow JWT cannot reach acme data', async ({ page }) => {
		await signInAndWait(page, TECHFLOW_ADMIN);

		// localStorage now holds techflow's JWT. Hop to acme's invoices.
		// Frontend sends X-Tenant-Slug=acme + techflow Bearer. Backend
		// rejects → api.ts redirects → end state: anon on acme /login.
		await page.goto(`${ACME_BASE}/invoices`);
		await page.waitForURL(/\/login/, { timeout: 10_000 });
		expect(page.url()).toMatch(new RegExp(`^${escapeRegExp(ACME_BASE)}/`));
	});

	test('reverse direction: acme JWT cannot reach techflow data', async ({ page }) => {
		// Belt-and-braces — confirms the check isn't one-directional
		// (we caught a bug like that once when org_id was substringed).
		const acmeToken = await tokenAfterLogin(page, ACME_BASE, ACME_ADMIN);
		expect(acmeToken).toBeTruthy();

		await page.goto(`${TECHFLOW_BASE}/invoices`);
		await page.waitForURL(/\/login/, { timeout: 10_000 });
		expect(page.url()).toMatch(new RegExp(`^${escapeRegExp(TECHFLOW_BASE)}/`));
	});
});

test.describe('tenant isolation — API', () => {
	test('techflow JWT + X-Tenant-Slug:acme is rejected at the backend', async ({
		request,
		page
	}) => {
		const techflowToken = await tokenAfterLogin(page, TECHFLOW_BASE, TECHFLOW_ADMIN);

		// Direct API call, no browser in the loop. This is the contract
		// the UI redirect depends on — if it ever stops returning 4xx
		// the redirect is just cosmetic and an attacker can scrape JSON.
		const res = await request.get(`${API_URL}/api/invoices`, {
			headers: {
				Authorization: `Bearer ${techflowToken}`,
				'X-Tenant-Slug': 'acme'
			}
		});
		expect(res.status(), 'cross-tenant API call must not return 2xx').toBeGreaterThanOrEqual(
			400
		);
		expect(res.status()).toBeLessThan(500);
	});

	test('techflow JWT + X-Tenant-Slug:techflow does succeed (positive control)', async ({
		request,
		page
	}) => {
		// Without this control, the cross-tenant test could be passing
		// because the token itself is broken, not because of isolation.
		const techflowToken = await tokenAfterLogin(page, TECHFLOW_BASE, TECHFLOW_ADMIN);

		const res = await request.get(`${API_URL}/api/invoices`, {
			headers: {
				Authorization: `Bearer ${techflowToken}`,
				'X-Tenant-Slug': 'techflow'
			}
		});
		expect(res.status(), 'matching-tenant API call must succeed').toBe(200);
	});

	test('no Authorization header is rejected the same way regardless of tenant header', async ({
		request
	}) => {
		// Authorization is the gate; the tenant header alone must never
		// be enough. Both shapes of "no auth" must fail closed.
		const r1 = await request.get(`${API_URL}/api/invoices`, {
			headers: { 'X-Tenant-Slug': 'acme' }
		});
		const r2 = await request.get(`${API_URL}/api/invoices`, {
			headers: { 'X-Tenant-Slug': 'techflow' }
		});
		expect(r1.status()).toBe(401);
		expect(r2.status()).toBe(401);
	});

	test('copying a token to a different origin does not grant cross-tenant access', async ({
		page,
		browser
	}) => {
		// Simulate an attacker copying their own JWT into a different
		// tenant's origin (different subdomain ⇒ separate localStorage).
		// Even after the SPA reads the planted token, the backend must
		// refuse any data request — the JWT carries `org=techflow` and
		// the host's X-Tenant-Slug header says `acme`.
		//
		// The token is obtained on techflow's OWN page: this describe runs on
		// acme's origin, and signing techflow's account in on acme's page is
		// refused since decisions §288.
		const techflowToken = await tokenAfterLogin(page, TECHFLOW_BASE, TECHFLOW_ADMIN);
		expect(techflowToken).toBeTruthy();

		// Use a fresh context for the acme origin — sharing the page
		// context can drag cookies / state in non-obvious ways.
		const acmeCtx = await browser.newContext();
		try {
			const r = await acmeCtx.request.get(`${API_URL}/api/invoices`, {
				headers: {
					Authorization: `Bearer ${techflowToken}`,
					'X-Tenant-Slug': 'acme'
				}
			});
			// 403 (tenant-mismatch guard) or 401 (in case the guard ever
			// gets reshaped to fail-as-anon). The forbidden line is "any
			// 2xx response" — that would mean the data leaked.
			expect(r.status(), 'planted token must not read acme data').toBeGreaterThanOrEqual(
				400
			);
			expect(r.status()).toBeLessThan(500);

			// Also verify nothing leaked in the body (defensive against
			// a future change that returns 4xx but still includes a
			// preview list).
			const bodyText = await r.text();
			expect(bodyText.toLowerCase()).not.toContain('"invoice_number"');
		} finally {
			await acmeCtx.close();
		}
	});
});

/**
 * The JOURNEY, not just the isolation property. Production, 2026-10-20: an
 * account of one workspace signed in on another workspace's page, login handed
 * it a token for its own org, and every call on that page was then refused
 * (403 `token_tenant_mismatch`) behind a signed-in screen stuck on "Loading…".
 * No data leaked — which is all the specs above assert — and that is exactly
 * why they missed it: each signs in on a tenant's OWN origin and hops, where
 * per-origin localStorage just leaves the other origin signed out. These sign
 * in on the wrong page, and assert the user ends somewhere usable
 * (docs/decisions.md §288).
 */
test.describe('signing in on another workspace’s page', () => {
	const loginError = (page: import('@playwright/test').Page) => page.locator('.login-form .error');

	async function submitLogin(
		page: import('@playwright/test').Page,
		creds: { email: string; password: string }
	) {
		await page.locator('input[type="email"]').fill(creds.email);
		await page.locator('input[type="password"]').fill(creds.password);
		const response = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/auth/login' && r.request().method() === 'POST'
		);
		await page.locator('form button[type="submit"]').click();
		return response;
	}

	test('a techflow account on acme’s page is sent to techflow, and acme’s own account then signs in', async ({
		page
	}) => {
		await acceptConsent(page);
		await page.goto(`${ACME_BASE}/login`);

		const refused = await submitLogin(page, TECHFLOW_ADMIN);
		expect(refused.status()).toBe(403);

		// No session was created, and the page says where this account signs in.
		const error = loginError(page);
		// The sentence names the workspace by host only; the URL, with its
		// percent-encoded address, is the link's and is never repeated in it.
		const sentence = error.locator('span');
		await expect(sentence).toHaveText(
			/^This account belongs to another workspace, techflow\.[^\s/?]+\. Sign in there instead\.$/
		);
		await expect(error).not.toContainText('?email=');
		await expect(error).not.toContainText('%40');
		const ownPage = page
			.locator('.login-form [role="alert"]')
			.getByRole('link', { name: "Go to your workspace's sign-in page" });
		await expect(ownPage).toBeVisible();
		await expect(ownPage).toHaveAttribute(
			'href',
			/^https?:\/\/techflow\.[^/]+\/login\?email=admin%40techflow\.com$/
		);
		// The link sits on its own line under the sentence and inside the box,
		// in both themes and at 320px. It used to be a narrow column beside the
		// text that ran past the box's right edge.
		for (const colorScheme of ['dark', 'light'] as const) {
			await page.emulateMedia({ colorScheme });
			for (const width of [1280, 320]) {
				await page.setViewportSize({ width, height: 800 });
				const where = `${colorScheme}, ${width}px`;
				const box = (await error.boundingBox())!;
				const text = (await sentence.boundingBox())!;
				const link = (await ownPage.boundingBox())!;
				expect(link.y, `link under the sentence (${where})`).toBeGreaterThanOrEqual(
					text.y + text.height - 1
				);
				expect(link.x, `link inside the box (${where})`).toBeGreaterThanOrEqual(box.x);
				expect(link.x + link.width, `link inside the box (${where})`).toBeLessThanOrEqual(
					box.x + box.width + 0.5
				);
			}
		}
		await page.emulateMedia({ colorScheme: null });
		await page.setViewportSize({ width: 1280, height: 800 });
		expect(page.url()).toMatch(new RegExp(`^${escapeRegExp(ACME_BASE)}/login`));
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();

		// The same page still works for acme's own account, end to end: the
		// dashboard's figures load rather than the shell sitting on "Loading…".
		const ok = await submitLogin(page, ACME_ADMIN);
		expect(ok.status()).toBe(200);
		await page.waitForURL(new RegExp(`^${escapeRegExp(ACME_BASE)}/?$`));
		await expect(page.locator('.kpi-row .kpi[data-kpi-state="value"]')).toHaveCount(5);
		await expect(page.getByText('Failed to load organization')).toHaveCount(0);
	});

	test('a token for another workspace already in storage signs the user out to this page’s sign-in', async ({
		page,
		request
	}) => {
		// Defence in depth for a token that got there some other way (one issued
		// before the sign-in refusal, a renamed workspace): every tenant-scoped
		// call answers 403 `token_tenant_mismatch`, and the SPA must treat that
		// like an expired session instead of hanging.
		const techflowToken = await tokenAfterLogin(page, TECHFLOW_BASE, TECHFLOW_ADMIN);

		await acceptConsent(page);
		await page.goto(`${ACME_BASE}/login`);
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await page.evaluate((t) => localStorage.setItem('auth_token', t), techflowToken);

		// The token is still a live session in techflow; the SPA revokes it
		// there on the way out (decisions §291), with the token alone — not
		// acme's slug, which is what caused the refusal.
		const revocation = page.waitForRequest(
			(r) => new URL(r.url()).pathname === '/api/auth/logout' && r.method() === 'POST'
		);
		await page.goto(`${ACME_BASE}/`);
		await page.waitForURL(`${ACME_BASE}/login?reason=other_workspace`);
		// Information, not a failure: a neutral notice in a polite live region,
		// worded so it never claims this page is the user's own workspace.
		// The form has a second status region (the SSO-config loading line), so
		// name the one carrying the notice.
		const status = page
			.locator('.login-form [role="status"]')
			.filter({ has: page.locator('.info') });
		await expect(status).toHaveAttribute('aria-live', 'polite');
		await expect(status.locator('.info')).toHaveText(
			"You were signed out because your session belonged to a different workspace. Sign in here, or on your own workspace's page."
		);
		await expect(loginError(page)).toHaveCount(0);
		expect(await page.evaluate(() => localStorage.getItem('auth_token'))).toBeNull();

		const sent = await revocation;
		expect(sent.headers()['authorization']).toBe(`Bearer ${techflowToken}`);
		expect(sent.headers()['x-tenant-slug']).toBeUndefined();
		// …and techflow itself now refuses the token.
		await expect
			.poll(
				async () =>
					(
						await request.get(`${API_URL}/api/auth/me`, {
							headers: {
								Authorization: `Bearer ${techflowToken}`,
								'X-Tenant-Slug': 'techflow'
							}
						})
					).status()
			)
			.toBe(401);
	});
});
