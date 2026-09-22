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
 * /organization — settings page. Sixteen panels of unrelated settings share the
 * route because they share `PATCH /api/organization`; it shows **one at a time**,
 * picked from `ui/SettingsRail.svelte` and addressed by `?section=<slug>`.
 *
 * So a test here names the panel it is about in the URL. Navigating by URL
 * rather than clicking the rail is deliberate: the rail's own behaviour is
 * `section-nav.spec.ts`'s subject, and coupling every settings test to it would
 * make one navigation regression fail sixteen unrelated specs.
 *
 * We assert every rail destination resolves to its own panel, and round-trip an
 * edit via two: Company Profile (sends name + settings.company) and Invoice
 * Defaults (sends settings.invoice_defaults). Both revert via a PATCH in finally.
 */

/**
 * Every `?section=` slug and the `<h2>` of the panel it shows — the page's URL
 * contract, mirroring `SECTION_GROUPS` in `routes/organization/+page.svelte`.
 *
 * One table, and `the rail offers exactly the panels this spec knows about`
 * below holds it against the rail the page actually renders: a panel added to
 * the page without being added here fails that test instead of quietly going
 * unexercised.
 */
const PANELS = [
	['getting-started', 'Getting started'],
	['company', 'Company Profile'],
	['defaults', 'Invoice Defaults'],
	['branding', 'Branding'],
	['custom-domains', 'Custom Domains'],
	['erp', 'ERP Integration'],
	['extraction', 'AI Extraction'],
	['email-intake', 'Email Intake'],
	['chat', 'Chat Notifications'],
	['data-sync', 'Data Sync'],
	['payments', 'Payments (ACH / Wire / RTP)'],
	['cards', 'Virtual Cards'],
	['security', 'Security'],
	['fraud', 'Fraud Detection'],
	['residency', 'Data Residency'],
	['plan', 'Plan']
] as const;

/**
 * The six panels whose settings block `org_settings_view.NON_ADMIN_SETTINGS`
 * withholds, so they have no non-admin data at all — slug, heading, and the
 * testid of the admin-only hint that replaces their body for a non-admin.
 *
 * Shared by the admin case (none of these hints may appear) and the clerk cases
 * (every one of them must, with no field left behind it). Each entry now costs
 * its own panel visit, since no two of them are on screen together.
 */
const ADMIN_ONLY_PANELS = [
	['extraction', 'AI Extraction', 'extraction-admin-only'],
	['erp', 'ERP Integration', 'erp-admin-only'],
	['payments', 'Payments (ACH / Wire / RTP)', 'payments-admin-only'],
	['cards', 'Virtual Cards', 'cards-admin-only'],
	['security', 'Security', 'security-admin-only'],
	['fraud', 'Fraud Detection', 'fraud-admin-only']
] as const;

/** The `section.card` carrying `heading`. */
function card(page: import('@playwright/test').Page, heading: string) {
	return page.locator('section.card', {
		has: page.getByRole('heading', { name: heading })
	});
}

test.describe('/organization settings', () => {
	test('the rail offers exactly the panels this spec knows about', async ({ page }) => {
		await page.goto('/organization');
		// `evaluateAll` has no auto-wait, so gate on the default panel first —
		// the rail and the panel are inside the same `{#if org}` block, so a
		// rendered heading proves the rail is rendered too
		// (frontend/CLAUDE.md § `networkidle` is not a readiness signal).
		await expect(page.getByRole('heading', { name: 'Getting started', exact: true })).toBeVisible();

		const railSlugs = await page
			.locator('nav a[data-section-link]')
			.evaluateAll((els) => els.map((el) => el.getAttribute('data-section-link') ?? ''));

		expect(railSlugs).toEqual(PANELS.map(([slug]) => slug));
	});

	test('every rail section shows its own panel, and only that one', async ({ page }) => {
		for (const [i, [slug, heading]] of PANELS.entries()) {
			// A different panel's heading — the next one round the table, so each
			// entry both proves itself present and proves its neighbour gone.
			const [, otherHeading] = PANELS[(i + 1) % PANELS.length];

			await page.goto(`/organization?section=${slug}`);

			// Positive first. This is the wait: the panels live behind
			// `GET /api/organization`, and Fraud Detection behind the admin-only
			// `…/fraud-rules/defaults` on top of that, so a `toHaveCount(0)` run
			// first would pass against a page that had rendered nothing yet.
			await expect(
				page.getByRole('heading', { name: heading, exact: true }),
				`?section=${slug} must show "${heading}"`
			).toBeVisible();
			await expect(
				page.getByRole('heading', { name: otherHeading, exact: true }),
				`?section=${slug} must not also show "${otherHeading}"`
			).toHaveCount(0);

			// …and the rail agrees with the URL about where the reader is.
			await expect(page.locator(`[data-section-link="${slug}"]`)).toHaveAttribute(
				'aria-current',
				'page'
			);
		}
	});

	test('Company Profile saves a phone change and round-trips through GET', async ({
		page
	}) => {
		await page.goto('/organization?section=company');
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
		await page.goto('/organization?section=defaults');
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
		await page.goto('/organization?section=company');
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

			// Reload — the page hydrates from /api/organization and the reload
			// keeps `?section=company`, so the textarea should be repopulated
			// with the new value.
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
		await page.goto('/organization?section=security');
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
			// initial load — reload (which keeps `?section=security`) and confirm
			// it still renders from a fresh GET too.
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
		// Within each panel the positive assertion runs FIRST, because the
		// absence assertion after it would pass against a page that had not
		// finished resolving. Fraud Detection is the strongest gate in the list:
		// its card needs both `GET /api/auth/me` to have landed AND the
		// admin-only `…/fraud-rules/defaults` to have answered, so reaching its
		// field proves the page is fully settled as an admin.
		for (const [slug, heading] of ADMIN_ONLY_PANELS) {
			await page.goto(`/organization?section=${slug}`);

			await expect(
				card(page, heading).locator('select, input, textarea').first(),
				`${heading} must keep its fields for an admin`
			).toBeVisible();

			await expect(
				page.locator('[data-testid$="-admin-only"]'),
				`${heading} must show an admin no admin-only hint`
			).toHaveCount(0);
			await expect(page.locator('fieldset.sections')).not.toHaveAttribute('disabled', '');
		}
	});
});

/**
 * The non-admin read-only mode, and whether what it shows is TRUE.
 *
 * `/organization` stays admin-only in the nav, but the page carries a
 * deliberate read-only mode for anyone who arrives by typed URL or a stale
 * bookmark after a role change: one disabled `<fieldset>` around the panel
 * stack, asserted by the clerk cases in `email-intake.spec.ts` and
 * `tenant-url.spec.ts`.
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
		// carry role="alert". The page's EAGER reads are the org settings and the
		// public config, both role-open, so arriving on the default panel must
		// produce nothing to refuse.
		await expect(page.locator('fieldset.sections [role="alert"]')).toHaveCount(0);

		// …and the panel whose read is withheld says so, instead of rendering a
		// 403 the reader cannot act on. Its read is panel-scoped now, so it only
		// fires once the panel is asked for — which is the state to check.
		await page.goto('/organization?section=chat');
		await expect(page.getByTestId('chat-admin-only')).toBeVisible();
		await expect(page.locator('fieldset.sections [role="alert"]')).toHaveCount(0);
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

		for (const [slug, heading, testid] of ADMIN_ONLY_PANELS) {
			await page.goto(`/organization?section=${slug}`);
			await expect(page.getByTestId('org-readonly-banner')).toBeVisible();

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

		// The sharpest of the six, because the value is a hardcoded literal
		// rather than a fallback: on `program_type === 'platform'` (the
		// initializer) the Extraction panel printed this into a disabled input, so
		// a clerk read it as their tenant's extraction provider. Asserted on the
		// panel that owns it, with its hint as the proof the panel is settled.
		await page.goto('/organization?section=extraction');
		await expect(page.getByTestId('extraction-admin-only')).toBeVisible();
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
		// exactly why these panels keep their fields rather than a hint. One
		// panel per visit now, so each is asked for by name.
		const org = await getOrg(page);

		await page.goto('/organization?section=company');
		await expect(page.getByTestId('org-readonly-banner')).toBeVisible();
		const name = card(page, 'Company Profile').getByLabel('Company Name');
		// The tenant's OWN name, read back from the same response — a panel that
		// merely rendered an input would pass an emptiness check.
		await expect(name).toHaveValue(org.name);
		await expect(name).toBeDisabled();

		await page.goto('/organization?section=defaults');
		await expect(card(page, 'Invoice Defaults').locator('select').first()).toHaveValue(
			org.settings.invoice_defaults?.currency ?? 'USD'
		);

		// Branding's own read (`GET /api/organization/branding`) is role-open too,
		// and `tenant-url.spec.ts` asserts the clerk's controls here are disabled.
		await page.goto('/organization?section=branding');
		await expect(card(page, 'Branding').getByLabel('Product Name')).toBeVisible();
	});
});
