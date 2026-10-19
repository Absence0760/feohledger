import { test, expect, type Browser, type BrowserContext, type Page } from '@playwright/test';

import { API_BASE, TENANT_ROOT_URL } from '../fixtures/env';
import {
	REAL,
	STATE_FILE,
	awaitEmail,
	expectNoPageOverflow,
	inboxIds,
	readTenant,
	totpCode,
	totpStep,
	ublInvoice,
	type DayOneTenant
} from './fixtures';

/**
 * Day one (#575): what the operator did by hand on the first production deploy
 * (#566), against an install a deploy has just built from an empty database —
 * see `playwright.config.ts` in this directory for how that stack is made.
 *
 * No seed data, no SQL, no fixture tenant: the only things that exist when this
 * starts are what `deploy/add-tenant.sh` creates — an organization and its
 * admin, holding a generated temporary password. Every other row is made
 * through the app, by the people who would make it:
 *
 *   1. the admin signs in with the temporary password, is forced to change it,
 *      and enrols TOTP;
 *   2. the admin invites an AP Manager, whose invitation arrives by email;
 *   3. the admin uploads the first invoice — a tax-exclusive e-invoice from a
 *      supplier nobody has set up, so the vendor is auto-created unverified —
 *      and it references a purchase order that does not exist;
 *   4. the AP Manager signs in (forced change), clears the PO flag, verifies
 *      the supplier, approves the invoice and records it paid outside
 *      FeohLedger;
 *   5. the admin resets the AP Manager's password, and the AP Manager's next
 *      sign-in is forced to change it; the new password then works;
 *   6. the admin signs back in, through the TOTP challenge.
 *
 * Two browser contexts, one per person, for the whole journey — the way two
 * people on two laptops would do it, and it means the admin signs in with a
 * password and a TOTP code exactly twice.
 */

const tenant = readTenant();

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
	if (tenant) return;
	const hint = `no day-one stack (${STATE_FILE} is missing) — run scripts/day-one/stack.sh all first`;
	if (process.env.FEOH_REQUIRE_INTEGRATION) {
		throw new Error(`${hint}; FEOH_REQUIRE_INTEGRATION is set, refusing to skip`);
	}
	test.skip(true, hint);
});

// Carried from step to step. The journey is one story; each test is a chapter.
let admin: Page;
let manager: Page;
let contexts: BrowserContext[] = [];
let mfaSecret = '';
let mfaStepUsed = -1;
let invoiceId = '';
let vendorId = '';
let managerPassword = '';

const ADMIN_PASSWORD = 'Day-One-Owner-Passw0rd';
const MANAGER_PASSWORD = 'Day-One-Manager-Passw0rd';
const MANAGER_PASSWORD_AFTER_RESET = 'After-Reset-Manager-Passw0rd';

async function person(browser: Browser): Promise<Page> {
	const context = await browser.newContext();
	contexts.push(context);
	return context.newPage();
}

test.beforeAll(async ({ browser }) => {
	if (!tenant) return;
	admin = await person(browser);
	manager = await person(browser);
});

test.afterAll(async () => {
	for (const context of contexts) await context.close();
	contexts = [];
});

function t(): DayOneTenant {
	return tenant!;
}

function api(path: string): string {
	return `${API_BASE}${path}`;
}

async function headers(page: Page): Promise<Record<string, string>> {
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	expect(token, 'signed in').toBeTruthy();
	return { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': t().slug };
}

/** A first visit, as a person makes it: the consent banner is answered, not
 *  pre-dismissed through storage. */
async function answerConsent(page: Page): Promise<void> {
	const banner = page.getByRole('region', { name: 'Cookie and privacy consent' });
	await expect(banner).toBeVisible();
	await banner.getByRole('button', { name: 'Accept all' }).click();
	await expect(banner).toBeHidden();
}

async function submitLogin(page: Page, email: string, password: string): Promise<void> {
	await page.goto('/login');
	await page.locator('input[type="email"]').fill(email);
	await page.locator('input[type="password"]').fill(password);
	await page.locator('form button[type="submit"]').click();
}

/** /change-password, filled the way the form asks. Lands on the tenant root. */
async function changePassword(page: Page, current: string, next: string): Promise<void> {
	await expect(page).toHaveURL(/\/change-password$/);
	await page.locator('input[autocomplete="current-password"]').fill(current);
	const fresh = page.locator('input[autocomplete="new-password"]');
	await fresh.first().fill(next);
	await fresh.last().fill(next);
	const changed = page.waitForResponse(
		(r) => r.url().endsWith('/api/auth/change-password') && r.request().method() === 'POST'
	);
	await page.getByRole('button', { name: 'Change password' }).click();
	const resp = await changed;
	expect(resp.status()).toBe(200);
	expect(((await resp.json()) as { must_change_password: boolean }).must_change_password).toBe(
		false
	);
	await page.waitForURL(TENANT_ROOT_URL);
}

async function signOut(page: Page): Promise<void> {
	await page.locator('.profile-btn').click();
	await page.locator('.profile-logout').click();
	await expect(page).toHaveURL(/\/login/);
}

test('the deploy left a working, empty install with signup closed', async () => {
	const health = await admin.request.get(api('/api/health'));
	expect(health.status()).toBe(200);

	// FEOH_SIGNUP_ENABLED=false, as the production env sets it: the SPA is told
	// signup is closed rather than offered a form that can only be refused.
	const config = await admin.request.get(api('/api/public-config'));
	expect(config.status()).toBe(200);
	expect(((await config.json()) as { signup_enabled: boolean }).signup_enabled).toBe(false);

	// The tenant host serves its sign-in page.
	await admin.goto('/login');
	await expect(admin.locator('input[type="email"]')).toBeVisible();
	await answerConsent(admin);
});

test('first admin: temporary password, forced change, TOTP enrolment', async () => {
	await submitLogin(admin, t().admin_email, t().admin_temp_password);
	// add-tenant.sh generated the password, so the first sign-in must change it.
	await changePassword(admin, t().admin_temp_password, ADMIN_PASSWORD);

	await admin.goto('/profile?section=mfa');
	await expect(admin.getByText('Not configured')).toBeVisible();
	const enroll = admin.waitForResponse(
		(r) => r.url().endsWith('/api/auth/mfa/enroll') && r.request().method() === 'POST'
	);
	await admin.getByRole('button', { name: /Set up two-factor/ }).click();
	const enrolled = await enroll;
	expect(enrolled.status()).toBe(200);
	mfaSecret = ((await enrolled.json()) as { secret: string }).secret;
	await expect(admin.locator('img[alt="MFA QR code"]')).toBeVisible();

	mfaStepUsed = totpStep();
	await admin.locator('input[autocomplete="one-time-code"]').fill(totpCode(mfaSecret, mfaStepUsed));
	await admin.getByRole('button', { name: /Verify and enable/ }).click();
	await expect(admin.getByText('Enabled', { exact: true })).toBeVisible();
});

test('admin invites an AP Manager, whose invitation arrives by email', async () => {
	await admin.goto('/admin');
	await admin.getByRole('button', { name: '+ Invite User' }).click();
	const modal = admin.locator('div.modal[role="dialog"][aria-label="Invite user"]');
	await expect(modal).toBeVisible();
	await modal.locator('input[type="text"]').fill(REAL.managerName);
	await modal.locator('input[type="email"]').fill(REAL.managerEmail);
	await modal.locator('.check-label', { hasText: 'AP Manager' }).locator('input').check();

	const created = admin.waitForResponse(
		(r) => r.url().endsWith('/api/admin/users') && r.request().method() === 'POST'
	);
	await modal.getByRole('button', { name: /Create User/ }).click();
	expect((await created).status()).toBe(201);

	const credentials = admin.locator('div.modal[role="dialog"][aria-label="User created"]');
	await expect(credentials).toBeVisible();
	const shown = (await credentials.locator('.credential-value.password').textContent())?.trim();
	await credentials.getByRole('button', { name: /^Done$/ }).click();
	await expect(admin.locator('table tbody td.email-cell', { hasText: REAL.managerEmail })).toBeVisible();
	await expectNoPageOverflow(admin);

	// What the manager actually receives: the tenant's own sign-in URL and the
	// same temporary password the admin was shown.
	const invite = await awaitEmail(admin, t().mailpit_url, REAL.managerEmail, /invited/i);
	expect(invite.text).toContain(new URL(admin.url()).origin);
	managerPassword = /Password:\s+(\S+)/.exec(invite.text)?.[1] ?? '';
	expect(managerPassword).not.toBe('');
	expect(managerPassword).toBe(shown);
});

test('a provider secret is sealed under the KMS key, not the development key', async () => {
	// A deployed environment refuses the local key outright (credential_crypto),
	// so a save that succeeds here went through KMS — LocalStack's, standing in
	// for infra/'s app key under the same kind of alias.
	const saved = await admin.request.put(api('/api/organization/credentials/erp'), {
		headers: await headers(admin),
		data: { set: { webhook_signing_secret: 'day-one-erp-webhook-signing-secret-0001' }, clear: [] }
	});
	expect(saved.status(), await saved.text()).toBe(200);
	const status = await admin.request.get(api('/api/organization/credentials'), {
		headers: await headers(admin)
	});
	expect(((await status.json()) as { erp: string[] }).erp).toContain('webhook_signing_secret');
});

test('admin uploads the first invoice: a tax-exclusive e-invoice from a new supplier', async () => {
	await admin.goto('/invoices');
	const empty = admin.getByTestId('invoices-empty-state');
	await expect(empty).toBeVisible();
	// The picker must offer the file at all — a browser filters its dialog by
	// `accept`, which once left `.xml` e-invoices unpickable.
	await expect(admin.locator('input[type="file"]')).toHaveAttribute('accept', /(^|,)\.xml(,|$)/);

	const chooser = admin.waitForEvent('filechooser');
	await empty.getByRole('button').click();
	const uploaded = admin.waitForResponse(
		(r) => r.url().endsWith('/api/invoices/upload') && r.request().method() === 'POST'
	);
	await (await chooser).setFiles({
		name: 'ubl-invoice.xml',
		mimeType: 'application/xml',
		buffer: ublInvoice()
	});
	const resp = await uploaded;
	expect(resp.status()).toBe(202);
	invoiceId = ((await resp.json()) as { id: string }).id;

	// Extraction runs in the background; the row reaching review is the signal.
	const row = admin.locator('table tbody tr', { hasText: REAL.invoiceNumber });
	await expect(row.locator('.badge')).toHaveText('Ready for Review');
	await expect(row).toContainText(REAL.vendorName);
	await expectNoPageOverflow(admin);

	const invoice = await admin.request.get(api(`/api/invoices/${invoiceId}`), {
		headers: await headers(admin)
	});
	const body = (await invoice.json()) as {
		subtotal: number | string;
		tax_amount: number | string;
		amount: number | string;
		po_number: string;
		vendor_id: string;
		warnings: { type: string; code: string }[];
	};
	// The header carries subtotal + tax + total, as the document states them.
	expect(Number(body.subtotal).toFixed(2)).toBe(REAL.subtotal);
	expect(Number(body.tax_amount).toFixed(2)).toBe(REAL.taxAmount);
	expect(Number(body.amount).toFixed(2)).toBe(REAL.total);
	expect(body.po_number).toBe(REAL.poNumber);
	vendorId = body.vendor_id;
	// #572: net lines plus header tax reconcile, so the tax-exclusive invoice
	// raises no line-total finding — only the two real ones: a supplier no one
	// has verified, and a purchase order the tenant does not have.
	expect(body.warnings.map((w) => w.type).sort()).toEqual(['po_mismatch', 'unverified_vendor']);

	await admin.goto('/vendors');
	const vendorRow = admin.locator('table tbody tr', { hasText: REAL.vendorName });
	await expect(vendorRow.getByRole('button', { name: 'Verify' })).toBeVisible();
	await expectNoPageOverflow(admin);
});

test('AP Manager: first sign-in forces a change, then the PO flag is cleared', async () => {
	await manager.goto('/login');
	await answerConsent(manager);
	await submitLogin(manager, REAL.managerEmail, managerPassword);
	await changePassword(manager, managerPassword, MANAGER_PASSWORD);

	await manager.goto('/exceptions');
	const row = manager.locator('table tbody tr', { hasText: 'PO Mismatch' }).filter({
		hasText: REAL.invoiceNumber
	});
	await expect(row).toBeVisible();
	await expectNoPageOverflow(manager);

	await row.getByRole('button', { name: 'Resolve' }).click();
	const dialog = manager
		.getByRole('dialog')
		.filter({ has: manager.getByTestId('exception-resolve-form') });
	await expect(dialog).toBeVisible();
	await dialog.locator('input[type="text"]').fill(`${REAL.poNumber} was a verbal order, confirmed with operations`);
	const resolved = manager.waitForResponse(
		(r) => /\/api\/exceptions\/[^/]+\/resolve$/.test(r.url()) && r.request().method() === 'POST'
	);
	await dialog.getByRole('button', { name: 'Resolve', exact: true }).click();
	const resp = await resolved;
	expect(resp.status()).toBe(200);
	expect(((await resp.json()) as { status: string }).status).toBe('resolved');
	await expect(dialog).toBeHidden();
});

test('AP Manager verifies the new supplier', async () => {
	await manager.goto('/vendors');
	const row = manager.locator('table tbody tr', { hasText: REAL.vendorName });
	const verified = manager.waitForResponse(
		(r) => r.url().endsWith(`/api/vendors/${vendorId}/verify`) && r.request().method() === 'POST'
	);
	await row.getByRole('button', { name: 'Verify' }).click();
	expect((await verified).status()).toBe(200);
	await expect(row.getByRole('button', { name: 'Verify' })).toHaveCount(0);
});

test('AP Manager approves the invoice', async () => {
	await manager.goto('/invoices');
	await manager.getByRole('button', { name: `Edit invoice ${REAL.invoiceNumber}` }).click();
	const modal = manager.locator('div.modal[role="dialog"]');
	await expect(modal).toBeVisible();
	const approved = manager.waitForResponse(
		(r) => r.url().endsWith(`/api/invoices/${invoiceId}/approve`) && r.request().method() === 'POST'
	);
	await modal.getByRole('button', { name: /^Approve$/ }).click();
	expect((await approved).status()).toBe(200);
	await expect(modal).toBeHidden();
	await expect(
		manager.locator('table tbody tr', { hasText: REAL.invoiceNumber }).locator('.badge')
	).toHaveText('Approved');
});

test('AP Manager records it paid outside FeohLedger', async () => {
	await manager.goto('/payments');
	const row = manager.locator('table tbody tr', { hasText: REAL.invoiceNumber });
	await expect(row).toBeVisible();
	await expectNoPageOverflow(manager);
	await row.getByRole('button', { name: `Record as paid — invoice ${REAL.invoiceNumber}` }).click();

	const dialog = manager.getByRole('dialog', { name: 'Record a payment made outside FeohLedger' });
	await expect(dialog).toBeVisible();
	await dialog.getByTestId('record-method').selectOption('ach');
	await dialog.getByTestId('record-reference').fill('ACH-20261019-000123');
	const recorded = manager.waitForResponse(
		(r) => r.url().endsWith('/api/payments/record-outside') && r.request().method() === 'POST'
	);
	await dialog.getByTestId('record-confirm').click();
	const resp = await recorded;
	expect(resp.status()).toBe(201);
	expect(((await resp.json()) as { status: string; provider: string })).toMatchObject({
		status: 'completed',
		provider: 'external'
	});
	await expect(dialog).toBeHidden();
	await expect(manager.locator('table tbody tr', { hasText: REAL.invoiceNumber })).toHaveCount(0);

	await manager.getByRole('tab', { name: 'History' }).click();
	const history = manager.locator('table tbody tr', { hasText: REAL.invoiceNumber });
	await expect(history.getByTestId('payment-external')).toHaveText('Paid outside FeohLedger');
	await expectNoPageOverflow(manager);

	const invoice = await manager.request.get(api(`/api/invoices/${invoiceId}`), {
		headers: await headers(manager)
	});
	expect(((await invoice.json()) as { status: string }).status).toBe('paid');
});

test("admin resets the AP Manager's password; the next sign-in must change it", async () => {
	await admin.goto('/admin');
	const row = admin.locator('table tbody tr', { hasText: REAL.managerEmail });
	await row.getByRole('button', { name: 'Edit' }).click();
	const modal = admin.locator('div.modal[role="dialog"][aria-label="Edit user"]');
	await expect(modal.locator('input[type="email"]')).toHaveValue(REAL.managerEmail);
	// No password input to autofill: the reset issues a generated one (#566).
	await expect(modal.locator('input[type="password"]')).toHaveCount(0);

	const before = await inboxIds(admin, t().mailpit_url);
	await modal.getByRole('button', { name: 'Reset password' }).click();
	const reset = admin.waitForResponse(
		(r) => r.url().endsWith('/reset-password') && r.request().method() === 'POST'
	);
	await modal.getByRole('button', { name: 'Confirm reset' }).click();
	expect((await reset).status()).toBe(200);
	const credentials = admin.locator('div.modal[role="dialog"][aria-label="Password reset"]');
	await expect(credentials).toBeVisible();
	const temp = (await credentials.locator('.credential-value.password').textContent())?.trim() ?? '';
	expect(temp.length).toBeGreaterThanOrEqual(12);
	await credentials.getByRole('button', { name: /^Done$/ }).click();
	// The manager is told too, with the same temporary password.
	const notice = await awaitEmail(admin, t().mailpit_url, REAL.managerEmail, /.*/, before);
	expect(/Password:\s+(\S+)/.exec(notice.text)?.[1]).toBe(temp);

	// The manager's own password stopped working the moment it was reset.
	await signOut(manager);
	await submitLogin(manager, REAL.managerEmail, MANAGER_PASSWORD);
	await expect(manager.locator('.error, [role="alert"]').first()).toBeVisible();
	await expect(manager).toHaveURL(/\/login$/);

	await submitLogin(manager, REAL.managerEmail, temp);
	await changePassword(manager, temp, MANAGER_PASSWORD_AFTER_RESET);

	await signOut(manager);
	await submitLogin(manager, REAL.managerEmail, MANAGER_PASSWORD_AFTER_RESET);
	await manager.waitForURL(TENANT_ROOT_URL);
});

test('admin signs back in through the TOTP challenge', async () => {
	await signOut(admin);
	await submitLogin(admin, t().admin_email, ADMIN_PASSWORD);
	await admin.waitForURL(/\/login\/mfa$/);

	// A code is single-use, and the enrolment spent the one for its step. The
	// server accepts one step either side, so the next step's code is valid now
	// and is not the spent one — no waiting on the clock.
	const step = totpStep() === mfaStepUsed ? mfaStepUsed + 1 : totpStep();
	await admin.locator('input[autocomplete="one-time-code"]').fill(totpCode(mfaSecret, step));
	await admin.getByRole('button', { name: /Verify/ }).click();
	await admin.waitForURL(TENANT_ROOT_URL);
});
