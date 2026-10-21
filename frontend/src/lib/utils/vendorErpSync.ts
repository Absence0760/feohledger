import {
	FEATURE_ERP_INTEGRATIONS,
	FEATURE_ERP_MERGE,
	type PlanFeature
} from '#lib/types/planFeatures.ts';

/**
 * What the vendor list offers in place of "Sync from ERP" (#635 finding 21).
 *
 * `POST /api/vendors/sync-erp` answers 400 when no ERP is configured and 402
 * when the configured route is not on the plan (`deps.ensure_live_erp_entitled`).
 * The button used to check only the caller's role, so a new workspace got a
 * button whose one outcome was an error toast.
 *
 * - `sync`     — offer the button.
 * - `connect`  — nothing is configured: point at Organization → ERP.
 * - `upgrade`  — configured, but `feature` is not on the plan.
 *
 * `stored` is `settings.erp` from `GET /api/organization`. A non-admin reads a
 * projection holding only `integration_method` (`org_settings_view`), which the
 * ERP form always writes, so the ROUTE is read from it. `type` is consulted
 * only to recognise the never-gated `mock` adapter, which a non-admin cannot
 * see — for them a direct route counts as live and gated on
 * `erp_integrations`, which every plan holds. An ERP block written without
 * `integration_method` (by hand or an older client) is invisible to a
 * non-admin, who then sees no sync offer at all. The route rule mirrors the backend's `dispatcher.resolve_adapter_key`:
 * `integration_method` defaults to `merge_dev`, and `merge_dev` wins whatever
 * the type; a direct `mock` is never plan-gated.
 */
export type VendorErpSyncOffer =
	| { kind: 'sync' }
	| { kind: 'connect' }
	| { kind: 'upgrade'; feature: PlanFeature };

export function vendorErpSyncOffer(
	stored: Record<string, unknown> | null | undefined,
	hasFeature: (feature: PlanFeature) => boolean
): VendorErpSyncOffer {
	if (!stored || typeof stored !== 'object' || Object.keys(stored).length === 0) {
		return { kind: 'connect' };
	}
	const method = typeof stored.integration_method === 'string' ? stored.integration_method : '';
	const type = typeof stored.type === 'string' ? stored.type : '';
	if ((method || 'merge_dev') === 'merge_dev') {
		if (!hasFeature(FEATURE_ERP_INTEGRATIONS)) return { kind: 'upgrade', feature: FEATURE_ERP_INTEGRATIONS };
		if (!hasFeature(FEATURE_ERP_MERGE)) return { kind: 'upgrade', feature: FEATURE_ERP_MERGE };
		return { kind: 'sync' };
	}
	if (type === 'mock') return { kind: 'sync' };
	if (!hasFeature(FEATURE_ERP_INTEGRATIONS)) return { kind: 'upgrade', feature: FEATURE_ERP_INTEGRATIONS };
	return { kind: 'sync' };
}
