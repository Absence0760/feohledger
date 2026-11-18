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
 * admin, with no password and a set-password link in their inbox. Every other
 * row is made through the app, by the people who would make it:
 *
 *   1. the admin sets a password from the emailed link (the operator never saw
 *      a credential), signs in and enrols TOTP;
 *   2. the admin invites an AP Manager and a financial controller (a second
 *      AP Manager), whose invitations arrive by email as set-password links
 *      (no temporary password, decisions §283);
 *   3. the admin uploads the first invoice — a tax-exclusive e-invoice from a
 *      supplier nobody has set up, so the vendor is auto-created unverified —
 *      and it references a purchase order that does not exist;
 *   4. the controller sets a password from the link and clears the PO flag. It is a
 *      third person's call: the admin uploaded the invoice and the AP Manager
 *      will approve it, and an approver who cleared the hold would have it
 *      re-raised by their own approval (decisions §420, §422);
 *   5. the AP Manager sets a password from the link, verifies the supplier,
 *      approves the invoice and records it paid outside FeohLedger;
 *   6. the admin resets the AP Manager's password: the old one stops working,
 *      a new set-password link arrives by email, and the password it sets
 *      works;
 *   7. the admin signs back in, through the TOTP challenge.
 *
 * Three browser contexts, one per person, for the whole journey — the way two
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
let controller: Page;
let contexts: BrowserContext[] = [];
let mfaSecret = '';
let mfaStepUsed = -1;
let invoiceId = '';
let vendorId = '';
let managerLink = '';
let controllerLink = '';

const ADMIN_PASSWORD = 'Day-One-Owner-Passw0rd';
const MANAGER_PASSWORD = 'Day-One-Manager-Passw0rd';
const CONTROLLER_PASSWORD = 'Day-One-Controller-Passw0rd';
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
	controller = await person(browser);
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

/** The set-password link from an email: the reset page, filled the way it
 *  asks, then a real sign-in with the password it set. Lands on the tenant root. */
async function setPasswordFromLink(page: Page, link: string, email: string, password: string) {
	await page.goto(link);
	const fresh = page.locator('input[autocomplete="new-password"]');
	await expect(fresh.first()).toBeVisible();
	await fresh.first().fill(password);
	await fresh.last().fill(password);
	const redeemed = page.waitForResponse(
		(r) => r.url().endsWith('/api/auth/reset-password') && r.request().method() === 'POST'
	);
	await page.locator('form button[type="submit"]').click();
	expect((await redeemed).status()).toBe(200);
	await page.getByRole('link', { name: 'Go to sign in' }).click();
	await submitLogin(page, email, password);
	await page.waitForURL(TENANT_ROOT_URL);
}

/** The one set-password link in an email's text. */
function setPasswordLink(text: string): string {
	const link = /https?:\/\/\S+\/login\/reset-password\?\S+/.exec(text)?.[0] ?? '';
	expect(link).not.toBe('');
	return link;
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

test('first admin: set-password link by email, TOTP enrolment', async () => {
	// add-tenant.sh created the admin with no password and emailed them the
	// same single-use link an invite sends — nothing for the operator to relay.
	const welcome = await awaitEmail(
		admin,
		t().mailpit_url,
		t().admin_email,
		/workspace is ready/i
	);
	expect(welcome.text).not.toMatch(/Password:/);
	const link = setPasswordLink(welcome.text);
	expect(new URL(link).origin).toBe(new URL(admin.url()).origin);
	expect(new URL(link).searchParams.get('mode')).toBe('set');
	await setPasswordFromLink(admin, link, t().admin_email, ADMIN_PASSWORD);

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

/** The admin invites `name` as an AP Manager from /admin; returns the
 *  set-password link from the email they receive, after checking the admin was
 *  shown no credential and that the link is on the tenant's own host. */
async function inviteApManager(name: string, email: string): Promise<string> {
	await admin.goto('/admin');
	await admin.getByRole('button', { name: '+ Invite User' }).click();
	const modal = admin.locator('div.modal[role="dialog"][aria-label="Invite user"]');
	await expect(modal).toBeVisible();
	await modal.locator('input[type="text"]').fill(name);
	await modal.locator('input[type="email"]').fill(email);
	await modal.locator('.check-label', { hasText: 'AP Manager' }).locator('input').check();

	const created = admin.waitForResponse(
		(r) => r.url().endsWith('/api/admin/users') && r.request().method() === 'POST'
	);
	await modal.getByRole('button', { name: /Create User/ }).click();
	const resp = await created;
	expect(resp.status()).toBe(201);
	expect(await resp.json()).not.toHaveProperty('temporary_password');

	const sent = admin.locator('div.modal[role="dialog"][aria-label="Invitation sent"]');
	await expect(sent).toBeVisible();
	await expect(sent).toContainText(email);
	await sent.getByRole('button', { name: /^Done$/ }).click();
	await expect(admin.locator('table tbody td.email-cell', { hasText: email })).toBeVisible();
	await expectNoPageOverflow(admin);

	// What the invitee actually receives: a single-use link on the tenant's
	// own host, and no password at all.
	const invite = await awaitEmail(admin, t().mailpit_url, email, /invited/i);
	expect(invite.text).not.toMatch(/Password:/);
	const link = setPasswordLink(invite.text);
	expect(new URL(link).origin).toBe(new URL(admin.url()).origin);
	expect(new URL(link).searchParams.get('mode')).toBe('set');
	return link;
}

test('admin invites an AP Manager, whose invitation arrives by email', async () => {
	managerLink = await inviteApManager(REAL.managerName, REAL.managerEmail);
});

// A third person, so the PO flag can be cleared by someone who neither
// uploaded nor approves the invoice (decisions §420, §422).
test('admin invites a financial controller, whose invitation arrives by email', async () => {
	controllerLink = await inviteApManager(REAL.controllerName, REAL.controllerEmail);
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

test('controller: sets a password from the emailed link, then the PO flag is cleared', async () => {
	await controller.goto('/login');
	await answerConsent(controller);
	await setPasswordFromLink(controller, controllerLink, REAL.controllerEmail, CONTROLLER_PASSWORD);

	await controller.goto('/exceptions');
	const row = controller.locator('table tbody tr', { hasText: 'PO Mismatch' }).filter({
		hasText: REAL.invoiceNumber
	});
	await expect(row).toBeVisible();
	await expectNoPageOverflow(controller);

	await row.getByRole('button', { name: 'Resolve' }).click();
	const dialog = controller
		.getByRole('dialog')
		.filter({ has: controller.getByTestId('exception-resolve-form') });
	await expect(dialog).toBeVisible();
	await dialog.locator('input[type="text"]').fill(`${REAL.poNumber} was a verbal order, confirmed with operations`);
	const resolved = controller.waitForResponse(
		(r) => /\/api\/exceptions\/[^/]+\/resolve$/.test(r.url()) && r.request().method() === 'POST'
	);
	await dialog.getByRole('button', { name: 'Resolve', exact: true }).click();
	const resp = await resolved;
	expect(resp.status()).toBe(200);
	expect(((await resp.json()) as { status: string }).status).toBe('resolved');
	await expect(dialog).toBeHidden();
});

test('AP Manager: sets a password from the emailed link', async () => {
	await manager.goto('/login');
	await answerConsent(manager);
	await setPasswordFromLink(manager, managerLink, REAL.managerEmail, MANAGER_PASSWORD);
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

test("admin resets the AP Manager's password; a new link sets the next one", async () => {
	await admin.goto('/admin');
	const row = admin.locator('table tbody tr', { hasText: REAL.managerEmail });
	await row.getByRole('button', { name: 'Edit' }).click();
	const modal = admin.locator('div.modal[role="dialog"][aria-label="Edit user"]');
	await expect(modal.locator('input[type="email"]')).toHaveValue(REAL.managerEmail);
	// No password input to autofill: the reset never takes a typed one (#566).
	await expect(modal.locator('input[type="password"]')).toHaveCount(0);

	const before = await inboxIds(admin, t().mailpit_url);
	await modal.getByRole('button', { name: 'Reset password' }).click();
	const reset = admin.waitForResponse(
		(r) => r.url().endsWith('/reset-password') && r.request().method() === 'POST'
	);
	await modal.getByRole('button', { name: 'Confirm reset' }).click();
	const resp = await reset;
	expect(resp.status()).toBe(200);
	expect(await resp.json()).not.toHaveProperty('temporary_password');
	const sent = admin.locator('div.modal[role="dialog"][aria-label="Password reset"]');
	await expect(sent).toBeVisible();
	await expect(sent).toContainText(REAL.managerEmail);
	await sent.getByRole('button', { name: /^Done$/ }).click();
	// Only the manager gets the way back in: a link, in their own inbox.
	const notice = await awaitEmail(admin, t().mailpit_url, REAL.managerEmail, /.*/, before);
	expect(notice.text).not.toMatch(/Password:/);
	const link = setPasswordLink(notice.text);

	// The manager's own password stopped working the moment it was reset.
	await signOut(manager);
	await submitLogin(manager, REAL.managerEmail, MANAGER_PASSWORD);
	await expect(manager.locator('.error, [role="alert"]').first()).toBeVisible();
	await expect(manager).toHaveURL(/\/login$/);

	await setPasswordFromLink(manager, link, REAL.managerEmail, MANAGER_PASSWORD_AFTER_RESET);
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
