import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	tenantHeaders,
	test
} from '../fixtures/helpers';

/**
 * "Send to ERP" from the invoice-modal button — the UI money-path leg the
 * API-level lifecycle spec doesn't cover.
 *
 * Regression: an approved invoice is financially frozen server-side — a
 * `PATCH /api/invoices/{id}` that touches any `_FINANCIALLY_LOCKED_STATUSES`
 * financial field (amount / tax_rate / …) 409s. The modal's submit handler
 * (`submitDone`) pre-saved the WHOLE field set — including the frozen `amount`
 * — before calling `/complete`, so on an approved invoice the pre-save 409'd
 * and the "Send to ERP" click silently did nothing (the invoice never left
 * `approved`). The handler now omits the financial fields once the invoice is
 * financially locked, so the ERP advance runs. The API lifecycle spec drives
 * `/send-to-erp` directly and never exercised this button, which is why the
 * bug slipped through — hence this UI-level guard.
 */

type Inv = { id: string; invoice_number: string; status: string };

// Not a round figure: `round_amount` (>= 1000, a multiple of 100) opens a
// `fraud_flag`, and an open fraud flag refuses the ERP send (decisions §286).

async function createNewInvoice(page: import('@playwright/test').Page): Promise<Inv> {
	const unique = `E2E-ERPUI-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { invoice_number: unique, vendor: 'ERP UI Vendor', amount: '1534.21', currency: 'USD', status: 'new' }
	});
	if (resp.status() !== 201) throw new Error(`create failed (${resp.status()}): ${await resp.text()}`);
	return (await resp.json()) as Inv;
}

async function action(
	page: import('@playwright/test').Page,
	id: string,
	verb: string,
	creds?: Record<string, string>
) {
	return page.request.post(`${API_BASE}/api/invoices/${id}/${verb}`, {
		headers: creds ?? (await authedTenantHeaders(page)),
		data: {}
	});
}

/** Sign the tenant's seeded `manager` account in for the approve step —
 *  segregation of duties refuses the same actor who created the invoice
 *  from also approving it (manual entry now stamps uploaded_by_id, same as
 *  the file-upload path always did). */
async function managerCreds(
	page: import('@playwright/test').Page
): Promise<Record<string, string>> {
	const slug = currentTenantSlug();
	const resp = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug, 'Content-Type': 'application/json' },
		data: { email: `demo+manager@${slug}.localhost`, password: 'demo' }
	});
	expect(resp.ok(), `manager login failed (${resp.status()})`).toBe(true);
	const { access_token } = (await resp.json()) as { access_token: string };
	return tenantHeaders(access_token, slug);
}

async function getStatus(page: import('@playwright/test').Page, id: string): Promise<string> {
	const r = await page.request.get(`${API_BASE}/api/invoices/${id}`, { headers: await authedTenantHeaders(page) });
	return ((await r.json()) as Inv).status;
}

async function setErp(page: import('@playwright/test').Page, erp: unknown) {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: { erp } }
	});
}

test.describe('/invoices — Send to ERP button', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/invoices');
	});

	test('approved invoice: "Send to ERP" advances via the mock ERP (no financial-freeze 409)', async ({
		page
	}) => {
		// Point the tenant at the in-process mock ERP so the dispatch settles
		// deterministically (sending_to_erp → sent_to_erp → done).
		await setErp(page, { type: 'mock', integration_method: 'direct' });
		const inv = await createNewInvoice(page);
		try {
			// Poll for each transition's commit to land before firing the next —
			// the mutating endpoints commit after the response is sent (the
			// read-after-write race in docs/known-issues.md), so chaining raw
			// action() calls back-to-back can otherwise 409 on stale status.
			expect((await action(page, inv.id, 'complete')).status()).toBe(200);
			await expect.poll(() => getStatus(page, inv.id), { timeout: 10_000 }).toBe('ready_for_review');
			expect((await action(page, inv.id, 'approve', await managerCreds(page))).status()).toBe(
				200
			);
			await expect.poll(() => getStatus(page, inv.id), { timeout: 10_000 }).toBe('approved');

			// Open the approved invoice's detail modal from the list.
			await page.goto('/invoices');
			const row = page.locator('table tbody tr', { hasText: inv.invoice_number }).first();
			await expect(row).toBeVisible({ timeout: 10_000 });
			await row.getByRole('button', { name: 'Edit' }).click();

			const modal = page.locator('div.modal[role="dialog"]', { hasText: inv.invoice_number }).first();
			await expect(modal).toBeVisible();

			// The workflow has an erp_export step, so the submit button reads
			// "Send to ERP" on an approved invoice.
			const sendBtn = modal.getByRole('button', { name: 'Send to ERP' });
			await expect(sendBtn).toBeVisible();
			await sendBtn.click();

			// Before the fix the pre-save 409'd and this stayed `approved` forever.
			await expect
				.poll(async () => getStatus(page, inv.id), { timeout: 15_000 })
				.toMatch(/sent_to_erp|posted_in_erp|done/);
		} finally {
			await page.request
				.delete(`${API_BASE}/api/invoices/${inv.id}`, { headers: await authedTenantHeaders(page) })
				.catch(() => {});
			await setErp(page, null);
		}
	});

	test('a vendor blocked from payment: "Send to ERP" is refused with the vendor sentence (decisions §320)', async ({
		page
	}) => {
		await setErp(page, { type: 'mock', integration_method: 'direct' });
		const inv = await createNewInvoice(page);
		let vendorId: string | null = null;
		try {
			expect((await action(page, inv.id, 'complete')).status()).toBe(200);
			await expect.poll(() => getStatus(page, inv.id), { timeout: 10_000 }).toBe('ready_for_review');
			expect((await action(page, inv.id, 'approve', await managerCreds(page))).status()).toBe(
				200
			);
			await expect.poll(() => getStatus(page, inv.id), { timeout: 10_000 }).toBe('approved');

			// Manual entry links the typed name to a vendor (an unverified one,
			// which the push still books). Block it, as AP or a sanctions match would.
			const detail = await page.request.get(`${API_BASE}/api/invoices/${inv.id}`, {
				headers: await authedTenantHeaders(page)
			});
			vendorId = ((await detail.json()) as { vendor_id: string | null }).vendor_id;
			expect(vendorId, 'manual entry links a vendor').toBeTruthy();
			const blocked = await page.request.post(`${API_BASE}/api/vendors/${vendorId}/block`, {
				headers: await authedTenantHeaders(page),
				data: { reason: 'e2e block' }
			});
			expect(blocked.status(), await blocked.text()).toBe(200);

			await page.goto('/invoices');
			const row = page.locator('table tbody tr', { hasText: inv.invoice_number }).first();
			await expect(row).toBeVisible({ timeout: 10_000 });
			await row.getByRole('button', { name: 'Edit' }).click();
			const modal = page.locator('div.modal[role="dialog"]', { hasText: inv.invoice_number }).first();
			await expect(modal).toBeVisible();
			await modal.getByRole('button', { name: 'Send to ERP' }).click();

			// The 409's coded refusal, stated in the reader's language
			// (erpRefusal.vendorBlockedExport), not the raw code.
			await expect(page.locator('.toast.error')).toContainText(
				'blocked from payment (a sanctions match or a block by AP)'
			);
			expect(await getStatus(page, inv.id)).toBe('approved');
		} finally {
			if (vendorId) {
				await page.request
					.post(`${API_BASE}/api/vendors/${vendorId}/unblock`, {
						headers: await authedTenantHeaders(page)
					})
					.catch(() => {});
			}
			await page.request
				.delete(`${API_BASE}/api/invoices/${inv.id}`, { headers: await authedTenantHeaders(page) })
				.catch(() => {});
			await setErp(page, null);
		}
	});
});
