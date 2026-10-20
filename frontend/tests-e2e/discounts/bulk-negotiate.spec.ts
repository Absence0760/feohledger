import {
	API_BASE,
	authedTenantHeaders,
	deleteVendorsWhere,
	expect,
	selectVendorInPicker,
	signInAndWait,
	signOut,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * `/discounts` — proposing a vendor-wide offer
 * (`POST /api/discounts/bulk-negotiate`).
 *
 * The endpoint shipped fully built with NO caller anywhere in the app — the
 * last member of the round-21 caller-less group. This spec is the caller's
 * regression net, and it locks the four properties that make the control
 * honest rather than merely present:
 *
 *   1. the role gate matches `_WRITE_ROLES` (admin / ap_manager) — narrower
 *      than the page's accept/decline gate, so a CFO and a clerk both read the
 *      page and neither sees the trigger;
 *   2. the confirm is two-click arm-then-commit, and the FIRST click posts
 *      nothing — the proposal covers every open invoice the vendor has, at a
 *      base the proposer cannot see;
 *   3. the created offer's `base_amount` — the summed open balance, which only
 *      the server can total — is rendered, and it is the FIRST figure the page
 *      shows (nothing is previewed before the server computes it,
 *      `docs/decisions.md` §34);
 *   4. the 409 refusal ("no open invoices to negotiate against") lands in a
 *      persistent inline region, not a toast that fades before it is read.
 *
 * Runs against the real backend like its siblings in this directory. Every
 * fixture row is uniquely named and torn down with `deleteVendorsWhere`, which
 * cascades `discount_offers`.
 */

const RUN = `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
const VENDOR_WITH_BALANCE = `BulkNeg Open ${RUN}`;
const VENDOR_NO_BALANCE = `BulkNeg Empty ${RUN}`;

interface Vendor {
	id: string;
	name: string;
}

async function makeVendor(page: import('@playwright/test').Page, name: string): Promise<Vendor> {
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: await authedTenantHeaders(page),
		data: { name }
	});
	expect(resp.status(), await resp.text()).toBeLessThan(300);
	return (await resp.json()) as Vendor;
}

/** An `approved` invoice bound to the vendor — one of the statuses
 *  `_OPEN_FOR_DISCOUNT` sums. `POST /api/invoices` deliberately ignores a
 *  client-supplied status (the status-injection fix), so it is forced by SQL,
 *  exactly as `money-path.spec.ts` does. */
async function makeApprovedInvoice(
	page: import('@playwright/test').Page,
	vendor: Vendor,
	amount: number
): Promise<void> {
	const resp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: {
			vendor: vendor.name,
			invoice_number: `BULKNEG-${RUN}-${amount}`,
			amount
		}
	});
	expect(resp.status(), await resp.text()).toBeLessThan(300);
	const inv = (await resp.json()) as { id: string };
	tenantPsql(
		`UPDATE invoices SET status='approved', vendor_id='${vendor.id}' WHERE id='${inv.id}'`
	);
}

// The "does this tenant have under 100 vendors?" premise check that used to
// live here is GONE, along with the cap it guarded. The picker is a searchable,
// server-paged combobox: the fixture vendor is found by typing its name, so a
// tenant's vendor count no longer decides whether this spec can run.

function openForm(page: import('@playwright/test').Page) {
	return page.getByRole('button', { name: 'Propose vendor offer' });
}

const submit = (page: import('@playwright/test').Page) =>
	page.getByTestId('bulk-negotiate-submit');

async function fillTier(
	page: import('@playwright/test').Page,
	days: string,
	percent: string
): Promise<void> {
	await page.getByLabel('Tier 1 — pay within days').fill(days);
	await page.getByLabel('Tier 1 — discount percent').fill(percent);
}

test.describe('/discounts — propose a vendor-wide offer', () => {
	test('the created offer reports the summed open balance the server computed', async ({
		page
	}) => {
		try {
			const vendor = await makeVendor(page, VENDOR_WITH_BALANCE);
			await makeApprovedInvoice(page, vendor, 1000);
			await makeApprovedInvoice(page, vendor, 2000);

			await page.goto('/discounts');
			await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();
			await openForm(page).click();

			// Nothing about the vendor's balance is shown before the server totals
			// it — the result panel is the first and only sighting of that figure.
			await expect(page.getByTestId('bulk-negotiate-result')).toHaveCount(0);

			await selectVendorInPicker(
				page.getByTestId('bulk-negotiate-vendor'),
				VENDOR_WITH_BALANCE
			);
			await fillTier(page, '10', '2.00');

			// First click ARMS and posts nothing.
			let posted = 0;
			await page.route('**/api/discounts/bulk-negotiate', async (route) => {
				posted += 1;
				await route.continue();
			});
			await submit(page).click();
			await expect(submit(page)).toHaveText(`Confirm — propose to ${VENDOR_WITH_BALANCE}`);
			expect(posted, 'arming must not submit the proposal').toBe(0);

			const created = page.waitForResponse(
				(r) =>
					r.url().includes('/api/discounts/bulk-negotiate') && r.request().method() === 'POST'
			);
			await submit(page).click();
			const resp = await created;
			expect(resp.status(), await resp.text()).toBe(201);

			// 1000 + 2000 — the two open invoices, summed server-side.
			await expect(page.getByTestId('bulk-negotiate-base')).toContainText('3,000.00');
			// And the tier's exact saving off that base: 2 % of 3,000.
			await expect(page.getByTestId('bulk-negotiate-result')).toContainText('60.00');

			// The offer really exists, vendor-scoped and merely `offered` —
			// proposing commits no cash.
			const body = (await resp.json()) as { scope: string; status: string };
			expect(body.scope).toBe('vendor');
			expect(body.status).toBe('offered');
		} finally {
			// Cascades this vendor's invoices AND its discount_offers.
			deleteVendorsWhere(`name = '${VENDOR_WITH_BALANCE}'`);
		}
	});

	test('a vendor with no open invoices is refused, and the reason persists', async ({ page }) => {
		try {
			await makeVendor(page, VENDOR_NO_BALANCE);

			await page.goto('/discounts');
			await openForm(page).click();
			await selectVendorInPicker(page.getByTestId('bulk-negotiate-vendor'), VENDOR_NO_BALANCE);
			await fillTier(page, '7', '1.50');

			await submit(page).click(); // arm
			const refused = page.waitForResponse(
				(r) =>
					r.url().includes('/api/discounts/bulk-negotiate') && r.request().method() === 'POST'
			);
			await submit(page).click(); // commit
			expect((await refused).status()).toBe(409);

			// The backend's own sentence, in a region that stays on screen.
			const err = page.getByTestId('bulk-negotiate-error');
			await expect(err).toBeVisible();
			await expect(err).toContainText('no open invoices');
			// Still the form, never a result panel claiming an offer exists.
			await expect(page.getByTestId('bulk-negotiate-result')).toHaveCount(0);
		} finally {
			deleteVendorsWhere(`name = '${VENDOR_NO_BALANCE}'`);
		}
	});

	test('an unreadable tier is refused before anything is posted', async ({ page }) => {
		await page.goto('/discounts');
		await openForm(page).click();

		// 100 % is outside the server's `lt=100`; refusing it here buys a
		// sentence instead of a raw 422.
		await fillTier(page, '10', '100');
		await expect(page.getByTestId('bulk-negotiate-tier-invalid')).toBeVisible();
		await expect(submit(page)).toBeDisabled();

		await fillTier(page, '10', '2.00');
		await expect(page.getByTestId('bulk-negotiate-tier-invalid')).toHaveCount(0);
	});

	test('with no end date the form says the offer cannot be ranked', async ({ page }) => {
		await page.goto('/discounts');
		await openForm(page).click();
		// `valid_until` starts empty, which is what the backend allows and what
		// leaves the offer horizon-less.
		await expect(page.getByTestId('bulk-negotiate-no-horizon')).toBeVisible();

		await page.getByTestId('bulk-negotiate-valid-until').fill('2099-01-01');
		await expect(page.getByTestId('bulk-negotiate-no-horizon')).toHaveCount(0);
	});

	test('a clerk and a CFO read the page but get no proposal control', async ({
		page,
		tenantClerk,
		tenantCfo
	}) => {
		// Both are allowed on `/discounts` (`_READ_ROLES`); neither is in
		// `_WRITE_ROLES`, so neither may propose. A CFO is the interesting half:
		// they MAY accept and decline, so the two gates genuinely differ.
		//
		// The worker tenant's own clerk and CFO, signed in on its own page. This
		// used to sign acme's accounts in on the worker tenant's page, which
		// login allowed until decisions §288; every call on the page was then
		// refused for the token's tenant, so "no proposal control" held for a
		// page that had loaded nothing.
		for (const creds of [tenantClerk, tenantCfo]) {
			await page.goto('/');
			await signOut(page);
			await signInAndWait(page, creds);
			await page.goto('/discounts');
			await expect(page.getByRole('heading', { name: 'Discounts' })).toBeVisible();
			await expect(openForm(page)).toHaveCount(0);
		}
	});

	test('the endpoint itself refuses a CFO, not just the button', async ({ page, tenantCfo }) => {
		// A hidden button proves nothing about the gate.
		await page.goto('/');
		await signOut(page);
		await signInAndWait(page, tenantCfo);
		const resp = await page.request.post(`${API_BASE}/api/discounts/bulk-negotiate`, {
			headers: await authedTenantHeaders(page),
			data: {
				vendor_id: '00000000-0000-0000-0000-000000000001',
				tiers: [{ days: 7, percent: '2.00' }]
			}
		});
		// 403 before the vendor lookup — never a 404 that would confirm the id.
		// And the ROLE refusal: a 403 for a token from another workspace
		// (`token_tenant_mismatch`) would pass a bare status check while proving
		// nothing about the gate — which is what this test did while it signed
		// in acme's CFO on the worker tenant's page.
		expect(resp.status()).toBe(403);
		expect(((await resp.json()) as { detail?: unknown }).detail).toBe(
			'Your role does not permit this action.'
		);
	});
});
