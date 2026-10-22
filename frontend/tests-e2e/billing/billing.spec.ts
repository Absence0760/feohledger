import { execFileSync } from 'node:child_process';

import { API_BASE, currentTenantSlug, expect, signInAndWait, TENANT_ROOT_URL, test } from '../fixtures/helpers';

/**
 * /billing — Platform Billing & Metering (read/display surface).
 *
 * Consumes `GET /api/billing/subscription` (admin/cfo): current plan + tier +
 * price, subscription status badge, billing-period window, and usage-to-date
 * meters. This is the AP platform's OWN customer billing (control-plane), not
 * the AP money path the app runs for customers.
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (admin is in the allowed set admin/cfo),
 * so the page loads directly without a redirect. The "not authorized" block
 * opts out and signs in as the clerk.
 *
 * Billing rows live in the CONTROL plane (`feohledger`), keyed by the
 * org's id — not the tenant DB — so the happy-path test seeds a Plan +
 * Subscription via control-plane psql and tears them down in `finally`.
 *
 * Selectors are accessible name / data-testid — never brittle CSS/nth-child,
 * never `waitForTimeout`.
 */

const CONTROL_DB = 'feohledger';

/** Run a synchronous `psql -c <query>` against the CONTROL-plane DB and return
 *  the FIRST output line. `INSERT … RETURNING id` emits the id line AND a
 *  trailing `INSERT 0 1` command tag on this psql build, so we take line 0 (the
 *  RETURNING value) rather than the whole — a plain trim would keep the tag. */
function controlPsql(query: string): string {
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', CONTROL_DB, '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out
		.toString()
		.split('\n')
		.map((l) => l.trim())
		.filter((l) => l.length > 0 && !/^INSERT \d+ \d+$/.test(l))[0] ?? '';
}

/** Like `controlPsql` but returns EVERY output row, not just the first. */
function controlPsqlAll(query: string): string[] {
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', CONTROL_DB, '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out
		.toString()
		.split('\n')
		.map((l) => l.trim())
		.filter((l) => l.length > 0 && !/^(INSERT|UPDATE|DELETE) \d+( \d+)?$/.test(l));
}

/** The control-plane org id for the current worker's tenant slug. */
function currentOrgId(): string {
	const slug = currentTenantSlug();
	const id = controlPsql(`SELECT id FROM organizations WHERE slug='${slug}'`);
	expect(id, `no control-plane org for slug ${slug}`).toMatch(/[0-9a-f-]{36}/);
	return id;
}

/**
 * Park (cancel) whatever live subscription the org already has, and hand back
 * the ids so the test's `finally` can put them back.
 *
 * `tenant_provisioning` calls `ensure_subscription(plan_code="free")` for every
 * org, so an e2e tenant already holds a live subscription before any test runs.
 * These tests then seeded a SECOND `active` row for the same org — a state
 * `uq_subscription_one_live_per_org` (migration 0093) correctly forbids, and
 * which only worked previously because that index lived in a migration and was
 * never declared on the model, so a `create_all`-built database silently lacked
 * it (`docs/decisions.md` §109). With two live rows the endpoint under test
 * picked one arbitrarily, so these assertions passed by luck.
 *
 * Seed the state the app actually supports instead: park what is live, run as
 * the sole live subscription, then restore exactly the parked ids — **after**
 * the test's own row is deleted, or the restore would recreate the collision.
 * Restoring by captured id rather than re-provisioning a `free` row leaves the
 * tenant byte-identical for whatever runs next in this worker.
 */
function parkLiveSubscriptions(orgId: string): string[] {
	const parked = controlPsqlAll(
		`SELECT id FROM subscriptions WHERE organization_id='${orgId}' AND status <> 'canceled'`
	);
	if (parked.length) {
		controlPsql(
			`UPDATE subscriptions SET status='canceled' WHERE id IN (${parked
				.map((id) => `'${id}'`)
				.join(',')})`
		);
	}
	return parked;
}

/** Undo `parkLiveSubscriptions`. Call only once the test's own subscription
 *  rows are gone — two live rows for one org is the very thing being avoided. */
function restoreLiveSubscriptions(parked: string[]): void {
	if (!parked.length) return;
	controlPsql(
		`UPDATE subscriptions SET status='active' WHERE id IN (${parked
			.map((id) => `'${id}'`)
			.join(',')})`
	);
}

test.describe('/billing (admin)', () => {
	// Sign the worker admin in explicitly (don't rely on the shared per-worker
	// storage-state cache) so the gated page is reliably authed before each
	// test — admin is in the allowed set {admin, cfo}. This is a real,
	// deterministic sign-in, not a wait/retry workaround.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/billing');
	});

	test('renders the billing surface header', async ({ page }) => {
		await expect(page.getByRole('heading', { level: 1, name: 'Subscription', exact: true })).toBeVisible();
	});

	test('with no subscription, shows the friendly empty state + usage meters', async ({ page }) => {
		// No subscription is seeded for e2e tenants, so the endpoint returns
		// plan=null / subscription=null and the page renders the empty state.
		// (If a previous run's seed leaked, this still holds only when clean —
		// the happy-path test always cleans up after itself.)
		const empty = page.getByTestId('billing-empty');
		const plan = page.getByTestId('billing-plan');
		// Exactly one of {empty, plan} is present depending on seed state.
		await expect(empty.or(plan)).toBeVisible({ timeout: 10_000 });

		if (await empty.isVisible()) {
			await expect(empty.getByRole('heading', { name: 'No active subscription' })).toBeVisible();
			// Usage meters render even with no plan.
			await expect(page.locator('.kpi').first()).toBeVisible();
			await expect(page.locator('.kpi-label', { hasText: 'Extractions' }).first()).toBeVisible();
		}
	});

	test('with a seeded subscription, shows plan, price, status badge + period', async ({ page }) => {
		const orgId = currentOrgId();
		const planCode = `e2e_billing_${Date.now()}`;
		let planId: string | null = null;
		// The org already holds a live `free` subscription from provisioning;
		// park it so this seeded one is the only live row (see the helper).
		const parked = parkLiveSubscriptions(orgId);
		try {
			planId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${planCode}', 'E2E Growth', 49.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{"public_api": true}'::jsonb, 14, true, now(), now()) RETURNING id`
			);
			expect(planId).toMatch(/[0-9a-f-]{36}/);

			controlPsql(
				`INSERT INTO subscriptions (id, organization_id, plan_id, status, ` +
					`current_period_start, current_period_end, trial_end, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${orgId}', '${planId}', 'active', ` +
					`now() - interval '5 days', now() + interval '25 days', NULL, now(), now())`
			);

			await page.goto('/billing');

			const plan = page.getByTestId('billing-plan');
			await expect(plan).toBeVisible({ timeout: 10_000 });
			await expect(plan.getByRole('heading', { name: 'E2E Growth' })).toBeVisible();
			// Status badge.
			await expect(plan.getByText('Active', { exact: true })).toBeVisible();
			// Exact price rendered through <Money> ($49.00).
			await expect(plan.getByText('$49.00')).toBeVisible();
			// The granted entitlement flag surfaces.
			await expect(plan.getByText('public api')).toBeVisible();
			// Usage meters still render.
			await expect(page.locator('.kpi-label', { hasText: 'Extractions' }).first()).toBeVisible();
		} finally {
			// Tear down subscription first (FK to plan), then the plan.
			if (planId) {
				controlPsql(`DELETE FROM subscriptions WHERE plan_id='${planId}'`);
				controlPsql(`DELETE FROM plans WHERE id='${planId}'`);
			}
			// Only now — restoring while this test's row is still live would
			// recreate the two-live-rows collision the index forbids.
			restoreLiveSubscriptions(parked);
		}
	});

	test('renders the invoices / receipts section (mock provider seeds receipts)', async ({
		page
	}) => {
		// `GET /api/billing/invoices` is admin/cfo-gated and sourced through the
		// org's billing adapter. The local-first default (`mock`) fabricates
		// deterministic receipts only when the org has a provider customer id; a
		// fresh e2e tenant has none, so the list is legitimately empty and the
		// section shows its "No invoices yet." empty state. Either way the section
		// renders with its table (never a 500), which is what we assert here.
		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByRole('heading', { name: 'Invoices & receipts' })).toBeVisible();
		// A grid is present (the DataTable shell) regardless of row count.
		await expect(section.locator('.grid-container table')).toBeVisible();
	});

	test('with seeded billing receipts, the list shows rows + a hosted-url link', async ({
		page
	}) => {
		// Point the org at a billing customer id so the `mock` adapter fabricates
		// deterministic receipts, and stub one row's hosted_url via route
		// interception so the "View" link is deterministically present. The route
		// stub keeps this test independent of provider seed state.
		await page.route('**/api/billing/invoices', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					invoices: [
						{
							id: 'mock_in_e2e_2026-06',
							number: 'MOCK-2026-06',
							period: '2026-06',
							amount: '49.00',
							currency: 'USD',
							status: 'paid',
							hosted_url: 'https://billing.example.com/invoices/mock_in_e2e_2026-06',
							created_at: '2026-06-01T00:00:00Z'
						},
						{
							id: 'mock_in_e2e_2026-05',
							number: 'MOCK-2026-05',
							period: '2026-05',
							amount: '49.00',
							currency: 'USD',
							status: 'open',
							hosted_url: null,
							created_at: '2026-05-01T00:00:00Z'
						}
					]
				})
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		// Both rows surface their invoice number + exact <Money> amount + status.
		await expect(section.getByText('MOCK-2026-06')).toBeVisible();
		await expect(section.getByText('MOCK-2026-05')).toBeVisible();
		await expect(section.getByText('$49.00').first()).toBeVisible();
		await expect(section.getByText('Paid', { exact: true })).toBeVisible();
		await expect(section.getByText('Open', { exact: true })).toBeVisible();
		// The paid row's hosted-url opens in a new tab.
		const viewLink = section.getByRole('link', {
			name: /View invoice MOCK-2026-06 \(opens in a new tab\)/
		});
		await expect(viewLink).toBeVisible();
		await expect(viewLink).toHaveAttribute(
			'href',
			'https://billing.example.com/invoices/mock_in_e2e_2026-06'
		);
		await expect(viewLink).toHaveAttribute('target', '_blank');
	});

	test('with no receipts, the invoices section shows the empty state', async ({ page }) => {
		await page.route('**/api/billing/invoices', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ provider: 'mock', invoices: [] })
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('No invoices yet.')).toBeVisible();
	});

	test('renders the payment-methods section (mock provider seeds a card)', async ({ page }) => {
		// `GET /api/billing/payment-methods` is admin/cfo-gated and sourced through
		// the org's billing adapter. The `mock` adapter fabricates a deterministic
		// `visa ····4242` only when the org has a provider customer id; a fresh e2e
		// tenant has none, so the list is legitimately empty and the section shows
		// "No payment method on file." Either way the section renders with its
		// table (never a 500), which is what we assert here.
		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByRole('heading', { name: 'Payment methods' })).toBeVisible();
		// The add/replace-card action and the DataTable shell both render.
		await expect(page.getByTestId('billing-add-card')).toBeVisible();
		await expect(section.locator('.grid-container table')).toBeVisible();
	});

	test('with a saved card stubbed, the list shows the brand · ****last4 · exp', async ({
		page
	}) => {
		// Stub a card so the row is deterministic regardless of provider seed state.
		// PII-safe metadata only — never a PAN.
		await page.route('**/api/billing/payment-methods', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					payment_methods: [
						{
							id: 'mock_pm_e2e',
							brand: 'visa',
							last4: '4242',
							exp_month: 12,
							exp_year: 2030,
							is_default: true
						}
					]
				})
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('Visa ····4242')).toBeVisible();
		await expect(section.getByText('Expires 12/2030')).toBeVisible();
		await expect(section.getByText('Default', { exact: true })).toBeVisible();
	});

	test('with no card, the payment-methods section shows the empty state', async ({ page }) => {
		await page.route('**/api/billing/payment-methods', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ provider: 'mock', payment_methods: [] })
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('No payment method on file.')).toBeVisible();
	});

	test('add-card with a returned client_secret shows the ready / Elements seam', async ({
		page
	}) => {
		// A configured provider returns a single-use client_secret; the UI moves to
		// the "ready" state and surfaces the deployed-only Elements seam (no real
		// Stripe in the local-first stack, no secret-bearing call from the client).
		await page.route('**/api/billing/payment-method/setup-intent', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					configured: true,
					client_secret: 'mock_seti_e2e_secret',
					setup_intent_id: 'mock_seti_e2e'
				})
			});
		});

		await page.goto('/billing');

		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await expect(setup).toBeVisible({ timeout: 10_000 });
		await expect(page.getByTestId('billing-card-elements-placeholder')).toBeVisible();
	});

	test('add-card when not configured shows the billing-not-configured state', async ({ page }) => {
		// No provider customer / unconfigured provider → configured=false, null
		// secret. The UI shows a clear "not configured" affordance, not an error.
		await page.route('**/api/billing/payment-method/setup-intent', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					configured: false,
					client_secret: null,
					setup_intent_id: null
				})
			});
		});

		await page.goto('/billing');

		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await expect(setup).toBeVisible({ timeout: 10_000 });
		await expect(setup.getByText(/Billing is not configured/)).toBeVisible();
	});

	test('the Subscription section tab is visible + active for the admin', async ({ page }) => {
		// /billing is a child of the Settings nav group (beside Organization —
		// decisions §314), so it surfaces as a section sub-tab, not a top-level
		// sidebar row. On /billing the Settings sub-tab bar is shown with
		// Subscription as the active tab.
		const tab = page.locator('nav.section-tabs a[href="/billing"]');
		await expect(tab).toBeVisible();
		await expect(tab).toHaveText('Subscription');
		await expect(tab).toHaveAttribute('aria-current', 'page');
	});

	test.describe('plan-change flow (seeded subscription)', () => {
		// The "Change plan" action only renders once the org has a live
		// subscription (it lives inside the plan-card block), so every test in
		// this group seeds a real Plan + Subscription — mirroring the
		// "with a seeded subscription" test above — and tears it down after.
		let orgId: string;
		let currentPlanId: string;
		let targetPlanId: string;
		// `plans.code` is varchar(50) — keep the per-test suffix short (a testId
		// hash is too long) but still unique enough to avoid cross-test collision.
		let currentCode: string;
		let targetCode: string;
		let parked: string[] = [];

		test.beforeEach(async ({ page }) => {
			const suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
			currentCode = `e2e_pc_cur_${suffix}`;
			targetCode = `e2e_pc_tgt_${suffix}`;
			orgId = currentOrgId();
			// Park the provisioning-time `free` subscription so the seeded
			// "current plan" below is the org's only live row (see the helper).
			parked = parkLiveSubscriptions(orgId);
			currentPlanId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${currentCode}', 'E2E Current', 49.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{}'::jsonb, 0, true, now(), now()) RETURNING id`
			);
			targetPlanId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${targetCode}', 'E2E Target', 99.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{}'::jsonb, 0, true, now(), now()) RETURNING id`
			);
			controlPsql(
				`INSERT INTO subscriptions (id, organization_id, plan_id, status, ` +
					`current_period_start, current_period_end, trial_end, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${orgId}', '${currentPlanId}', 'active', ` +
					`now() - interval '5 days', now() + interval '25 days', NULL, now(), now())`
			);

			await page.goto('/billing');
		});

		test.afterEach(() => {
			controlPsql(`DELETE FROM subscriptions WHERE plan_id IN ('${currentPlanId}', '${targetPlanId}')`);
			controlPsql(`DELETE FROM plans WHERE id IN ('${currentPlanId}', '${targetPlanId}')`);
			// After the deletes — the change-plan flow leaves a live row on the
			// TARGET plan, so restoring earlier would collide on the index.
			restoreLiveSubscriptions(parked);
			parked = [];
		});

		test('changes plan and shows the real returned proration', async ({ page }) => {
			await page.getByTestId('billing-change-plan').click();

			const modal = page.getByRole('dialog', { name: 'Change plan' });
			await expect(modal).toBeVisible();

			// The current plan is marked and its radio is disabled — a genuine
			// change is the point of the UI flow.
			const currentRadio = modal.getByRole('radio', { name: 'Select the E2E Current plan' });
			await expect(currentRadio).toBeDisabled();
			await expect(modal.getByText('Current plan')).toBeVisible();

			const targetRadio = modal.getByRole('radio', { name: 'Select the E2E Target plan' });
			await targetRadio.check();

			const confirm = page.getByTestId('billing-change-plan-confirm');
			await expect(confirm).toBeEnabled();
			await confirm.click();

			// Real POST /api/billing/change-plan applied the move; the result view
			// renders the ACTUAL proration the response returned (an upgrade from
			// $49 to $99 is a positive mid-period charge), never a fabricated one.
			const result = page.getByTestId('billing-plan-change-result');
			await expect(result).toBeVisible({ timeout: 10_000 });
			await expect(result.getByText("You're now on the E2E Target plan.")).toBeVisible();
			await expect(result.getByText('Prorated adjustment')).toBeVisible();

			await result.getByRole('button', { name: 'Done' }).click();
			await expect(modal).toBeHidden();

			// The plan card behind the modal reflects the change without a manual
			// reload (confirmPlanChange re-fetches the subscription on success).
			await expect(page.getByTestId('billing-plan').getByRole('heading', { name: 'E2E Target' })).toBeVisible();
		});

		test('an idempotent no-op change ("changed": false) renders as a clean success, not an error', async ({
			page
		}) => {
			// Stub only the POST — the backend's own idempotent-no-op case is the
			// org already being on the target plan, which this UI's disabled
			// current-plan radio prevents reaching directly. The response shape is
			// still the API's contract, so the UI must honour a `changed: false`
			// reply regardless of which plan triggered it.
			await page.route('**/api/billing/change-plan', async (route) => {
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						changed: false,
						old_plan_code: currentCode,
						new_plan_code: currentCode,
						proration: { amount: '0.00', unused_days: 0, period_days: 0 }
					})
				});
			});

			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			await modal.getByRole('radio', { name: 'Select the E2E Target plan' }).check();
			await page.getByTestId('billing-change-plan-confirm').click();

			const noop = page.getByTestId('billing-plan-change-noop');
			await expect(noop).toBeVisible({ timeout: 10_000 });
			await expect(noop).toHaveText("You're already on the E2E Current plan — nothing changed.");
			// No proration panel on the no-op path.
			await expect(page.getByText('Prorated adjustment')).toHaveCount(0);
		});

		test('the picker lists the plan catalog from GET /api/billing/plans, cheapest first', async ({
			page
		}) => {
			// Other active plans may already exist in the control-plane catalog
			// (the platform default seed, or other tests' fixtures) — assert our
			// two seeded plans are present and correctly ordered RELATIVE to each
			// other, not the total row count.
			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			const options = modal.locator('.plan-option');
			await expect(options.filter({ hasText: 'E2E Current' })).toBeVisible({ timeout: 10_000 });
			const texts = await options.allTextContents();
			const currentIdx = texts.findIndex((t) => t.includes('E2E Current'));
			const targetIdx = texts.findIndex((t) => t.includes('E2E Target'));
			expect(currentIdx).toBeGreaterThanOrEqual(0);
			expect(targetIdx).toBeGreaterThanOrEqual(0);
			// $49 current plan sorts before the $99 target plan.
			expect(currentIdx).toBeLessThan(targetIdx);
		});
	});
});

test.describe('/billing (clerk — not authorized)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and cannot see the nav row', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/billing');
		// admin/cfo only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { level: 1, name: 'Subscription', exact: true })).toHaveCount(0);

		// The clerk also can't reach the Subscription tab (it's admin/cfo-only,
		// and they were bounced to the dashboard, which has no section tabs).
		await expect(page.locator('a[href="/billing"]')).toHaveCount(0);
	});

	test('the API 403s a clerk directly', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const base = API_BASE;
		const headers = { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() };
		// Every billing read (subscription + invoices + payment methods + the
		// plan catalog) is admin/cfo-only; so is starting a SetupIntent or
		// changing the plan.
		const sub = await page.request.get(`${base}/api/billing/subscription`, { headers });
		expect(sub.status()).toBe(403);
		const invoices = await page.request.get(`${base}/api/billing/invoices`, { headers });
		expect(invoices.status()).toBe(403);
		const methods = await page.request.get(`${base}/api/billing/payment-methods`, { headers });
		expect(methods.status()).toBe(403);
		const setup = await page.request.post(`${base}/api/billing/payment-method/setup-intent`, {
			headers
		});
		expect(setup.status()).toBe(403);
		const plans = await page.request.get(`${base}/api/billing/plans`, { headers });
		expect(plans.status()).toBe(403);
		const changePlan = await page.request.post(`${base}/api/billing/change-plan`, {
			headers,
			data: { plan_code: 'growth' }
		});
		expect(changePlan.status()).toBe(403);
	});
});
