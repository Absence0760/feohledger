import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';
// A VALUE import from `#lib`, under the narrow exception in frontend/CLAUDE.md:
// the generated plan module is pure by design (no imports at all) and says so
// in its header. The spec compares the rendered grid with the catalogue the
// page was generated from instead of re-typing a single price.
import { PLAN_CURRENCY, PLANS } from '#lib/marketing/plans.generated.ts';

/**
 * The public pricing grid (`Pricing.svelte`) renders the plan catalogue
 * (`backend/app/services/billing/plan_catalog.py`, via `pnpm gen:pricing`),
 * docs/decisions.md §253. Issue #426 was this page selling per-seat plans, an
 * annual toggle and a trial the billing code never modelled; these pin the
 * rendered page to the catalogue and keep those claims off it.
 *
 * Expected figures are formatted here with `Intl` in `en-US` — the locale the
 * suite's browser runs in — from the generated values, never typed.
 */

test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
	// No reveal transitions to wait through; the copy is what is under test.
	await page.emulateMedia({ reducedMotion: 'reduce' });
});

function usd(amount: string, whole: boolean): string {
	return new Intl.NumberFormat('en-US', {
		style: 'currency',
		currency: PLAN_CURRENCY,
		...(whole ? { minimumFractionDigits: 0, maximumFractionDigits: 0 } : {}),
	}).format(Number(amount));
}

async function gotoPricing(page: import('@playwright/test').Page) {
	await page.goto('/#pricing');
	await expect(
		page.getByRole('heading', { level: 2, name: 'Simple plans. No sales call required.' })
	).toBeVisible();
	return page.locator('#pricing');
}

test.describe('pricing — the grid is the plan catalogue', () => {
	test('one card per catalogue plan, then Enterprise, in order', async ({ page }) => {
		const pricing = await gotoPricing(page);
		await expect(pricing.locator('.plan')).toHaveCount(PLANS.length + 1);
		const codes = await pricing
			.locator('.plan')
			.evaluateAll((els) => els.map((el) => el.getAttribute('data-plan')));
		expect(codes).toEqual([...PLANS.map((p) => p.code), 'enterprise']);
		for (const plan of PLANS) {
			await expect(
				pricing.getByRole('heading', { level: 3, name: plan.name, exact: true })
			).toBeVisible();
		}
	});

	for (const plan of PLANS) {
		test(`${plan.code}: price, allowance and past-the-limit come from the catalogue`, async ({
			page
		}) => {
			const pricing = await gotoPricing(page);
			const card = pricing.locator(`[data-plan="${plan.code}"]`);
			const whole = Number.isInteger(Number(plan.monthlyPrice));
			await expect(card.getByTestId('plan-price')).toHaveText(usd(plan.monthlyPrice, whole));
			await expect(card).toContainText('Unlimited users');
			await expect(card.getByTestId('plan-included')).toHaveText(
				`${new Intl.NumberFormat('en-US').format(plan.includedAiInvoices)} AI-read invoices a month`
			);
			await expect(card.getByTestId('plan-past-limit')).toHaveText(
				plan.overageUnitPrice === null
					? 'Past that, AI reading pauses. Everything else keeps working.'
					: `Then ${usd(plan.overageUnitPrice, false)} per extra AI-read invoice.`
			);
			// Every workspace starts on the first plan; nothing grants a trial.
			const cta = card.locator('a.plan-cta');
			await expect(cta).toHaveAttribute('href', '/signup');
			await expect(cta).not.toContainText(/trial/i);
		});
	}

	test('Enterprise is contact-sales on the published address', async ({ page }) => {
		const pricing = await gotoPricing(page);
		const card = pricing.locator('[data-plan="enterprise"]');
		await expect(card.getByRole('link', { name: 'Contact sales' })).toHaveAttribute(
			'href',
			'mailto:sales@feohledger.com'
		);
		await expect(card).toContainText(`Everything in ${PLANS[PLANS.length - 1].name}, plus:`);
	});

	test('no seat price, annual toggle, trial or popularity claim survives', async ({ page }) => {
		const pricing = await gotoPricing(page);
		await expect(pricing).not.toContainText(/per seat|seat minimum|annual|trial|most popular/i);
		await expect(pricing.getByRole('tablist')).toHaveCount(0);
		// What does count is explained on the page itself.
		await expect(
			pricing.getByRole('heading', { level: 3, name: 'What counts as an AI-read invoice?' })
		).toBeVisible();
	});
});
