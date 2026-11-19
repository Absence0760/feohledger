import type { Page } from '@playwright/test';

import {
	API_BASE,
	acceptConsent,
	currentTenantSlug,
	expect,
	test,
	tenantPsql,
} from '../fixtures/helpers';

/**
 * Embedded Supplier Chat — portal (vendor) surface.
 *
 * On /portal/invoices, clicking a row expands a per-row chat panel mounting
 * <SupplierChatThread surface="vendor">. The vendor posts via
 * POST /api/portal/invoices/{id}/chat; messages render in the same bubble UI
 * as the AP side, but the portal response is MASKED — an AP-authored message
 * carries `author_name` only, never an internal `users.id` (no mentions).
 *
 * Contract (§10, frontend):
 *   1. The vendor expands a row, posts a message, and it renders (right-
 *      aligned for the supplier's own role).
 *   2. An AP-authored message shows the AP author's name but no internal id.
 *
 * Auth model + seed shape mirror portal.spec.ts: one VendorUser per tenant
 * (`supplier@portal.test`, password "demo"), owning at least one invoice. The
 * spec opts out of the worker-admin storage state and drives the portal login.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

test.use({ storageState: { cookies: [], origins: [] } });

async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	// Sign-in lands on the portal HOME (it exists to answer "what needs my
	// attention"); this spec exercises the invoice list, so navigate on
	// explicitly rather than depending on where login happens to land.
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
	await page.goto('/portal/invoices');
}

test.describe('/portal supplier chat — vendor surface', () => {
	test('a vendor expands a row and posts a message that renders', async ({ page }) => {
		await portalSignIn(page);

		const firstRow = page.locator('table tbody tr.clickable').first();
		await expect(firstRow).toBeVisible({ timeout: 10_000 });
		await firstRow.click();

		const chat = page.locator('[data-testid="supplier-chat"]');
		await expect(chat).toBeVisible({ timeout: 10_000 });

		const body = `e2e vendor chat ${Date.now()}`;
		await chat.locator('[data-testid="chat-input"]').fill(body);
		await chat.locator('[data-testid="chat-send"]').click();

		// The supplier's own message renders right-aligned (the vendor surface's
		// own role is `supplier`).
		const msg = chat.locator('[data-testid="chat-msg"]', { hasText: body });
		await expect(msg).toBeVisible({ timeout: 10_000 });
		await expect(msg).toHaveAttribute('data-own', 'true');
		await expect(msg).toHaveAttribute('data-role', 'supplier');
	});

	test('an AP-authored message is shown by name only, with no internal id', async ({
		page,
		request,
		tenantAdmin,
	}) => {
		await portalSignIn(page);

		const slug = currentTenantSlug();

		// An invoice the portal vendor owns.
		const vendorId = tenantPsql(
			`SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`,
		).trim();
		expect(vendorId).not.toEqual('');
		// Take the NUMBER alongside the id: the panel assertion below has to
		// expand THIS invoice's row, and the portal list is not ordered by
		// `created_at` — every seeded portal invoice shares one, so `LIMIT 1`
		// picks arbitrarily among ties. Clicking `.first()` and hoping it was
		// the same row is what made this test fail against a seeded tenant.
		//
		// The `invoice_number <> ''` leg is load-bearing, not defensive. Any
		// sibling spec that submits through the portal leaves a row for THIS
		// vendor with no number yet (extraction has not run, or failed) — and
		// being the newest, it wins `ORDER BY created_at DESC`. The row is real,
		// so the test cannot assert it away; it simply is not the row this test
		// can drive, because the list renders it with nothing to filter on.
		// Ask for an invoice the portal list can actually be searched by.
		const [invoiceId, invoiceNumber] = tenantPsql(
			`SELECT id || '|' || invoice_number FROM invoices ` +
				`WHERE vendor_id='${vendorId}' AND invoice_number IS NOT NULL ` +
				`AND invoice_number <> '' ORDER BY created_at DESC, id DESC LIMIT 1`,
		)
			.trim()
			.split('|');
		expect(invoiceId).not.toEqual('');
		expect(invoiceNumber).toBeTruthy();

		// Author an AP-side (ap_team) message via the AP API so the portal has a
		// foreign message to mask. The AP response DOES expose author_user_id.
		const apToken = await apAdminToken(page, slug, tenantAdmin);
		const apBody = `ap reply ${Date.now()}`;
		const apPost = await request.post(`${API_BASE}/api/invoices/${invoiceId}/chat`, {
			headers: { Authorization: `Bearer ${apToken}`, 'X-Tenant-Slug': slug },
			data: { body: apBody },
		});
		expect(apPost.ok()).toBeTruthy();
		const apMsg = (await apPost.json()) as { author_user_id?: string };
		expect(apMsg.author_user_id).toBeTruthy();

		// Read the SAME thread through the portal client: the AP message must be
		// present, attributed by name, with NO author_user_id (the mask).
		const portalToken = await page.evaluate(() => localStorage.getItem('portal_auth_token'));
		const res = await request.get(`${API_BASE}/api/portal/invoices/${invoiceId}/chat`, {
			headers: { Authorization: `Bearer ${portalToken}`, 'X-Tenant-Slug': slug },
		});
		expect(res.ok()).toBeTruthy();
		const thread = (await res.json()) as { messages: Array<Record<string, unknown>> };
		const apSeen = thread.messages.find((m) => m.body === apBody);
		expect(apSeen).toBeTruthy();
		expect(apSeen!.author_role).toEqual('ap_team');
		expect(apSeen!.author_name).toBeTruthy();
		expect(apSeen).not.toHaveProperty('author_user_id');
		expect(apSeen).not.toHaveProperty('mention_user_ids');

		// And in the rendered panel, the AP message shows its author name.
		await page.reload();
		// Expand the row for the invoice the message was actually posted to,
		// named — not whichever row happens to be first.
		const row = page.locator('table tbody tr.clickable', { hasText: invoiceNumber }).first();
		await expect(row).toBeVisible({ timeout: 10_000 });
		await row.click();
		const chat = page.locator('[data-testid="supplier-chat"]');
		await expect(chat).toBeVisible({ timeout: 10_000 });

		// Locate the specific AP-authored bubble (by its body text) and assert it
		// is attributed by a non-empty author name. This is the contract check:
		// the portal must mask the internal id while still showing who posted —
		// catching a regression where the portal masks the body instead of the id.
		const apBubble = chat.locator('[data-testid="chat-msg"]', { hasText: apBody });
		await expect(apBubble).toBeVisible({ timeout: 10_000 });
		await expect(apBubble).toHaveAttribute('data-role', 'ap_team');
		await expect(apBubble.locator('.chat-author')).not.toBeEmpty();
	});

	test('a failed thread read says so and retries, never posing as an empty conversation', async ({
		page,
	}) => {
		await portalSignIn(page);

		// Fail the FIRST read of whichever thread is opened, then let reads
		// through: the retry must reach the real endpoint. GET only — a send
		// is not what this test is about.
		let failed = false;
		await page.route(/\/api\/portal\/invoices\/[^/]+\/chat$/, async (route) => {
			if (route.request().method() === 'GET' && !failed) {
				failed = true;
				await route.fulfill({ status: 500, body: '{"detail":"boom"}' });
				return;
			}
			await route.continue();
		});

		const firstRow = page.locator('table tbody tr.clickable').first();
		await expect(firstRow).toBeVisible({ timeout: 10_000 });
		await firstRow.click();

		const chat = page.locator('[data-testid="supplier-chat"]');
		const loadError = chat.locator('[data-testid="chat-load-error"]');
		await expect(loadError).toBeVisible();
		await expect(chat).not.toContainText('No messages yet');
		// No composer over a conversation the supplier cannot see.
		await expect(chat.locator('[data-testid="chat-input"]')).toHaveCount(0);

		await loadError.getByRole('button').click();
		await expect(loadError).toHaveCount(0);
		await expect(chat.locator('[data-testid="chat-input"]')).toBeVisible();
	});
});

/** Mint a control-plane admin bearer token via the AP login API. The AP app
 *  stores its JWT under `auth_token`, a different key from the portal's
 *  `portal_auth_token`, so this request-context login does not disturb the
 *  portal session in localStorage (we go through `page.request`, not the UI). */
async function apAdminToken(
	page: Page,
	slug: string,
	creds: { email: string; password: string },
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug },
		data: { email: creds.email, password: creds.password },
	});
	if (!res.ok()) throw new Error(`AP admin login failed: ${res.status()}`);
	const body = (await res.json()) as { access_token: string };
	return body.access_token;
}
