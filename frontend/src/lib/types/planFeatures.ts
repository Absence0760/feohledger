/**
 * Plan features — the client's copy of `backend/app/services/billing/
 * plan_catalog.py`'s `FEATURE_*` keys and which catalog tier first grants each
 * (docs/decisions.md §253; the gates themselves are §254).
 *
 * The server is the authority: every gated route refuses with a coded 402
 * (`plan_feature_required`, `params.feature`) whatever the client shows. This
 * map only lets a page render "Available on Growth — upgrade" in place of a
 * control that would 402, and lets that refusal's toast name the tier.
 * `planFeatures.test.ts` reads the backend catalog and fails if the two drift.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

export const FEATURE_PUBLIC_API = 'public_api';
export const FEATURE_ERP_INTEGRATIONS = 'erp_integrations';
export const FEATURE_SSO = 'sso';
export const FEATURE_SCIM = 'scim';
export const FEATURE_SSO_ENFORCEMENT = 'sso_enforcement';
export const FEATURE_MULTI_ENTITY = 'multi_entity';
export const FEATURE_AUDIT_SIEM_EXPORT = 'audit_siem_export';

export type PlanFeature =
	| typeof FEATURE_PUBLIC_API
	| typeof FEATURE_ERP_INTEGRATIONS
	| typeof FEATURE_SSO
	| typeof FEATURE_SCIM
	| typeof FEATURE_SSO_ENFORCEMENT
	| typeof FEATURE_MULTI_ENTITY
	| typeof FEATURE_AUDIT_SIEM_EXPORT;

/** The self-serve catalog tiers that grant features (Free grants none). */
export type PaidTier = 'growth' | 'scale';

/** The cheapest catalog tier that grants each feature. */
export const FEATURE_MIN_TIER: Record<PlanFeature, PaidTier> = {
	[FEATURE_PUBLIC_API]: 'growth',
	[FEATURE_ERP_INTEGRATIONS]: 'growth',
	[FEATURE_SSO]: 'growth',
	[FEATURE_SCIM]: 'scale',
	[FEATURE_SSO_ENFORCEMENT]: 'scale',
	[FEATURE_MULTI_ENTITY]: 'scale',
	[FEATURE_AUDIT_SIEM_EXPORT]: 'scale'
};

export const PLAN_FEATURES = Object.keys(FEATURE_MIN_TIER) as PlanFeature[];

const FEATURE_LABEL_KEYS: Record<PlanFeature, MessageKey> = {
	[FEATURE_PUBLIC_API]: 'planFeature.publicApi',
	[FEATURE_ERP_INTEGRATIONS]: 'planFeature.erpIntegrations',
	[FEATURE_SSO]: 'planFeature.sso',
	[FEATURE_SCIM]: 'planFeature.scim',
	[FEATURE_SSO_ENFORCEMENT]: 'planFeature.ssoEnforcement',
	[FEATURE_MULTI_ENTITY]: 'planFeature.multiEntity',
	[FEATURE_AUDIT_SIEM_EXPORT]: 'planFeature.auditSiemExport'
};

const TIER_LABEL_KEYS: Record<PaidTier, MessageKey> = {
	growth: 'planTier.growth',
	scale: 'planTier.scale'
};

export function isPlanFeature(value: unknown): value is PlanFeature {
	return typeof value === 'string' && value in FEATURE_MIN_TIER;
}

export function planFeatureLabelKey(feature: PlanFeature): MessageKey {
	return FEATURE_LABEL_KEYS[feature];
}

export function planTierLabelKey(feature: PlanFeature): MessageKey {
	return TIER_LABEL_KEYS[FEATURE_MIN_TIER[feature]];
}
