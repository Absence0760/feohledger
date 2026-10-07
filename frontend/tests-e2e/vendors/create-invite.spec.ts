import { API_BASE, authedTenantHeaders, expect, test } from '../fixtures/helpers';

/**
 * /vendors — create a vendor and invite it to the supplier portal from the UI.
 *
 * issue #328 (persona-new-user): `POST /api/vendors` and
 * `POST /api/vendors/{id}/portal-users` both existed on the backend but the
 * page exposed neither — a new tenant couldn't onboard a supplier by clicking
 * anything. Now: a "+ New Vendor" header action opens `CreateVendorModal`, and
 * an "Invite" row action opens `InviteVendorPortalUserModal` whose one-time
 * temp password is shown through the shared `SecretReveal`.
 *
 * The vendor is created fresh (unique name), then deleted via API in `finally`
 * — a never-transacted vendor is hard-deletable and cascades its portal user.
 */

test.describe('/vendors create + portal invite (acme admin)', () => {
	test('create a vendor, then invite it to the portal', async ({ page }) => {
		const stamp = Date.now();
		const vendorName = `E2E New Vendor ${stamp}`;
		const contactEmail = `e2e-portal-${stamp}@example.test`;
		let vendorId = '';

		try {
			await page.goto('/vendors');

			// --- Create ---
			await page.getByRole('button', { name: 'New Vendor' }).click();
			const createModal = page.getByRole('dialog', { name: 'Create vendor' });
			await expect(createModal).toBeVisible();
			// Labels carry a trailing " *" for required fields, so match non-exact.
			await createModal.getByLabel('Name').fill(vendorName);
			await createModal.getByLabel('Code').fill(`E2E-${stamp}`);

			const createResp = page.waitForResponse(
				(r) => r.url().endsWith('/api/vendors') && r.request().method() === 'POST'
			);
			await createModal.getByRole('button', { name: 'Create vendor' }).click();
			vendorId = (await (await createResp).json()).id as string;
			expect(vendorId).toBeTruthy();

			// Find it by name. The list is name-ordered and paged, so on a tenant
			// with more than a page of vendors a new one can sort past page one
			// (tracked in docs/followups.md); this spec is about the invite.
			await page.goto(`/vendors?search=${encodeURIComponent(vendorName)}`);
			const row = page.locator('table tbody tr', { hasText: vendorName });
			await expect(row).toHaveCount(1, { timeout: 10_000 });

			// --- Invite to portal ---
			await row.getByRole('button', { name: 'Invite' }).click();
			const inviteModal = page.getByRole('dialog', {
				name: 'Invite vendor to the supplier portal'
			});
			await expect(inviteModal).toBeVisible();
			await inviteModal.getByLabel('Contact name').fill('E2E Portal Contact');
			await inviteModal.getByLabel('Email').fill(contactEmail);

			const inviteResp = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/vendors/${vendorId}/portal-users`) &&
					r.request().method() === 'POST'
			);
			await inviteModal.getByRole('button', { name: 'Send invite' }).click();
			expect((await inviteResp).status()).toBe(201);

			// The confirmation names the address the credential was emailed to —
			// and carries NO password. The temp password is delivered only by the
			// email adapter now: returning it to the caller (who also chose the
			// address) let one ap_manager mint a supplier login they controlled,
			// which is the first link of the BEC bank-redirect chain the approval
			// segregation check now refuses. If a password ever reappears in this
			// dialog, that hole is back.
			const sent = page.getByTestId('vendor-invite-sent');
			await expect(sent).toBeVisible({ timeout: 10_000 });
			await expect(sent).toContainText(contactEmail);
			const inviteBody = await (await inviteResp).json();
			expect(inviteBody).not.toHaveProperty('temp_password');

			// Dismiss — the confirmation must be gone from the DOM afterwards.
			// `exact: true`, because the default substring match also hits the
			// row link of any vendor whose NAME contains "Done" — and one exists
			// in every worker tenant that has run `invoices/file-management`
			// (it posts an invoice for "E2E Done Vendor", which auto-creates the
			// vendor). A real tenant can name a supplier anything, so the exact
			// match is the durable fix, not a tidier fixture.
			await page.getByRole('button', { name: 'Done', exact: true }).click();
			await expect(sent).toHaveCount(0);
		} finally {
			if (vendorId) {
				await page.request.delete(`${API_BASE}/api/vendors/${vendorId}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		}
	});
});
