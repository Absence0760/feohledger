/**
 * The public pricing grid, derived from the plan catalogue.
 *
 * Every figure `Pricing.svelte` shows comes from here, and everything here
 * comes from `plans.generated.ts`, which `pnpm gen:pricing` writes from
 * `backend/app/services/billing/plan_catalog.py` (docs/decisions.md §253). The
 * page used to hand-type its grid and ended up selling a per-seat plan the
 * billing code never modelled (issue #426); now a catalogue change is a red
 * `pnpm check:pricing` until the page says the same thing.
 *
 * Pure (no `$app`, no `.svelte`), so `pricing.test.ts` checks the grid against
 * the generated module directly.
 */

import type { MessageKey } from '#lib/i18n/messages.ts';
import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
import { formatMoney } from '#lib/utils/money.ts';
import { PLAN_CURRENCY, PLANS, type CatalogPlan, type PlanFeature } from './plans.generated.ts';

/**
 * One label per gated feature. Typed over the generated union, so a feature
 * the catalogue gains without a label here is a `pnpm check` failure rather
 * than a bullet that silently never renders.
 */
export const FEATURE_LABEL_KEYS = {
	public_api: 'marketing.pricing.feature.publicApi',
	erp_integrations: 'marketing.pricing.feature.erpIntegrations',
	sso: 'marketing.pricing.feature.sso',
	scim: 'marketing.pricing.feature.scim',
	sso_enforcement: 'marketing.pricing.feature.ssoEnforcement',
	multi_entity: 'marketing.pricing.feature.multiEntity',
	audit_siem_export: 'marketing.pricing.feature.auditSiemExport',
} as const satisfies Record<PlanFeature, MessageKey>;

/**
 * What every plan includes — the ungated core of the product. Not a catalogue
 * entitlement (nothing gates them), so they are copy, but copy with no figure
 * in it: `pricing.test.ts` refuses a digit in any pricing string.
 */
export const CORE_FEATURE_KEYS: readonly MessageKey[] = [
	'marketing.pricing.core.capture',
	'marketing.pricing.core.approvals',
	'marketing.pricing.core.matching',
	'marketing.pricing.core.payments',
];

/** Per-tier one-liners. Keyed by plan code; a code without one shows none. */
export const TAGLINE_KEYS: Readonly<Record<string, MessageKey>> = {
	free: 'marketing.pricing.tagline.free',
	growth: 'marketing.pricing.tagline.growth',
	scale: 'marketing.pricing.tagline.scale',
};

/** The visually emphasised card — a presentation choice, not a price. */
export const FEATURED_PLAN_CODE = 'growth';

export interface PricingTier {
	readonly code: string;
	readonly name: string;
	readonly tagline: MessageKey | null;
	/** The monthly price, formatted in the catalogue's own currency. */
	readonly price: string;
	/** The AI-read allowance as a number (for plural selection)… */
	readonly included: number;
	/** …and as the active locale writes it (`3,000`, `3.000`). */
	readonly includedLabel: string;
	/** Price per AI-read invoice past the allowance, formatted, or `null`:
	 *  AI reading pauses and nothing else does. */
	readonly overage: string | null;
	/** The previous tier's name when this one builds on it. */
	readonly buildsOn: string | null;
	/** Features this tier adds over the one before, in catalogue order. */
	readonly addedFeatures: readonly MessageKey[];
	readonly featured: boolean;
}

/** True when a decimal string has no non-zero fractional digits. */
function isWholeAmount(amount: string): boolean {
	return /^-?\d+(\.0*)?$/.test(amount);
}

/**
 * A catalogue price, in the catalogue's currency. Whole amounts drop the
 * cents (`$49`, not `$49.00`); anything else keeps them, so a price can never
 * be rounded into a different figure than the one billed.
 */
export function formatPlanPrice(amount: string, locale?: string): string {
	return formatMoney(amount, {
		currency: PLAN_CURRENCY,
		whole: isWholeAmount(amount),
		locale: locale ?? getActiveFormatLocale(),
	});
}

/** An overage unit price — always with its minor units (`$0.10`). */
export function formatOveragePrice(amount: string, locale?: string): string {
	return formatMoney(amount, { currency: PLAN_CURRENCY, locale: locale ?? getActiveFormatLocale() });
}

export function formatCount(n: number, locale?: string): string {
	return new Intl.NumberFormat(locale ?? getActiveFormatLocale()).format(n);
}

/** The grid, one tier per catalogue plan, in catalogue order. */
export function pricingTiers(
	plans: readonly CatalogPlan[] = PLANS,
	locale?: string
): PricingTier[] {
	return plans.map((plan, i) => {
		const previous = i > 0 ? plans[i - 1] : null;
		const inherited = new Set<PlanFeature>(previous?.features ?? []);
		return {
			code: plan.code,
			name: plan.name,
			tagline: TAGLINE_KEYS[plan.code] ?? null,
			price: formatPlanPrice(plan.monthlyPrice, locale),
			included: plan.includedAiInvoices,
			includedLabel: formatCount(plan.includedAiInvoices, locale),
			overage:
				plan.overageUnitPrice === null ? null : formatOveragePrice(plan.overageUnitPrice, locale),
			buildsOn: previous?.name ?? null,
			addedFeatures: plan.features
				.filter((f) => !inherited.has(f))
				.map((f) => FEATURE_LABEL_KEYS[f]),
			featured: plan.code === FEATURED_PLAN_CODE,
		};
	});
}

/** The top catalogue plan's name — what the Enterprise card builds on. */
export function topPlanName(plans: readonly CatalogPlan[] = PLANS): string {
	return plans[plans.length - 1].name;
}
