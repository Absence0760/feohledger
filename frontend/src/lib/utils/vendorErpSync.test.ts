import { describe, expect, it } from 'vitest';
import { FEATURE_ERP_INTEGRATIONS, FEATURE_ERP_MERGE } from '#lib/types/planFeatures.ts';
import { vendorErpSyncOffer } from './vendorErpSync.ts';

const all = () => true;
const freePlan = (f: string) => f === FEATURE_ERP_INTEGRATIONS;
const none = () => false;

describe('vendorErpSyncOffer', () => {
	it('points at setup when nothing is configured', () => {
		expect(vendorErpSyncOffer(undefined, all)).toEqual({ kind: 'connect' });
		expect(vendorErpSyncOffer(null, all)).toEqual({ kind: 'connect' });
		expect(vendorErpSyncOffer({}, all)).toEqual({ kind: 'connect' });
	});

	it('offers sync for a direct ERP on a plan with ERP integrations (Free included)', () => {
		expect(vendorErpSyncOffer({ type: 'netsuite', integration_method: 'direct' }, freePlan)).toEqual({
			kind: 'sync'
		});
		// The non-admin projection carries only the route.
		expect(vendorErpSyncOffer({ integration_method: 'direct' }, freePlan)).toEqual({ kind: 'sync' });
	});

	it('asks for Scale when the route is Merge.dev and the plan lacks it', () => {
		expect(vendorErpSyncOffer({ type: 'sage', integration_method: 'merge_dev' }, freePlan)).toEqual({
			kind: 'upgrade',
			feature: FEATURE_ERP_MERGE
		});
		// No integration_method defaults to Merge.dev, as the backend dispatcher does.
		expect(vendorErpSyncOffer({ type: 'sage' }, freePlan)).toEqual({
			kind: 'upgrade',
			feature: FEATURE_ERP_MERGE
		});
		expect(vendorErpSyncOffer({ integration_method: 'merge_dev' }, all)).toEqual({ kind: 'sync' });
	});

	it('never plan-gates the mock ERP', () => {
		expect(vendorErpSyncOffer({ type: 'mock', integration_method: 'direct' }, none)).toEqual({
			kind: 'sync'
		});
	});

	it('gates a live direct ERP on erp_integrations', () => {
		expect(vendorErpSyncOffer({ type: 'netsuite', integration_method: 'direct' }, none)).toEqual({
			kind: 'upgrade',
			feature: FEATURE_ERP_INTEGRATIONS
		});
	});

	// Each input below is one the old truthiness check answered differently
	// from `dispatcher.resolve_adapter_key`, which defaults ONLY a missing key.
	it('treats a present-but-blank integration_method as a direct route, as the backend does', () => {
		// Backend: "" != "merge_dev" → direct → type "netsuite" → live, gated on
		// erp_integrations only. The old check read "" as merge_dev and asked
		// for Scale.
		for (const method of ['', null]) {
			expect(vendorErpSyncOffer({ type: 'netsuite', integration_method: method }, freePlan)).toEqual({
				kind: 'sync'
			});
			expect(vendorErpSyncOffer({ type: 'netsuite', integration_method: method }, none)).toEqual({
				kind: 'upgrade',
				feature: FEATURE_ERP_INTEGRATIONS
			});
			// ...and with a blank type that direct route is the never-gated mock.
			expect(vendorErpSyncOffer({ type: '', integration_method: method }, none)).toEqual({
				kind: 'sync'
			});
		}
	});

	it('reads a direct route with a blank type as the mock adapter, never plan-gated', () => {
		for (const type of ['', null]) {
			expect(vendorErpSyncOffer({ type, integration_method: 'direct' }, none)).toEqual({
				kind: 'sync'
			});
		}
	});

	it('still gates a direct route whose type the non-admin projection stripped', () => {
		// Absent `type` is the projection, not a mock: no claim about the adapter.
		expect(vendorErpSyncOffer({ integration_method: 'direct' }, none)).toEqual({
			kind: 'upgrade',
			feature: FEATURE_ERP_INTEGRATIONS
		});
	});
});
