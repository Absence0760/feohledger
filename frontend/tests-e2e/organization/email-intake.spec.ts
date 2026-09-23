import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /organization → Email Intake panel.
 *
 * `GET /api/organization/email-intake` and `POST .../rotate-token` shipped with
 * no frontend caller at all: the inbound-email channel was undiscoverable, and
 * — the sharper half — a leaked intake token could not be rotated from the
 * product, though the `+<token>` part of the address is a bearer secret that
 * lets anyone drop a payable into the tenant's queue.
 *
 * Two of the four states can be exercised against the real dev backend and two
 * cannot, so this spec splits accordingly (the same line
 * `custom-domain-rejection.spec.ts` draws):
 *
 *   • REAL — `FEOH_EMAIL_INTAKE_DOMAIN` is unset in the committed dev config,
 *     so `intake_address_for` returns null for every org and no address can
 *     exist. That is exactly the disabled-feature state, and it is asserted
 *     against the live API.
 *   • MOCKED — a *configured* deployment cannot be produced from a test: the
 *     intake domain is a backend env var read at import time, not a per-tenant
 *     setting. The address / rotate states replay the shapes the route really
 *     returns (`{address, enabled}` and `{address}`) through `page.route`; the
 *     assertion is that the panel renders whatever the backend said, which is
 *     the contract this spec exists to hold.
 *
 * The non-admin case is real end to end — it is about the page's own gate.
 *
 * `?section=email-intake` because the route shows one panel at a time now (the
 * slug is part of its URL contract), and this panel's read only fires once it
 * is shown. The `page.reload()` below keeps the query string.
 */

const CONFIGURED = 'invoices+a1b2c3d4@ap.example.test';
const ROTATED = 'invoices+z9y8x7w6@ap.example.test';

function panel(page: import('@playwright/test').Page) {
	return page.locator('section.card', {
		has: page.getByRole('heading', { name: 'Email Intake' })
	});
}

/** Replay a deployment that HAS an intake domain configured. */
async function mockIntake(
	page: import('@playwright/test').Page,
	status: { address: string | null; enabled: boolean; domain_configured?: boolean }
) {
	// The mock replays the REAL payload, so it carries `domain_configured` —
	// the field that tells "this deployment has no intake domain" apart from
	// "this org has no token yet" on the first read. These mocks all model a
	// CONFIGURED deployment, which is the state the dev backend cannot produce.
	const body = { domain_configured: true, ...status };
	await page.route('**/api/organization/email-intake**', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/organization/email-intake/rotate-token') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ address: ROTATED })
			});
		}
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(body)
		});
	});
}

test.describe('/organization email intake', () => {
	test('the disabled-feature state explains itself and offers no rotate', async ({ page }) => {
		// Force the *proven* unavailable state: mint a token through the real
		// endpoint so `enabled` is true, then reload. `enabled: true` with a null
		// address can only mean the platform has no intake domain — which is the
		// committed dev default, so this is the honest local state, not a stub.
		await page.goto('/organization?section=email-intake');
		const minted = await page.request.post(
			`${API_BASE}/api/organization/email-intake/rotate-token`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(minted.ok()).toBeTruthy();
		// The precondition this whole test rests on: no intake domain configured.
		expect((await minted.json()) as { address: string | null }).toEqual({ address: null });

		await page.reload();

		const card = panel(page);
		await expect(card.getByTestId('email-intake-unavailable')).toBeVisible();
		await expect(card.getByTestId('email-intake-unavailable')).toContainText(
			'FEOH_EMAIL_INTAKE_DOMAIN'
		);
		// No address to show, and — the point — no control that could only
		// mint another token addressing nothing.
		await expect(card.getByTestId('email-intake-address')).toHaveCount(0);
		await expect(card.getByRole('button', { name: 'Rotate the email intake address' })).toHaveCount(
			0
		);
	});

	test('an admin sees the intake address with a copy control', async ({ page }) => {
		await mockIntake(page, { address: CONFIGURED, enabled: true });
		await page.goto('/organization?section=email-intake');

		const card = panel(page);
		await expect(card.getByTestId('email-intake-address')).toHaveText(CONFIGURED);
		await expect(card.getByRole('button', { name: 'Copy the email intake address' })).toBeVisible();
		// The address is a bearer secret, and the panel has to say so.
		await expect(card.getByText('Treat this address like a password.')).toBeVisible();
	});

	test('rotate needs the armed second click and yields a different address', async ({ page }) => {
		await mockIntake(page, { address: CONFIGURED, enabled: true });
		await page.goto('/organization?section=email-intake');

		const card = panel(page);
		const rotate = card.getByRole('button', { name: 'Rotate the email intake address' });

		// First click ARMS only — it must not reach the endpoint. Watch for the
		// POST so a regression that fires on the first click fails here rather
		// than passing on the second click's assertion.
		let rotatePosts = 0;
		page.on('request', (r) => {
			if (r.url().endsWith('/rotate-token') && r.method() === 'POST') rotatePosts += 1;
		});

		await rotate.click();
		await expect(card.getByTestId('email-intake-rotate-warning')).toBeVisible();
		await expect(card.getByTestId('email-intake-rotate-warning')).toContainText(
			'stops accepting email the moment you confirm'
		);
		await expect(rotate).toHaveText('Confirm rotate');
		await expect(card.getByTestId('email-intake-address')).toHaveText(CONFIGURED);
		expect(rotatePosts).toBe(0);

		// Second click commits, and the replacement is revealed with a copy
		// affordance through the shared SecretReveal.
		await rotate.click();
		const revealed = page.getByTestId('email-intake-address-rotated');
		await expect(revealed).toBeVisible();
		await expect(revealed).toHaveText(ROTATED);
		expect(ROTATED).not.toBe(CONFIGURED);
		await expect(page.getByText('The previous address no longer works.')).toBeVisible();
		expect(rotatePosts).toBe(1);

		// Dismissing the reveal leaves the panel on the new address.
		await page.getByRole('button', { name: 'Done' }).click();
		await expect(revealed).toHaveCount(0);
		await expect(card.getByTestId('email-intake-address')).toHaveText(ROTATED);
	});

	test('an unprovisioned tenant is offered create, never rotate', async ({ page }) => {
		await mockIntake(page, { address: null, enabled: false });
		await page.goto('/organization?section=email-intake');

		const card = panel(page);
		await expect(card.getByTestId('email-intake-unprovisioned')).toBeVisible();
		await expect(card.getByRole('button', { name: 'Create intake address' })).toBeVisible();
		// Nothing exists yet, so nothing can be invalidated — the destructive
		// wording and the armed confirm belong to rotation only.
		await expect(card.getByRole('button', { name: 'Rotate the email intake address' })).toHaveCount(
			0
		);
	});

	test('a non-admin gets a read-only page and no intake controls', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization?section=email-intake');

		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

		// The whole settings stack is inside one disabled <fieldset>, so every
		// control below it is inert. Assert the mechanism, then spot-check
		// fields from two unrelated panels — that is what proves the mode is
		// page-level rather than this panel's own gate.
		// (`toBeDisabled` is asserted on the controls, not the fieldset:
		// Playwright's disabled check doesn't count a <fieldset> itself, even
		// though it does honour a disabled fieldset ancestor.)
		//
		// The two spot-checks each need their own panel visit now that the route
		// shows one at a time — which makes the point more sharply than before,
		// since the fieldset has to still be disabled on arrival at a panel that
		// was never on screen when the role resolved.
		await expect(page.locator('fieldset.sections')).toHaveAttribute('disabled', '');

		await page.goto('/organization?section=company');
		await expect(page.getByLabel('Company Name')).toBeDisabled();

		await page.goto('/organization?section=custom-domains');
		await expect(page.getByLabel('New custom domain')).toBeDisabled();

		await page.goto('/organization?section=email-intake');
		const card = panel(page);
		// GET is admin-only; the panel says so instead of rendering a 403 as a
		// load failure the reader cannot act on.
		await expect(card.getByTestId('email-intake-admin-only')).toBeVisible();
		await expect(card.getByTestId('email-intake-address')).toHaveCount(0);
		await expect(card.getByRole('button', { name: 'Rotate the email intake address' })).toHaveCount(
			0
		);
	});
});
