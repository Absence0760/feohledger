import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import {
	buildCommercePayload,
	buildCommerceSecretUpdate,
	buildCommerceTestPayload,
	COMMERCE_SYNC_REASON_KEYS,
	COMMERCE_SYNC_STATUS_KEYS,
	commerceDestinationChanged,
	commerceSecretIsSaved,
	initialCommerceValues,
	missingCommerceFields,
	syncReasonKey,
	type CommerceProvider
} from './commerce';

const shop: CommerceProvider = {
	key: 'shopx',
	label: 'Shop X',
	auth: 'credentials',
	regions: [],
	docs_url: 'https://example.invalid',
	supports_fractional_quantities: false,
	available: true,
	fields: [
		{
			name: 'shop_domain',
			label_key: 'org.commerce.field.shop_domain',
			secret: false,
			required: true,
			destination: true
		},
		{ name: 'access_token', label_key: 'org.commerce.field.access_token', secret: true, required: true },
		{
			name: 'environment',
			label_key: 'org.commerce.field.environment',
			secret: false,
			required: false,
			options: ['production', 'sandbox']
		}
	]
};

describe('the commerce connection form', () => {
	const stored = { provider: 'shopx', shop_domain: 'a.example.com', location_id: 'loc-1' };
	const sealed = ['access_token'];

	it('starts every secret blank and shows the stored configuration', () => {
		expect(initialCommerceValues(shop, stored)).toEqual({
			shop_domain: 'a.example.com',
			access_token: '',
			environment: 'production'
		});
		expect(commerceSecretIsSaved(shop, shop.fields[1], stored, sealed)).toBe(true);
		expect(commerceSecretIsSaved(shop, shop.fields[1], stored, [])).toBe(false);
	});

	it('stops calling a secret saved once a destination field moves', () => {
		const moved = { shop_domain: 'b.example.com', access_token: '', environment: 'production' };
		expect(commerceDestinationChanged(shop, moved, stored)).toBe(true);
		expect(commerceSecretIsSaved(shop, shop.fields[1], stored, sealed, moved)).toBe(false);
	});

	it('starts blank for another platform', () => {
		expect(initialCommerceValues(shop, { provider: 'mock' }).shop_domain).toBe('');
		expect(commerceSecretIsSaved(shop, shop.fields[1], { provider: 'mock' }, sealed)).toBe(false);
	});

	it('sends configuration to settings and secrets to the credentials endpoint', () => {
		const values = { shop_domain: 'a.example.com', access_token: 'tok', environment: 'sandbox' };
		expect(buildCommercePayload(shop, values)).toEqual({
			provider: 'shopx',
			shop_domain: 'a.example.com',
			environment: 'sandbox'
		});
		expect(buildCommerceSecretUpdate(shop, values)).toEqual({ set: { access_token: 'tok' } });
		expect(buildCommerceSecretUpdate(shop, { ...values, access_token: '' })).toBeNull();
		expect(
			buildCommerceSecretUpdate(shop, { ...values, access_token: '' }, { access_token: true })
		).toEqual({ clear: ['access_token'] });
		// A test carries what was typed; a blank secret is left to the server.
		expect(buildCommerceTestPayload(shop, values).access_token).toBe('tok');
		expect(buildCommerceTestPayload(shop, { ...values, access_token: '' })).not.toHaveProperty(
			'access_token'
		);
	});

	it('counts a kept sealed secret as filled, a removed one as missing', () => {
		const values = { shop_domain: 'a.example.com', access_token: '', environment: 'production' };
		expect(missingCommerceFields(shop, values, stored, sealed)).toEqual([]);
		expect(
			missingCommerceFields(shop, values, stored, sealed, { access_token: true }).map(
				(f) => f.name
			)
		).toEqual(['access_token']);
	});
});

describe('the sync status vocabulary', () => {
	it('has a catalogued label for every status and reason', () => {
		for (const key of [
			...Object.values(COMMERCE_SYNC_STATUS_KEYS),
			...Object.values(COMMERCE_SYNC_REASON_KEYS)
		]) {
			expect(en, key).toHaveProperty([key]);
		}
	});

	it('falls back for a reason this build predates', () => {
		expect(syncReasonKey('no_sku')).toBe('commerceSync.reason.no_sku');
		expect(syncReasonKey('shopify_throttle')).toBe('commerceSync.reason.other');
		expect(syncReasonKey(null)).toBeNull();
	});
});
