import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	tenantHeaders,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * `/vendors/change-requests` — the UI half of the vendor bank/tax dual-control
 * (BEC / bank-redirect) gate.
 *
 * The backend gate has shipped for a while: `POST /api/vendors/{id}/bank-change`
 * STAGES a `VendorChangeRequest` instead of applying it, and a SECOND user
 * holding `vendor.bank_change.approve` applies it. But there was no UI for the
 * queue, so `/vendors` staged a change, toasted "submitted for approval", and
 * left the reviewer with nowhere to go — vendor banking could not be updated
 * through the app at all. This spec locks the whole loop through the UI:
 *
 *   1. Saving bank details on /vendors does NOT mutate the vendor.
 *   2. The staged request shows up in the approval queue, reachable from
 *      /vendors itself (the discoverability half of the fix).
 *   3. The PROPOSER cannot approve it — the row says so and the Approve button
 *      is disabled — and the API 403s the proposer even if the button were
 *      reached (segregation of duties; the UI is not the gate).
 *   4. A SECOND approver can approve from the queue, and only THEN do the
 *      vendor's live bank details change.
 *
 * Identity model: the per-worker fixtures give each tenant a distinct admin and
 * ap_manager (`demo+admin@e2eN.localhost` / `demo+manager@…`), both holding
 * `vendor.bank_change.approve` by the system-role default map — so a genuine
 * second approver exists without provisioning one. The manager's JWT is minted
 * once via the API and injected into localStorage rather than driving the login
 * form again (the login route is rate-limited per IP; the CFO-approval spec
 * established this pattern).
 */

interface VendorResp {
	id: string;
	name: string;
	bank_details: {
		counterparty_id?: string | null;
		account_last4?: string | null;
		bank_name?: string | null;
	} | null;
}

const ORIGINAL_BANK = 'Original Bank e2e';
const PROPOSED_BANK = 'Redirected Bank e2e';

// Resolved once per test. The slug is derived from the page's OWN origin
// (`<slug>.localhost`) and threaded explicitly into both the API headers and
// `tenantPsql`, so the JWT and the psql connection can never target different
// tenant DBs (the same care `bank-change-fraud-controls.spec.ts` takes — a
// staged row going missing is very hard to read as a tenant mismatch).
let H: Record<string, string>;
let SLUG: string;

function slugFromPage(page: import('@playwright/test').Page): string {
	return new URL(page.url()).hostname.split('.')[0];
}

async function roleToken(
	page: import('@playwright/test').Page,
	creds: { email: string; password: string }
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': SLUG },
		data: { email: creds.email, password: creds.password }
	});
	expect(res.status(), 'second-approver login should succeed').toBe(200);
	const body = (await res.json()) as { access_token?: string; token?: string };
	const token = body.access_token ?? body.token;
	expect(token, 'login returned a token').toBeTruthy();
	return token as string;
}

/** An already-onboarded vendor carrying an "original" account on file.
 *  `POST /api/vendors` stages any submitted `bank_details` rather than applying
 *  them (the same dual-control gate), so the baseline is written straight to
 *  the row — that's setup, not the mutation under test. No `counterparty_id`,
 *  so the row action reads "Bank" rather than "Bank ✓". */
async function createVendorWithBank(
	page: import('@playwright/test').Page,
	name: string
): Promise<VendorResp> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: H,
		data: { name }
	});
	expect(resp.status(), `create vendor ${name}`).toBe(201);
	const vendor = (await resp.json()) as VendorResp;
	const json = JSON.stringify({ bank_name: ORIGINAL_BANK, account_last4: '2222' }).replace(
		/'/g,
		"''"
	);
	tenantPsql(`UPDATE vendors SET bank_details='${json}'::jsonb WHERE id='${vendor.id}'`, SLUG);
	return vendor;
}

async function getVendor(
	page: import('@playwright/test').Page,
	id: string,
	headers: Record<string, string> = H
): Promise<VendorResp> {
	const resp = await page.request.get(`${API_BASE}/api/vendors/${id}`, { headers });
	expect(resp.status()).toBe(200);
	return (await resp.json()) as VendorResp;
}

function cleanupVendor(vendorId: string): void {
	// audit_log is append-only (immutable trigger) and PII-free — deliberately
	// left in place. Everything FK'd to the vendor goes first.
	try {
		deleteVendorsWhere(`id='${vendorId}'`, SLUG);
	} catch {
		/* best-effort */
	}
}

test.describe('/vendors/change-requests — dual-control bank-change approvals', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/vendors');
		SLUG = slugFromPage(page);
		H = await authedTenantHeaders(page, SLUG);
	});

	test('the queue is reachable from the vendors page', async ({ page }) => {
		// The discoverability half: /vendors stages the change, so it has to say
		// where the change went. Without this link the gate is unreachable.
		await page.getByRole('link', { name: 'Bank change approvals', exact: true }).click();
		await expect(page).toHaveURL(/\/vendors\/change-requests\/?$/);
		await expect(
			page.getByRole('heading', { name: 'Bank & Tax Change Approvals' })
		).toBeVisible();
	});

	test('a staged bank change only applies after a SECOND approver signs it off', async ({
		page,
		tenantManager
	}) => {
		const vendorName = `Dual-Control Co ${Date.now()}`;
		const vendor = await createVendorWithBank(page, vendorName);
		try {
			// ---- 1. Stage the change through the real /vendors UI ------------
			await page.goto('/vendors');
			await page.getByRole('textbox', { name: 'Search vendors' }).fill(vendorName);
			const vendorRow = page.locator('tr', { hasText: vendorName });
			await expect(vendorRow).toBeVisible();
			await vendorRow.getByRole('button', { name: 'Bank', exact: true }).click();

			const bankModal = page.getByRole('dialog', { name: 'Vendor bank counterparty' });
			await expect(bankModal).toBeVisible();
			// The dialog states the dual-control rule up front.
			await expect(bankModal).toContainText('second approver');
			await bankModal.getByLabel('Bank name').fill(PROPOSED_BANK);
			await bankModal.getByLabel('Account last 4').fill('8888');
			await bankModal.getByRole('button', { name: 'Save' }).click();

			// The toast names the queue the request landed in.
			await expect(
				page.locator('.toast', { hasText: 'Bank-detail change submitted' })
			).toBeVisible();
			await expect(page.locator('.toast.error')).toHaveCount(0);

			// ---- 2. The live vendor is UNTOUCHED -----------------------------
			const staged = await getVendor(page, vendor.id);
			expect(staged.bank_details?.bank_name).toBe(ORIGINAL_BANK);

			// ---- 3. The request is in the queue, and the PROPOSER can't approve
			await page.goto('/vendors/change-requests');
			const queueRow = page.locator('tr[data-testid="change-request-row"]', {
				hasText: vendorName
			});
			await expect(queueRow).toBeVisible();
			// The queue list masks the proposed value — last-4 only, never a full
			// account number.
			await expect(queueRow).toContainText('8888');
			await expect(queueRow).toContainText('Pending');
			// Segregation of duties, said on the row — in the server's own words, from
			// the row's `approve_refusal` verdict — rather than as a button that can
			// only fail.
			await expect(queueRow).toContainText('You cannot approve a bank-detail change you requested.');
			await expect(queueRow.getByRole('button', { name: 'Approve', exact: true })).toBeDisabled();

			// …and the server refuses the proposer regardless — the UI is not the
			// gate, it just declines to offer an action that cannot succeed.
			const requestId = tenantPsql(
				`SELECT id FROM vendor_change_requests WHERE vendor_id='${vendor.id}' ` +
					`AND status='pending' ORDER BY created_at DESC LIMIT 1`,
				SLUG
			)
				.split('\n')
				.map((l) => l.trim())
				.filter(Boolean)[0]!;
			expect(requestId).toMatch(/[0-9a-f-]{36}/);
			const selfApprove = await page.request.post(
				`${API_BASE}/api/vendors/change-requests/${requestId}/approve`,
				{ headers: H }
			);
			expect(selfApprove.status(), 'the proposer must not be able to approve').toBe(403);

			// Still pending, still unapplied.
			expect((await getVendor(page, vendor.id)).bank_details?.bank_name).toBe(ORIGINAL_BANK);

			// ---- 4. A SECOND approver signs it off from the queue -------------
			const managerToken = await roleToken(page, tenantManager);
			await page.evaluate((t) => localStorage.setItem('auth_token', t), managerToken);
			await page.goto('/vendors/change-requests');

			const mgrRow = page.locator('tr[data-testid="change-request-row"]', {
				hasText: vendorName
			});
			await expect(mgrRow).toBeVisible();
			await expect(mgrRow).not.toContainText('You requested this');

			const approve = mgrRow.getByRole('button', { name: 'Approve', exact: true });
			await expect(approve).toBeEnabled();
			// Armed two-click confirm — this is the click that redirects where
			// money goes, so one stray click must not commit it.
			await approve.click();
			const confirm = mgrRow.getByRole('button', { name: 'Confirm approve' });
			await expect(confirm).toBeVisible();
			await confirm.click();

			await expect(page.locator('.toast', { hasText: 'Change approved' })).toBeVisible();
			// The row leaves the Pending view once decided.
			await expect(
				page.locator('tr[data-testid="change-request-row"]', { hasText: vendorName })
			).toHaveCount(0);

			// ---- 5. ONLY NOW do the vendor's live bank details change ---------
			const mgrHeaders = tenantHeaders(managerToken, SLUG);
			const applied = await getVendor(page, vendor.id, mgrHeaders);
			expect(applied.bank_details?.bank_name).toBe(PROPOSED_BANK);
			expect(applied.bank_details?.account_last4).toBe('8888');
		} finally {
			cleanupVendor(vendor.id);
		}
	});
});
