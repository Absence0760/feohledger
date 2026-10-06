import type { Page, Route } from '@playwright/test';

import { API_BASE, currentTenantSlug, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /admin/privacy — an UNMASKED-banking DSAR export collects a second factor.
 *
 * `POST /api/privacy/dsar` with `include_banking` demands a second-factor proof
 * (`api/auth.require_sensitive_step_up`): a current authenticator code or a
 * passkey assertion, never the password. The server answers
 * `sensitive_step_up_required` when none was sent, and the page reacts to that
 * by opening the step-up prompt and resending with the proof.
 *
 * The e2e backend runs with the MFA master switch off (`FEOH_MFA_ENABLED=false`
 * in `backend/.env.development`), where the gate is skipped and the audit row
 * says `mfa_off_local` — so the first test drives the real export end to end,
 * and the prompt's behaviour is pinned by answering the DSAR call with the
 * refusals a switch-on backend sends. What is under test is the proof the page
 * SENDS and how it reads each verdict; the server's verdict on a proof is
 * pytest's (`backend/tests/test_privacy_unmasked_step_up.py`).
 */

const JUSTIFICATION = 'Supplier asked for their own record, ticket AP-7';

async function createVendor(page: Page): Promise<string> {
	const token = await page.evaluate(() => localStorage.getItem('auth_token'));
	const name = `E2E Unmasked Vendor ${Date.now()}`;
	const resp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers: {
			Authorization: `Bearer ${token}`,
			'X-Tenant-Slug': currentTenantSlug(),
			'Content-Type': 'application/json'
		},
		data: { name, email: `unmasked.${Date.now()}@example.test` }
	});
	expect(resp.ok()).toBe(true);
	return ((await resp.json()) as { id: string }).id;
}

async function fillUnmaskedExport(page: Page, vendorId: string): Promise<void> {
	await page.goto('/admin/privacy');
	await page.getByLabel('Subject type').selectOption('vendor_contact');
	await page.getByLabel('Identifier').fill(vendorId);
	await page.getByTestId('dsar-include-banking').check();
	await page.getByLabel('Reason for the unmasked export').fill(JUSTIFICATION);
}

function refusal(route: Route, status: number, code: string, message: string) {
	return route.fulfill({ status, json: { detail: { code, message, params: {} } } });
}

const isDsar = (url: URL) => url.pathname === '/api/privacy/dsar';

test.describe('/admin/privacy — unmasked export step-up (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('the option is offered only for a vendor contact, and needs a reason', async ({ page }) => {
		await page.goto('/admin/privacy');
		await page.getByLabel('Identifier').fill('someone@example.test');
		// A user subject has no bank details, so there is nothing to unmask.
		await expect(page.getByRole('button', { name: 'Export data (DSAR)' })).toBeEnabled();
		await expect(page.getByTestId('dsar-include-banking')).toHaveCount(0);

		await page.getByLabel('Subject type').selectOption('vendor_contact');
		await page.getByTestId('dsar-include-banking').check();
		const exportButton = page.getByRole('button', { name: 'Export data (DSAR)' });
		await expect(exportButton).toBeDisabled();
		await page.getByLabel('Reason for the unmasked export').fill(JUSTIFICATION);
		await expect(exportButton).toBeEnabled();
	});

	test('with MFA off locally the unmasked export goes straight through and says so', async ({
		page
	}) => {
		const vendorId = await createVendor(page);
		await fillUnmaskedExport(page, vendorId);

		const sent = page.waitForRequest((r) => isDsar(new URL(r.url())) && r.method() === 'POST');
		await page.getByRole('button', { name: 'Export data (DSAR)' }).click();
		const body = (await sent).postDataJSON();
		expect(body.include_banking).toBe(true);
		expect(body.banking_justification).toBe(JUSTIFICATION);
		expect(body.step_up).toBeUndefined();

		const result = page.getByRole('dialog', { name: 'Data export' });
		await expect(result).toBeVisible({ timeout: 10_000 });
		await expect(result.getByTestId('dsar-unmasked-notice')).toBeVisible();
		await expect(page.getByRole('dialog', { name: 'Confirm it’s you' })).toHaveCount(0);
	});

	test('a required step-up opens the prompt and the proof rides the resend', async ({ page }) => {
		const vendorId = await createVendor(page);
		const bodies: Record<string, unknown>[] = [];
		await page.route(isDsar, async (route) => {
			if (route.request().method() !== 'POST') return route.fallback();
			const body = route.request().postDataJSON();
			bodies.push(body);
			if (bodies.length === 1) {
				return refusal(route, 403, 'sensitive_step_up_required', 'server English');
			}
			if (bodies.length === 2) {
				return refusal(route, 400, 'sensitive_step_up_failed', 'server English');
			}
			// The real backend (MFA off) ignores the proof and answers for real.
			return route.fallback();
		});

		await fillUnmaskedExport(page, vendorId);
		await page.getByRole('button', { name: 'Export data (DSAR)' }).click();

		const prompt = page.getByRole('dialog', { name: 'Confirm it’s you' });
		await expect(prompt).toBeVisible();
		// Never a password field: a password is not a second factor here.
		await expect(prompt.locator('input[type="password"]')).toHaveCount(0);
		const confirm = prompt.getByRole('button', { name: 'Confirm' });
		await prompt.getByLabel('Authenticator code').fill('12345');
		await expect(confirm).toBeDisabled();
		await prompt.getByLabel('Authenticator code').fill('123456');
		await confirm.click();

		// The server refused that code: the prompt stays, in the reader's language.
		await expect(prompt.getByTestId('step-up-error')).toHaveText(
			'That authenticator code or passkey could not be verified. Confirm a current authenticator code or a registered passkey to continue.'
		);
		expect(bodies[1].step_up).toEqual({ code: '123456' });
		expect(bodies[1].include_banking).toBe(true);
		expect(bodies[1].banking_justification).toBe(JUSTIFICATION);

		await prompt.getByLabel('Authenticator code').fill('654321');
		await prompt.getByRole('button', { name: 'Confirm' }).click();
		await expect(prompt).toBeHidden();
		expect(bodies[2].step_up).toEqual({ code: '654321' });
		const result = page.getByRole('dialog', { name: 'Data export' });
		await expect(result).toBeVisible({ timeout: 10_000 });
		await expect(result.getByTestId('dsar-unmasked-notice')).toBeVisible();
	});

	test('an admin with no second factor is told where to set one up', async ({ page }) => {
		const vendorId = await createVendor(page);
		await page.route(isDsar, async (route) => {
			if (route.request().method() !== 'POST') return route.fallback();
			return refusal(route, 403, 'sensitive_step_up_no_factor', 'server English');
		});

		await fillUnmaskedExport(page, vendorId);
		await page.getByRole('button', { name: 'Export data (DSAR)' }).click();

		const alert = page.getByTestId('dsar-no-factor');
		await expect(alert).toBeVisible();
		await expect(alert).toContainText('a password alone cannot authorize it');
		await expect(
			alert.getByRole('link', { name: 'Set up an authenticator app or a passkey on your profile' })
		).toHaveAttribute('href', '/profile?section=mfa');
		await expect(page.getByRole('dialog', { name: 'Confirm it’s you' })).toHaveCount(0);
	});
});
