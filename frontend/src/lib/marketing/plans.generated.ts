// GENERATED FILE — do not edit by hand.
//
// Source of truth: backend/app/services/billing/plan_catalog.py
// Regenerate:      pnpm gen:pricing
// Drift check:     pnpm check:pricing  (runs in CI)
//
// The public pricing page renders every price, allowance, overage rate
// and feature from this module (docs/decisions.md §253, issue #426), so
// a catalogue change is a red CI check until this file is regenerated.
// Money is an exact decimal STRING — format it with formatMoney, never
// parse it into arithmetic.

/** The ISO 4217 code every catalog plan is priced in. */
export const PLAN_CURRENCY = 'USD';

/** Every gated feature, in catalogue order (`ALL_FEATURES`). */
export const PLAN_FEATURES = [
	'public_api',
	'erp_integrations',
	'sso',
	'scim',
	'sso_enforcement',
	'multi_entity',
	'audit_siem_export',
] as const;

export type PlanFeature = (typeof PLAN_FEATURES)[number];

export interface CatalogPlan {
	/** Stable machine code (`Plan.code`). */
	readonly code: string;
	/** Display name. A proper noun; not translated. */
	readonly name: string;
	/** Flat price per organisation per month, as an exact decimal string. */
	readonly monthlyPrice: string;
	/** AI-read invoices the monthly price covers. */
	readonly includedAiInvoices: number;
	/** Price per AI-read invoice past the allowance, or `null`: AI reading
	 *  pauses at the limit and nothing else stops. */
	readonly overageUnitPrice: string | null;
	/** `Plan.trial_days`. Carried for completeness — nothing grants a trial
	 *  yet, so the page must not promise one. */
	readonly trialDays: number;
	/** Features the plan grants, in catalogue order. */
	readonly features: readonly PlanFeature[];
}

/** `DEFAULT_PLAN_CATALOG`, in catalogue order. Enterprise is not here: it is a
 *  negotiated contract, shown on the page as contact-sales. */
export const PLANS = [
	{
		code: 'free',
		name: 'Free',
		monthlyPrice: '0.00',
		includedAiInvoices: 100,
		overageUnitPrice: null,
		trialDays: 0,
		features: [],
	},
	{
		code: 'growth',
		name: 'Growth',
		monthlyPrice: '49.00',
		includedAiInvoices: 500,
		overageUnitPrice: '0.10',
		trialDays: 14,
		features: [
			'public_api',
			'erp_integrations',
			'sso',
		],
	},
	{
		code: 'scale',
		name: 'Scale',
		monthlyPrice: '199.00',
		includedAiInvoices: 3000,
		overageUnitPrice: '0.07',
		trialDays: 14,
		features: [
			'public_api',
			'erp_integrations',
			'sso',
			'scim',
			'sso_enforcement',
			'multi_entity',
			'audit_siem_export',
		],
	},
] as const satisfies readonly CatalogPlan[];
