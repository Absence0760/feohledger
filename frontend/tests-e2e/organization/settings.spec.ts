import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';
// A VALUE import, allowed because the module is pure by design and says so (no
// `$env`, no `$app`, no imports at all) — the `frontend/CLAUDE.md` exception
// `auth/rbac.spec.ts` established. The test computes the expected label with the
// resolver the store uses rather than re-typing the rung order.
import { resolveReportingCurrency } from '$lib/utils/reportingCurrency';

interface OrgResponse {
	id: string;
	name: string;
	slug: string;
	plan: string;
	settings: Record<string, unknown> & {
		company?: {
			tax_id?: string;
			address?: string;
			phone?: string;
			website?: string;
			logo_url?: string;
		};
		invoice_defaults?: {
			currency?: string;
			payment_terms?: string;
			number_prefix?: string;
			default_gl_account?: string;
			default_cost_center?: string;
		};
	};
}

async function getOrg(page: import('@playwright/test').Page): Promise<OrgResponse> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page)
	});
	return (await resp.json()) as OrgResponse;
}

async function patchOrg(
	page: import('@playwright/test').Page,
	body: Record<string, unknown>
): Promise<void> {
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: body
	});
}

/**
 * /organization — settings page. The page is one big set of sections
 * (Company Profile, Invoice Defaults, AI Extraction, ERP, Payments,
 * Cards, Security, Data Sync, Plan), each with its own Save button.
 *
 * We assert all sections render, and round-trip an edit via two
 * sections: Company Profile (sends name + settings.company) and
 * Invoice Defaults (sends settings.invoice_defaults). Both revert via
 * a PATCH in finally.
 */

/**
 * The six panels whose settings block `org_settings_view.NON_ADMIN_SETTINGS`
 * withholds, so they have no non-admin data at all — heading plus the testid of
 * the admin-only hint that replaces their body for a non-admin.
 *
 * Shared by the admin case (none of these hints may appear) and the clerk cases
 * (every one of them must, with no field left behind it).
 */
const ADMIN_ONLY_PANELS = [
	['AI Extraction', 'extraction-admin-only'],
	['ERP Integration', 'erp-admin-only'],
	['Payments (ACH / Wire / RTP)', 'payments-admin-only'],
	['Virtual Cards', 'cards-admin-only'],
	['Security', 'security-admin-only'],
	['Fraud Detection', 'fraud-admin-only']
] as const;

/** The `section.card` carrying `heading`. */
function card(page: import('@playwright/test').Page, heading: string) {
	return page.locator('section.card', {
		has: page.getByRole('heading', { name: heading })
	});
}

test.describe('/organization settings', () => {
	test.beforeEach(async ({ page }) => {
		await page.goto('/organization');
	});

	test('all section cards render with their headings', async ({ page }) => {
		const expected = [
			'Company Profile',
			'Invoice Defaults',
			'AI Extraction',
			'ERP Integration',
			'Payments (ACH / Wire / RTP)',
			'Virtual Cards',
			'Security',
			'Data Sync'
		];
		for (const heading of expected) {
			await expect(page.getByRole('heading', { name: heading })).toBeVisible();
		}
	});

	test('Company Profile saves a phone change and round-trips through GET', async ({
		page
	}) => {
		const before = await getOrg(page);
		const originalPhone = before.settings.company?.phone ?? '';
		const next = `+1-555-e2e-${Date.now() % 100000}`;

		try {
			const profileCard = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Company Profile' })
			});
			const phoneInput = profileCard.locator('input[type="tel"]');
			await phoneInput.fill(next);

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization') &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
			await profileCard.getByRole('button', { name: /Save Profile/ }).click();
			const resp = await saved;
			expect(resp.status()).toBe(200);

			const after = await getOrg(page);
			expect(after.settings.company?.phone).toBe(next);
		} finally {
			await patchOrg(page, {
				settings: {
					company: {
						...(before.settings.company ?? {}),
						phone: originalPhone
					}
				}
			});
		}
	});

	test('Invoice Defaults saves a currency change and round-trips', async ({ page }) => {
		const before = await getOrg(page);
		const originalCurrency = before.settings.invoice_defaults?.currency ?? 'USD';
		// Pick a non-current currency so we know the value flipped.
		const next = originalCurrency === 'EUR' ? 'GBP' : 'EUR';

		try {
			const defaultsCard = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Invoice Defaults' })
			});
			await defaultsCard.locator('select').first().selectOption(next);

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization') &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
			await defaultsCard.getByRole('button', { name: /Save Defaults/ }).click();
			await saved;

			const after = await getOrg(page);
			expect(after.settings.invoice_defaults?.currency).toBe(next);

			// `invoice_defaults.currency` is a rung of the reporting-currency
			// chain, and the `orgCurrency` store is session-cached — so the label
			// on this same page that names that currency has to follow the save,
			// not keep naming the answer from before it until a reload
			// (decisions §200). Computed with the store's own resolver, so a
			// tenant carrying a higher rung expects that rung instead.
			const expected = resolveReportingCurrency(after.settings);
			expect(expected).not.toBeNull();
			await expect(page.locator('#org-payments')).toContainText(
				`CFO sign-off threshold (${expected})`
			);
		} finally {
			await patchOrg(page, {
				settings: {
					invoice_defaults: {
						...(before.settings.invoice_defaults ?? {}),
						currency: originalCurrency
					}
				}
			});
		}
	});

	test('Company Profile saves an address change and pre-fills it on reload', async ({
		page
	}) => {
		const before = await getOrg(page);
		const originalAddress = before.settings.company?.address ?? '';
		const next = `e2e address ${Date.now()}`;

		try {
			const profileCard = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Company Profile' })
			});
			await profileCard.locator('textarea').fill(next);

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization') &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
			await profileCard.getByRole('button', { name: /Save Profile/ }).click();
			await saved;

			// Reload — page hydrates from /api/organization, so the textarea
			// should be repopulated with the new value.
			await page.reload();
			await expect(
				page
					.locator('section.card', {
						has: page.getByRole('heading', { name: 'Company Profile' })
					})
					.locator('textarea')
			).toHaveValue(next);
		} finally {
			await patchOrg(page, {
				settings: {
					company: {
						...(before.settings.company ?? {}),
						address: originalAddress
					}
				}
			});
		}
	});

	test('Security warns when "require MFA" is saved but the platform switch is off', async ({
		page
	}) => {
		// Local/CI dev always runs with FEOH_MFA_ENABLED=false, so saving
		// required=true here always lands on the "not enforced yet" branch —
		// see `settings.mfa.enforcement_active` in
		// backend/app/api/organization.py::_org_response.
		try {
			const securityCard = page.locator('section.card', {
				has: page.getByRole('heading', { name: 'Security' })
			});
			const checkbox = securityCard.locator('label.switch-row input[type="checkbox"]');
			await checkbox.check();

			const saved = page.waitForResponse(
				(r) =>
					r.url().endsWith('/api/organization') &&
					r.request().method() === 'PATCH' &&
					r.status() === 200
			);
			await securityCard.getByRole('button', { name: /Save/ }).click();
			await saved;

			await expect(page.getByTestId('mfa-enforcement-inactive')).toBeVisible();

			// The warning is derived from the PATCH response, not just the
			// initial load — reload and confirm it still renders from a fresh
			// GET too.
			await page.reload();
			await expect(page.getByTestId('mfa-enforcement-inactive')).toBeVisible();
		} finally {
			await patchOrg(page, { settings: { mfa: { required: false } } });
		}
	});

	test('an admin sees every panel body, and no admin-only hint', async ({ page }) => {
		// The other half of §153. The read-only treatment is keyed on the ROLE,
		// never on whether a value happens to be present, precisely so it cannot
		// leak here: an admin's form reads its saved credentials back into its
		// fields, and each section saves whole, so a blanked field would wipe a
		// live config on the next save.
		//
		// The positive loop runs FIRST, because the two absence assertions after
		// it would pass against a page that had not finished resolving. Fraud
		// Detection is the strongest gate in the list: its body needs both
		// `GET /api/auth/me` to have landed AND the admin-only
		// `…/fraud-rules/defaults` to have answered, so reaching it proves the
		// page is fully settled as an admin.
		for (const [heading] of ADMIN_ONLY_PANELS) {
			await expect(
				card(page, heading).locator('select, input, textarea').first(),
				`${heading} must keep its fields for an admin`
			).toBeVisible();
		}

		await expect(page.locator('[data-testid$="-admin-only"]')).toHaveCount(0);
		await expect(page.locator('fieldset.sections')).not.toHaveAttribute('disabled', '');
	});
});

/**
 * The non-admin read-only mode, and whether what it shows is TRUE.
 *
 * `/organization` stays admin-only in the nav, but the page carries a
 * deliberate read-only mode for anyone who arrives by typed URL or a stale
 * bookmark after a role change: one disabled `<fieldset>` around every panel,
 * asserted by the clerk cases in `email-intake.spec.ts` and `tenant-url.spec.ts`.
 * Neither of those asked whether the clerk was being told the truth, and in two
 * ways they were not (`docs/decisions.md` §153):
 *
 *   - `GET /api/organization/chat-notifications` is admin-only and was fetched
 *     unconditionally, so the Chat Notifications panel rendered a live
 *     `role="alert"` reading "Your role does not permit this action." — the
 *     exact anti-pattern that panel's own comment says the design avoids.
 *   - `services/org_settings_view.py::NON_ADMIN_SETTINGS` withholds the six
 *     blocks behind the panels above, so each fell back to its field
 *     initializers and presented PLATFORM defaults as the tenant's
 *     configuration — Extraction read "Claude Vision (Anthropic) / Platform"
 *     whatever the tenant had bought — while Fraud Detection vanished entirely.
 *
 * Widening the projection to fill those fields is NOT the fix and must never be:
 * the blocks carry the tenant's third-party credentials, which is what that
 * module exists to withhold.
 */
test.describe('/organization settings — non-admin read-only mode', () => {
	test('a clerk meets no error alert on mount', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization');

		// Non-vacuity for the absence assertion below: the page has rendered AND
		// resolved the role (the banner is gated on `auth.user` having landed).
		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

		// Scoped to the settings stack, because the global Toast live-regions also
		// carry role="alert". Every mount read on this page is now either
		// role-open (the org settings, custom domains, data residency, the public
		// config) or gated on `auth.isAdmin` (chat notifications, email intake,
		// fraud-rule defaults), so there is nothing left to refuse.
		await expect(page.locator('fieldset.sections [role="alert"]')).toHaveCount(0);

		// …and the panel whose read is withheld says so, instead of rendering a
		// 403 the reader cannot act on.
		await expect(page.getByTestId('chat-admin-only')).toBeVisible();
		await expect(
			card(page, 'Chat Notifications').locator('select, input'),
			'a clerk is offered no chat controls at all'
		).toHaveCount(0);
	});

	test('a clerk is told which panels are admin-only, and shown no defaults in them', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization');
		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

		for (const [heading, testid] of ADMIN_ONLY_PANELS) {
			const panel = card(page, heading);
			// The heading stays — the setting exists and knowing who to ask is the
			// useful part — and the hint replaces the body.
			await expect(page.getByRole('heading', { name: heading })).toBeVisible();
			await expect(panel.getByTestId(testid)).toBeVisible();
			// The body is GONE, not merely disabled. A disabled <fieldset> around a
			// platform default is still a platform default on screen, which is the
			// whole finding.
			await expect(
				panel.locator('select, input, textarea'),
				`${heading} must show a clerk no field it cannot populate`
			).toHaveCount(0);
		}

		// The sharpest of the five, because the value is a hardcoded literal
		// rather than a fallback: on `program_type === 'platform'` (the
		// initializer) the Extraction panel printed this into a disabled input, so
		// a clerk read it as their tenant's extraction provider.
		await expect(page.locator('input[value="Claude Vision (Anthropic)"]')).toHaveCount(0);
	});

	test('a clerk still reads the panels that do carry tenant data', async ({
		page,
		tenantClerk
	}) => {
		await signInAndWait(page, tenantClerk);
		// The clerk's OWN projected read. `GET /api/organization` is role-open and
		// `NON_ADMIN_SETTINGS` admits `company`, `invoice_defaults` and `brand`
		// whole — each listed there for a named non-admin consumer — which is
		// exactly why these panels keep their fields rather than a hint.
		const org = await getOrg(page);
		await page.goto('/organization');
		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

		const name = card(page, 'Company Profile').getByLabel('Company Name');
		// The tenant's OWN name, read back from the same response — a panel that
		// merely rendered an input would pass an emptiness check.
		await expect(name).toHaveValue(org.name);
		await expect(name).toBeDisabled();

		await expect(card(page, 'Invoice Defaults').locator('select').first()).toHaveValue(
			org.settings.invoice_defaults?.currency ?? 'USD'
		);

		// Branding's own read (`GET /api/organization/branding`) is role-open too,
		// and `tenant-url.spec.ts` asserts the clerk's controls here are disabled.
		await expect(card(page, 'Branding').getByLabel('Product Name')).toBeVisible();
	});
});
