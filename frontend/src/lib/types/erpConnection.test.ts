import { describe, expect, it } from 'vitest';
import {
	buildErpPayload,
	groupProviders,
	initialValues,
	missingRequired,
	readOAuthReturn,
	secretIsSaved,
	selectedProviderKey,
	type ErpProvider
} from './erpConnection';

const MASK = '********';

const netsuite: ErpProvider = {
	key: 'netsuite',
	label: 'Oracle NetSuite',
	regions: ['US', 'ZA'],
	auth: 'credentials',
	docs_url: 'https://example.invalid/ns',
	available: true,
	fields: [
		{ name: 'account_id', label_key: 'org.erp.accountId', secret: false, required: true },
		{ name: 'consumer_secret', label_key: 'org.erp.consumerSecret', secret: true, required: true }
	]
};

const qbo: ErpProvider = {
	key: 'quickbooks_online',
	label: 'QuickBooks Online',
	regions: ['US'],
	auth: 'oauth',
	docs_url: 'https://example.invalid/qbo',
	available: false,
	fields: [
		{ name: 'client_id', label_key: 'org.erp.clientId', secret: false, required: false },
		{ name: 'client_secret', label_key: 'org.erp.clientSecret', secret: true, required: false },
		{
			name: 'environment',
			label_key: 'org.erp.environment',
			secret: false,
			required: true,
			options: ['production', 'sandbox']
		}
	]
};

const syspro: ErpProvider = { ...netsuite, key: 'syspro', label: 'SYSPRO', regions: ['ZA'] };

const merge: ErpProvider = {
	key: 'merge_dev',
	label: 'Other ERP via Merge.dev',
	regions: [],
	auth: 'credentials',
	docs_url: 'https://example.invalid/merge',
	available: true,
	plan: 'scale',
	fields: [
		{ name: 'api_key', label_key: 'org.erp.mergeApiKey', secret: true, required: true },
		{ name: 'account_token', label_key: 'org.erp.accountToken', secret: true, required: true }
	]
};

const storedNetsuite = {
	type: 'netsuite',
	integration_method: 'direct',
	account_id: '123',
	consumer_secret: MASK
};

describe('groupProviders', () => {
	it('leads with the US and ZA groups, then the rest, Merge.dev last', () => {
		const groups = groupProviders([netsuite, qbo, syspro, merge]);
		expect(groups.map((g) => g.labelKey)).toEqual([
			'org.erp.group.us',
			'org.erp.group.za',
			'org.erp.group.other'
		]);
		expect(groups[0].providers.map((p) => p.key)).toEqual(['netsuite', 'quickbooks_online']);
		// An ERP popular in both regions appears under both.
		expect(groups[1].providers.map((p) => p.key)).toEqual(['netsuite', 'syspro']);
		expect(groups[2].providers.map((p) => p.key)).toEqual(['merge_dev']);
	});
});

describe('selectedProviderKey', () => {
	it('reads Merge.dev from the routing method, the default when absent', () => {
		expect(selectedProviderKey({ type: 'sap_s4hana', integration_method: 'merge_dev' })).toBe(
			'merge_dev'
		);
		expect(selectedProviderKey({ type: 'sap_s4hana' })).toBe('merge_dev');
		expect(selectedProviderKey(storedNetsuite)).toBe('netsuite');
		expect(selectedProviderKey(undefined)).toBeNull();
		expect(selectedProviderKey({})).toBeNull();
	});
});

describe('initialValues / secretIsSaved', () => {
	it('never puts the mask (or any secret) into an input', () => {
		const values = initialValues(netsuite, storedNetsuite, MASK);
		expect(values).toEqual({ account_id: '123', consumer_secret: '' });
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, MASK)).toBe(true);
		expect(secretIsSaved(netsuite, netsuite.fields[0], storedNetsuite, MASK)).toBe(false);
	});

	it('starts blank for a provider other than the one on file', () => {
		expect(initialValues(syspro, storedNetsuite, MASK)).toEqual({
			account_id: '',
			consumer_secret: ''
		});
		expect(secretIsSaved(syspro, syspro.fields[1], storedNetsuite, MASK)).toBe(false);
	});

	it('defaults a choice field to its first option', () => {
		expect(initialValues(qbo, undefined, MASK).environment).toBe('production');
	});
});

describe('buildErpPayload', () => {
	it('sends a direct ERP with its key, blank secrets kept blank, values trimmed', () => {
		expect(
			buildErpPayload(netsuite, { account_id: ' 999 ', consumer_secret: '' }, 'ignored')
		).toEqual({
			type: 'netsuite',
			integration_method: 'direct',
			account_id: '999',
			consumer_secret: ''
		});
	});

	it('sends Merge.dev with the long-tail ERP as its type', () => {
		expect(buildErpPayload(merge, { api_key: 'k', account_token: '' }, 'sap_s4hana')).toEqual({
			type: 'sap_s4hana',
			integration_method: 'merge_dev',
			api_key: 'k',
			account_token: ''
		});
	});

	it('never sends an OAuth block', () => {
		const body = buildErpPayload(qbo, { client_id: '', client_secret: '', environment: 'sandbox' }, '');
		expect(body).not.toHaveProperty('oauth');
	});
});

describe('missingRequired', () => {
	it('counts a saved secret as filled', () => {
		expect(missingRequired(netsuite, { account_id: '1', consumer_secret: '' }, storedNetsuite, MASK)).toEqual([]);
	});

	it('names required fields left empty', () => {
		const missing = missingRequired(netsuite, { account_id: ' ', consumer_secret: '' }, undefined, MASK);
		expect(missing.map((f) => f.name)).toEqual(['account_id', 'consumer_secret']);
	});
});

describe('readOAuthReturn', () => {
	it('reads the callback return, error first, bounded', () => {
		expect(readOAuthReturn(new URLSearchParams('section=erp&erp_connected=xero'))).toEqual({
			kind: 'connected',
			provider: 'xero'
		});
		expect(readOAuthReturn(new URLSearchParams('erp_connected=xero&erp_error=denied'))).toEqual({
			kind: 'error',
			code: 'denied'
		});
		const long = readOAuthReturn(new URLSearchParams(`erp_error=${'x'.repeat(500)}`));
		expect(long?.kind === 'error' && long.code.length).toBe(64);
		expect(readOAuthReturn(new URLSearchParams('section=erp'))).toBeNull();
	});
});
