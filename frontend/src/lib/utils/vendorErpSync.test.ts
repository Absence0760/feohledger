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
});
