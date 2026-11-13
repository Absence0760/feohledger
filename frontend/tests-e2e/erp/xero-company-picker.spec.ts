import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	test
} from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import { FAKE_ERP_BASE } from './helpers';

/**
 * The ERP company picker (decisions §355) — a Xero consent that authorised
 * several organisations, through the REAL OAuth flow
 * (backend/app/api/erp_oauth.py) against fake-erp's stub Xero (`pnpm erp:up`).
 *
 * fake-erp's `POST /xero/__organisations {state, count}` arms ONE consent (by
 * its `state`) to authorise several organisations, so a Xero connect in a
 * parallel worker is untouched. The callback must then link nothing and
 * return with `?erp_choose_company=xero`; the settings page offers the list
 * the server holds and links only the company the admin picks.
 *
 * The browser-binding cookie (§336) rides in this context's cookie jar:
 * `page.request` sets it at `/authorize` and carries it to the callback, and
 * the page's own credentialed fetches carry it to the picker routes, the way
 * the admin's browser does.
 *
 * Coverage:
 *   1. Pick — the picker lists both organisations as a named radio group;
 *      choosing the second connects exactly that one.
 *   2. Cancel — nothing is connected, and the pending connect is gone.
 *   3. Reload — the return parameter is stripped on mount, so the picker is
 *      found again by reading the parked consent on EVERY admin mount, not
 *      only when `?erp_choose_company=` is in the address.
 *   4. Nothing parked — a plain visit says nothing (the read's 409 is the
 *      normal case there, not a failure).
 *   5. axe — the picker at desktop width and at 320px (WCAG 1.4.10).
 */

const PROVIDER = 'xero';
const SECOND = 'fake-xero-tenant-2';

/** Authorize → consent (armed for two organisations) → callback. Returns the
 *  address the callback sent the browser to. */
async function consentToTwoOrganisations(page: Page): Promise<string> {
	const headers = await authedTenantHeaders(page);
	const start = await page.request.get(
		`${API_BASE}/api/organization/erp/oauth/${PROVIDER}/authorize`,
		{ headers }
	);
	expect(start.status(), await start.text()).toBe(200);
	const { authorize_url } = await start.json();
	const state = new URL(authorize_url).searchParams.get('state');
	expect(state).toBeTruthy();
	const armed = await page.request.post(`${FAKE_ERP_BASE}/xero/__organisations`, {
		data: { state, count: 2 }
	});
	expect(armed.ok(), await armed.text()).toBe(true);

	const consent = await page.request.get(authorize_url, { maxRedirects: 0 });
	expect(consent.status(), await consent.text()).toBe(302);
	const landed = await page.request.get(consent.headers()['location'], {
		maxRedirects: 0
	});
	expect(landed.status()).toBe(302);
	const home = new URL(landed.headers()['location']);
	expect(home.pathname).toBe('/organization');
	expect(home.searchParams.get('erp_error')).toBeNull();
	expect(home.searchParams.get('erp_choose_company')).toBe(PROVIDER);
	return home.href;
}

async function oauthStatus(page: Page) {
	const resp = await page.request.get(`${API_BASE}/api/organization/erp/oauth/status`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.ok()).toBe(true);
	return resp.json();
}

async function disconnect(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	await page.request.post(`${API_BASE}/api/organization/erp/oauth/disconnect`, {
		headers
	});
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: { ...headers, 'Content-Type': 'application/json' },
		data: { settings: { erp: null } }
	});
}

test.describe('/erp company picker against fake-erp Xero', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		await disconnect(page);
	});

	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({
			baseURL: tenantBase(currentTenantSlug())
		});
		const page = await context.newPage();
		await signInAndWait(page);
		await disconnect(page);
		await context.close();
	});

	test('a consent to two organisations links the one the admin picks', async ({ page }) => {
		const landing = await consentToTwoOrganisations(page);
		// Nothing is linked until the pick.
		expect((await oauthStatus(page)).connected).toBe(false);

		await page.goto(landing);
		const picker = page.getByTestId('erp-company-picker');
		await expect(picker).toBeVisible();
		const group = picker.getByRole('group', {
			name: 'Choose the Xero company to connect'
		});
		await expect(group.getByRole('radio')).toHaveCount(2);
		// The return parameter is spent from the address bar.
		await expect(page).not.toHaveURL(/erp_choose_company/);

		// Submitting with nothing chosen says so instead of connecting.
		await picker.getByTestId('erp-company-connect').click();
		await expect(picker.getByRole('alert')).toHaveText('Choose a company first.');

		await group.getByRole('radio', { name: /Fake Xero Org 2/ }).check();
		await picker.getByTestId('erp-company-connect').click();
		await expect(page.getByTestId('erp-status')).toHaveText('Xero is now connected.');
		await expect(picker).toHaveCount(0);
		// Focus lands on the outcome, not the page body (WCAG 2.4.3).
		await expect(page.getByTestId('erp-status')).toBeFocused();

		const status = await oauthStatus(page);
		expect(status.connected).toBe(true);
		expect(status.provider).toBe(PROVIDER);
		expect(status.external_tenant_id).toBe(SECOND);
	});

	test('cancelling the pick connects nothing', async ({ page }) => {
		const landing = await consentToTwoOrganisations(page);
		await page.goto(landing);
		const picker = page.getByTestId('erp-company-picker');
		await expect(picker).toBeVisible();
		await picker.getByTestId('erp-company-cancel').click();
		await expect(page.getByTestId('erp-status')).toHaveText('Nothing was connected to Xero.');
		await expect(picker).toHaveCount(0);

		expect((await oauthStatus(page)).connected).toBe(false);
		// The pending connect is gone: a read from this browser context (its
		// cookie jar holds the binding cookie) finds nothing to pick.
		const gone = await page.request.get(`${API_BASE}/api/organization/erp/oauth/pending`, {
			headers: await authedTenantHeaders(page)
		});
		expect(gone.status()).toBe(409);
		expect((await gone.json()).detail.code).toBe('erp_pick_expired');
	});

	test('a reload keeps the picker while the consent is still parked', async ({ page }) => {
		const landing = await consentToTwoOrganisations(page);
		await page.goto(landing);
		const picker = page.getByTestId('erp-company-picker');
		await expect(picker).toBeVisible();
		await expect(page).not.toHaveURL(/erp_choose_company/);

		// The parameter is gone from the address; the server still holds the
		// consent, and the reloaded panel finds it.
		await page.reload();
		await expect(picker).toBeVisible();
		await expect(picker.getByRole('radio')).toHaveCount(2);

		// Leaving and coming back (a plain /organization, no parameter) too.
		await page.goto('/organization');
		await expect(picker).toBeVisible();
		await picker.getByTestId('erp-company-cancel').click();
		await expect(picker).toHaveCount(0);
	});

	test('a plain visit with nothing parked shows no picker and no failure', async ({ page }) => {
		const read = page.waitForResponse((r) =>
			r.url().includes('/api/organization/erp/oauth/pending')
		);
		await page.goto('/organization');
		expect((await read).status()).toBe(409);
		await expect(page.getByTestId('erp-company-picker')).toHaveCount(0);
		// The status line (always mounted, a live region) stays empty.
		await expect(page.getByTestId('erp-status')).toHaveText('');
	});

	test('the picker has no axe violations at desktop width or at 320px', async ({ page }) => {
		const landing = await consentToTwoOrganisations(page);
		await page.goto(landing);
		const picker = page.getByTestId('erp-company-picker');
		await expect(picker).toBeVisible();
		await expectNoA11yViolations(page);

		await page.setViewportSize({ width: 320, height: 720 });
		await expect(picker).toBeVisible();
		await expectNoA11yViolations(page);
		const overflow = await page.evaluate(
			() => document.documentElement.scrollWidth - document.documentElement.clientWidth
		);
		expect(overflow, 'the picker scrolls the page sideways at 320px').toBeLessThanOrEqual(1);

		await picker.getByTestId('erp-company-cancel').click();
		await expect(picker).toHaveCount(0);
	});
});
