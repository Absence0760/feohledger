import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

// Start unauthenticated — same treatment as signout.spec.ts, and for the same
// reason, only more so. This spec REVOKES sessions, so any JWT it touches is
// blocklisted server-side afterwards. Reusing the worker's cached storage-state
// admin would hand a dead token to the rest of the file. Each test signs in
// fresh instead. (The worker's cached admin file is additionally protected by
// signing in as the CLERK below, so `revoke-others` never reaches it.)
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * `/profile` → "Signed-in devices": the account holder's own remedy for a
 * session they don't recognise. The backend half (list / revoke / revoke-others)
 * is covered by pytest; this is the UI panel end-to-end.
 *
 * The contract under test is the one that actually protects a user: "Sign out
 * everywhere else", clicked on the device you trust, makes the OTHER device's
 * token stop working — not merely disappear from a list.
 */

/** The `<section class="card">` holding the sessions panel. */
function sessionsPanel(page: import('@playwright/test').Page) {
	return page
		.locator('section.card')
		.filter({ has: page.getByRole('heading', { name: 'Signed-in devices' }) });
}

test.describe('signed-in devices', () => {
	test('revoke-others from one session invalidates the other session', async ({
		page,
		browser,
		baseURL,
		tenantClerk
	}) => {
		// --- Session A: the device that will be signed out remotely. ---
		await signInAndWait(page, tenantClerk);

		// Setup, not assertion: clear any session this account left behind in
		// earlier specs. Without it the concurrent-session cap
		// (FEOH_MAX_CONCURRENT_SESSIONS, default 5) could evict A on B's login,
		// and the spec would pass for the wrong reason.
		const cleanup = await page.request.post(`${API_BASE}/api/auth/sessions/revoke-others`, {
			headers: await authedTenantHeaders(page),
			data: {}
		});
		expect(cleanup.ok()).toBe(true);

		// --- Session B: a second, independent browser context, same account. ---
		const contextB = await browser.newContext({ baseURL });
		try {
			const pageB = await contextB.newPage();
			await signInAndWait(pageB, tenantClerk);

			await pageB.goto('/profile?section=sessions');
			const panel = sessionsPanel(pageB);

			// Both sessions are listed, and B knows which one it is. Waiting on the
			// row count is the real readiness signal — the panel fetches its list in
			// a mount effect and renders "Loading…" until it resolves.
			await expect(panel.locator('li')).toHaveCount(2);
			await expect(panel.locator('li', { hasText: 'This device' })).toHaveCount(1);

			// A is genuinely still alive right now. This is what makes the final
			// assertion mean "revoke-others killed it" rather than "it was already
			// dead" (expired token, cap eviction, or a stale fixture).
			await page.goto('/invoices');
			await expect(page).toHaveURL(/\/invoices$/);

			// --- Revoke everything except B. Armed two-click, per the panel. ---
			const revokeOthers = panel.getByRole('button', { name: 'Sign out everywhere else' });
			await expect(revokeOthers).toBeVisible();
			await revokeOthers.click();
			await panel.getByRole('button', { name: /^Confirm — sign out 1 other session$/ }).click();

			// B's own session survives; the list collapses to just this device and
			// the bulk control disappears (otherSessionCount is now 0).
			await expect(panel.locator('li')).toHaveCount(1);
			await expect(panel.locator('li', { hasText: 'This device' })).toHaveCount(1);
			await expect(
				panel.getByRole('button', { name: 'Sign out everywhere else' })
			).toHaveCount(0);

			// --- The point of the whole feature: A's token is now dead. ---
			// Its next authenticated request 401s, which clears the token and
			// bounces it to /login.
			await page.goto('/invoices');
			await page.waitForURL(/\/login$/, { timeout: 15_000 });
			const token = await page.evaluate(() => localStorage.getItem('auth_token'));
			expect(token).toBeNull();
		} finally {
			await contextB.close();
		}
	});
});
