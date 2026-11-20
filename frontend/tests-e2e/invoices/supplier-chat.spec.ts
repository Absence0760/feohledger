import { expect, test } from '../fixtures/helpers';

/**
 * Embedded Supplier Chat — AP surface (inside the invoice detail modal).
 *
 * The chat section mounts between the Activity timeline and the Review
 * controls (`.chat-section` → <SupplierChatThread surface="ap">). It loads
 * lazily on modal open from GET /api/invoices/{id}/chat (lazy thread: id=null,
 * messages=[] when none exists yet).
 *
 * Contract (§10, frontend):
 *   1. An AP user posts a message → it renders right-aligned (the AP team's
 *      own role bubble, data-own="true").
 *   2. Posting writes a `chat_message_posted` audit row, which the modal
 *      re-fetches → a new Activity row appears ("Posted a chat message").
 *   3. Resolve flips the status pill open → resolved.
 *
 * The feature flag `Organization.settings.supplier_chat.enabled` defaults to
 * True (local-first), so a freshly-seeded tenant has chat enabled — no DB
 * setup needed. The worker's stored auth is the tenant admin, so resolve
 * (admin/ap_manager/cfo gated) is allowed.
 *
 * Posts are append-only and can't be deleted, so each run uses a unique body
 * to keep the assertion unambiguous across re-runs against the same tenant.
 */

test.describe('/invoices supplier chat — AP surface', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
		await expect(page.locator('table tbody tr').first()).toBeVisible();
	});

	async function openFirstInvoice(page: import('@playwright/test').Page) {
		await page.locator('table tbody tr').first().getByRole('button', { name: 'Edit' }).click();
		const modal = page.locator('div.modal[role="dialog"]');
		await expect(modal).toBeVisible();
		return modal;
	}

	test('AP posts a message: it renders right-aligned and adds an Activity row', async ({
		page,
	}) => {
		const modal = await openFirstInvoice(page);

		const chat = modal.locator('[data-testid="supplier-chat"]');
		await expect(chat).toBeVisible({ timeout: 10_000 });

		const body = `e2e ap chat ${Date.now()}`;
		await chat.locator('[data-testid="chat-input"]').fill(body);
		await chat.locator('[data-testid="chat-send"]').click();

		// The new message bubble renders, attributed to the AP team's own role
		// → right-aligned (data-own="true").
		const msg = chat.locator('[data-testid="chat-msg"]', { hasText: body });
		await expect(msg).toBeVisible({ timeout: 10_000 });
		await expect(msg).toHaveAttribute('data-own', 'true');
		await expect(msg).toHaveAttribute('data-role', 'ap_team');

		// The post re-fetches the audit log; a "Posted a chat message" Activity
		// row appears (the ACTION_LABELS mapping for chat_message_posted).
		await expect(
			modal.locator('.activity-section').getByText('Posted a chat message').first(),
		).toBeVisible({ timeout: 10_000 });
	});

	test('resolve flips the chat status pill to resolved', async ({ page }) => {
		const modal = await openFirstInvoice(page);

		const chat = modal.locator('[data-testid="supplier-chat"]');
		await expect(chat).toBeVisible({ timeout: 10_000 });

		// Resolve requires at least one message in the thread (the control is
		// disabled on an empty thread). Post one first if the thread is open
		// and empty.
		// The pill's TEXT is the localized label ("Open" / "Offen"); the thread
		// state is read off `data-status`, the raw wire value.
		const status = chat.locator('[data-testid="chat-status"]');
		if ((await status.getAttribute('data-status')) === 'resolved') {
			// A prior run left it resolved — reopen so this run starts at open.
			await chat.getByRole('button', { name: 'Reopen chat thread' }).click();
			await expect(status).toHaveAttribute('data-status', 'open', { timeout: 10_000 });
		}

		await chat.locator('[data-testid="chat-input"]').fill(`e2e resolve seed ${Date.now()}`);
		await chat.locator('[data-testid="chat-send"]').click();
		await expect(chat.locator('[data-testid="chat-msg"]').last()).toBeVisible({
			timeout: 10_000,
		});

		await expect(status).toHaveAttribute('data-status', 'open');
		await expect(status).toHaveText('Open');
		await chat.getByRole('button', { name: 'Resolve chat thread' }).click();

		// The pill flips and a resolve Activity row lands.
		await expect(status).toHaveAttribute('data-status', 'resolved', { timeout: 10_000 });
		await expect(status).toHaveText('Resolved');
		await expect(
			modal.locator('.activity-section').getByText('Resolved chat thread').first(),
		).toBeVisible({ timeout: 10_000 });

		// Restore the thread to open so the suite is re-runnable.
		await chat.getByRole('button', { name: 'Reopen chat thread' }).click();
		await expect(status).toHaveAttribute('data-status', 'open', { timeout: 10_000 });
	});
});
