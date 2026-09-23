import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	test
} from '../fixtures/helpers';

interface FraudRules {
	round_amount_enabled: boolean;
	future_date_enabled: boolean;
	bank_change_enabled: boolean;
	stat_anomaly_enabled: boolean;
	rush_payment_enabled: boolean;
	new_vendor_large_enabled: boolean;
	personal_email_enabled: boolean;
	llm_anomaly_enabled: boolean;
	round_amount_min: string;
	rush_payment_max_days: number;
	new_vendor_max_age_days: number;
	new_vendor_large_amount: string;
	stat_anomaly_sigma: number;
	stat_anomaly_min_history: number;
	personal_email_domains: string[];
}

interface OrgResponse {
	settings: { fraud_rules?: Partial<FraudRules> };
}

async function getOrg(page: import('@playwright/test').Page): Promise<OrgResponse> {
	const resp = await page.request.get(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page)
	});
	return (await resp.json()) as OrgResponse;
}

async function getFraudDefaults(page: import('@playwright/test').Page): Promise<FraudRules> {
	const resp = await page.request.get(
		`${API_BASE}/api/organization/fraud-rules/defaults`,
		{ headers: await authedTenantHeaders(page) }
	);
	return (await resp.json()) as FraudRules;
}

async function clearFraudOverrides(page: import('@playwright/test').Page): Promise<void> {
	// Setting fraud_rules to {} drops every override and the engine falls
	// back to defaults.
	await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await authedTenantHeaders(page),
		data: { settings: { fraud_rules: {} } }
	});
}

/**
 * /organization Fraud Detection panel. The org-level toggles + thresholds
 * are checked by the warning engine on every invoice mutation
 * (`services/invoice_warnings.refresh_warnings`); these specs assert the
 * UI reads/writes the right shape so a stale UI can't drift away from
 * `DEFAULT_FRAUD_RULES`.
 *
 * `?section=fraud` because the route shows one panel at a time now (the slug
 * is part of its URL contract) — and this panel is the one that has to be
 * asked for by name: `GET /api/organization/fraud-rules/defaults` only fires
 * once the panel is shown, and the card does not render at all until it
 * answers.
 */

test.describe('/organization — fraud rules', () => {
	test.beforeEach(async ({ page }) => {
		await clearFraudOverrides(page);
		await page.goto('/organization?section=fraud');
	});

	test.afterAll(async ({ browser }) => {
		// Leave the tenant in a clean state for the next run. The new
		// context needs the worker's tenant baseURL since it's outside
		// the per-test fixture.
		const ctx = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await ctx.newPage();
		try {
			await signInAndWait(page);
			await clearFraudOverrides(page);
		} finally {
			await ctx.close();
		}
	});

	test('GET /api/organization/fraud-rules/defaults returns the canonical shape', async ({
		page
	}) => {
		const defaults = await getFraudDefaults(page);
		expect(defaults).toMatchObject({
			round_amount_enabled: true,
			future_date_enabled: true,
			bank_change_enabled: true,
			stat_anomaly_enabled: true,
			rush_payment_enabled: true,
			new_vendor_large_enabled: true,
			personal_email_enabled: true,
			llm_anomaly_enabled: false
		});
		expect(Array.isArray(defaults.personal_email_domains)).toBe(true);
		expect(defaults.personal_email_domains).toContain('gmail.com');
	});

	test('Fraud Detection card renders with all 8 rules and their hints', async ({ page }) => {
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });
		await expect(card).toBeVisible({ timeout: 5_000 });

		// Each rule's name renders inside a <strong> in the switch-row's
		// label — exact-match on that strong tag avoids the textarea label
		// re-matching "Personal email domain".
		for (const label of [
			'Round amounts',
			'Future invoice date',
			'Rush payment',
			'New vendor + large amount',
			'Bank / remit-to change',
			'Personal email domain',
			'Statistical amount anomaly',
			'LLM-based anomaly check'
		]) {
			await expect(card.locator('strong', { hasText: label })).toBeVisible();
		}
	});

	test('toggling round amounts off + saving persists to settings', async ({ page }) => {
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });
		const roundAmountSwitch = card
			.locator('label.switch-row', { hasText: 'Round amounts' })
			.locator('input[type="checkbox"]');

		// Default is on — flip off.
		await expect(roundAmountSwitch).toBeChecked();
		await roundAmountSwitch.uncheck();

		const saved = page.waitForResponse(
			(r) => r.url().endsWith('/api/organization') && r.request().method() === 'PATCH'
		);
		await card.getByRole('button', { name: 'Save' }).click();
		await saved;

		// The settings now carry the override.
		const after = await getOrg(page);
		expect(after.settings.fraud_rules?.round_amount_enabled).toBe(false);
	});

	test('threshold input round-trips: bumping rush_payment_max_days', async ({ page }) => {
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });
		const rushDaysInput = card
			.locator('.threshold-row', { hasText: 'Max days between invoice + due' })
			.locator('input[type="number"]');

		await rushDaysInput.fill('7');

		const saved = page.waitForResponse(
			(r) => r.url().endsWith('/api/organization') && r.request().method() === 'PATCH'
		);
		await card.getByRole('button', { name: 'Save' }).click();
		await saved;

		const after = await getOrg(page);
		expect(after.settings.fraud_rules?.rush_payment_max_days).toBe(7);
	});

	test('personal email domains: editing the textarea persists as a list', async ({ page }) => {
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });
		const textarea = card.locator('textarea');

		// Replace the default block with a smaller, distinctive list.
		await textarea.fill('throwaway.test\nfraud-test.example, demo.invalid');

		const saved = page.waitForResponse(
			(r) => r.url().endsWith('/api/organization') && r.request().method() === 'PATCH'
		);
		await card.getByRole('button', { name: 'Save' }).click();
		await saved;

		const after = await getOrg(page);
		// Both newline AND comma separators are split into discrete entries.
		expect(after.settings.fraud_rules?.personal_email_domains).toEqual([
			'throwaway.test',
			'fraud-test.example',
			'demo.invalid'
		]);
	});

	test('Reset to defaults reverts the form (without saving)', async ({ page }) => {
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });

		// First save an override so we can see the reset undo it.
		const rushDaysInput = card
			.locator('.threshold-row', { hasText: 'Max days between invoice + due' })
			.locator('input[type="number"]');
		await rushDaysInput.fill('14');

		// Click reset — form value flips back to the canonical default (3).
		await card.getByRole('button', { name: 'Reset to defaults' }).click();
		await expect(rushDaysInput).toHaveValue('3');
	});

	test('clerk role gets 403 from the defaults endpoint', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const resp = await page.request.get(
			`${API_BASE}/api/organization/fraud-rules/defaults`,
			{ headers: await authedTenantHeaders(page) }
		);
		expect(resp.status()).toBe(403);
	});
});
