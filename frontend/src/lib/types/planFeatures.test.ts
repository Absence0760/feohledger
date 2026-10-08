/**
 * The client's plan-feature map must agree with the backend catalog
 * (`backend/app/services/billing/plan_catalog.py`, decisions §253): the same
 * feature keys, and each feature's cheapest tier the one the catalog actually
 * grants it on. A drift here would send an admin to upgrade to a tier that
 * does not include the feature — or label a control "Scale only" that Growth
 * already unlocks.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import {
	FEATURE_MIN_TIER,
	PLAN_FEATURES,
	isPlanFeature,
	planFeatureLabelKey,
	planTierLabelKey
} from './planFeatures';

const CATALOG = readFileSync(
	new URL('../../../../backend/app/services/billing/plan_catalog.py', import.meta.url),
	'utf8'
);

/** `FEATURE_X = "x"` → { FEATURE_X: 'x' } */
function backendFeatures(): Record<string, string> {
	const out: Record<string, string> = {};
	for (const match of CATALOG.matchAll(/^(FEATURE_[A-Z_]+) = "([a-z_]+)"/gm)) {
		out[match[1]] = match[2];
	}
	return out;
}

/** The constant names a catalog plan's `entitlements` block lists. */
function grantedBy(code: string): Set<string> | 'all' {
	const start = CATALOG.indexOf(`"code": "${code}"`);
	expect(start, `no "${code}" plan in the catalog`).toBeGreaterThan(-1);
	const block = CATALOG.slice(start, CATALOG.indexOf('"usage_components"', start));
	if (block.includes('for feature in ALL_FEATURES')) return 'all';
	return new Set(block.match(/FEATURE_[A-Z_]+/g) ?? []);
}

describe('plan features', () => {
	it('names exactly the backend FEATURE_* keys', () => {
		expect([...PLAN_FEATURES].sort()).toEqual(Object.values(backendFeatures()).sort());
	});

	it('pins each feature to the cheapest tier that grants it', () => {
		const byKey = Object.fromEntries(
			Object.entries(backendFeatures()).map(([name, key]) => [key, name])
		);
		const growth = grantedBy('growth');
		const scale = grantedBy('scale');
		expect(grantedBy('free')).toEqual(new Set());
		for (const feature of PLAN_FEATURES) {
			const constant = byKey[feature];
			const onGrowth = growth === 'all' || growth.has(constant);
			const onScale = scale === 'all' || scale.has(constant);
			expect(onScale, `${feature} is not granted by scale`).toBe(true);
			expect(FEATURE_MIN_TIER[feature]).toBe(onGrowth ? 'growth' : 'scale');
		}
	});

	it('labels every feature and tier from the catalogue', () => {
		for (const feature of PLAN_FEATURES) {
			expect(en[planFeatureLabelKey(feature)]?.trim()).toBeTruthy();
			expect(en[planTierLabelKey(feature)]?.trim()).toBeTruthy();
		}
	});

	it('recognises only known keys', () => {
		expect(isPlanFeature('sso')).toBe(true);
		expect(isPlanFeature('max_seats')).toBe(false);
		expect(isPlanFeature(undefined)).toBe(false);
	});
});
