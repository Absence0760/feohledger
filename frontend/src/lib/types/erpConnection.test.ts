import { describe, expect, it } from 'vitest';
import {
	buildErpPayload,
	byoAppRequired,
	groupProviders,
	initialValues,
	missingRequired,
	oauthConnectionState,
	optionLabelKey,
	withoutOAuthReturn,
	type ErpOAuthStatus,
	OAUTH_ERROR_KEYS,
	destinationChanged,
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
		{
			name: 'account_id',
			label_key: 'org.erp.accountId',
			secret: false,
			required: true,
			destination: true
		},
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

	it('stops treating a secret as saved once a destination field changes', () => {
		const same = initialValues(netsuite, storedNetsuite, MASK);
		const moved = { ...same, account_id: '999' };
		expect(destinationChanged(netsuite, same, storedNetsuite)).toBe(false);
		expect(destinationChanged(netsuite, moved, storedNetsuite)).toBe(true);
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, MASK, same)).toBe(true);
		// The backend keeps no stored secret across a new destination, so the
		// form must not say it does: the secret becomes required again.
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, MASK, moved)).toBe(false);
		expect(missingRequired(netsuite, moved, storedNetsuite, MASK).map((f) => f.name)).toEqual([
			'consumer_secret'
		]);
	});

	it('reads blank and absent destinations as the same', () => {
		const stored = { type: 'netsuite', integration_method: 'direct', consumer_secret: MASK };
		const values = { account_id: '  ', consumer_secret: '' };
		expect(destinationChanged(netsuite, values, stored)).toBe(false);
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
		expect(missingRequired(netsuite, { account_id: '123', consumer_secret: '' }, storedNetsuite, MASK)).toEqual([]);
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

describe('OAUTH_ERROR_KEYS', () => {
	it('explains every actionable callback code in every locale', async () => {
		const { CATALOGUE_LOADERS } = await import('#lib/i18n/catalogues.ts');
		for (const [locale, load] of Object.entries(CATALOGUE_LOADERS)) {
			const messages = await load();
			for (const key of Object.values(OAUTH_ERROR_KEYS)) {
				expect(messages[key], `${locale}: ${key}`).toBeTruthy();
			}
		}
		expect(OAUTH_ERROR_KEYS.no_external_tenant).toBe('org.erp.oauth.error.noExternalTenant');
	});
});

function status(overrides: Partial<ErpOAuthStatus> = {}): ErpOAuthStatus {
	return {
		provider: null,
		connected: false,
		needs_reconnect: false,
		redirect_uri: 'http://localhost:8000/api/erp/oauth/callback',
		providers: [
			{
				key: 'quickbooks_online',
				display_name: 'QuickBooks Online',
				available: true,
				client_source: 'platform'
			},
			{ key: 'xero', display_name: 'Xero', available: false, client_source: null }
		],
		...overrides
	};
}

const xero: ErpProvider = { ...qbo, key: 'xero', label: 'Xero', available: true };

describe('buildErpPayload: removed secrets and unrendered settings', () => {
	it('sends a removed secret as null, unless a new value was typed', () => {
		const removed = new Set(['consumer_secret']);
		const blank = { account_id: '1', consumer_secret: '' };
		expect(buildErpPayload(netsuite, blank, '', removed).consumer_secret).toBeNull();
		const typed = { account_id: '1', consumer_secret: 'new' };
		expect(buildErpPayload(netsuite, typed, '', removed).consumer_secret).toBe('new');
	});

	it('sends back a stored setting the form does not render, for the same ERP only', () => {
		const stored = {
			...storedNetsuite,
			transaction_code_values: [{ id: 1, value: 'Ops' }],
			oauth: { connected: true }
		};
		const values = { account_id: '9', consumer_secret: '' };
		const body = buildErpPayload(netsuite, values, '', new Set(), stored);
		expect(body.transaction_code_values).toEqual([{ id: 1, value: 'Ops' }]);
		// The form's own value wins over what is stored; the OAuth block is never sent.
		expect(body.account_id).toBe('9');
		expect(body).not.toHaveProperty('oauth');
		// A different ERP starts clean.
		const other = buildErpPayload(syspro, values, '', new Set(), stored);
		expect(other).not.toHaveProperty('transaction_code_values');
	});
});

describe('byoAppRequired', () => {
	it('is true only when the status says no app is configured for that ERP', () => {
		expect(byoAppRequired(xero, status())).toBe(true);
		expect(byoAppRequired(qbo, status())).toBe(false);
		// Unknown is never a demand.
		expect(byoAppRequired(xero, null)).toBe(false);
		expect(byoAppRequired(netsuite, status())).toBe(false);
		expect(byoAppRequired({ ...xero, key: 'sage_accounting' }, status())).toBe(false);
	});
});

describe('missingRequired: removed secrets and a required own app', () => {
	it('counts a removed required secret as missing', () => {
		const missing = missingRequired(
			netsuite,
			{ account_id: '1', consumer_secret: '' },
			storedNetsuite,
			MASK,
			{ cleared: new Set(['consumer_secret']) }
		);
		expect(missing.map((f) => f.name)).toEqual(['consumer_secret']);
	});

	it('requires the client id and secret when no app is configured', () => {
		const values = { client_id: '', client_secret: '', environment: 'production' };
		expect(missingRequired(xero, values, undefined, MASK)).toEqual([]);
		const missing = missingRequired(xero, values, undefined, MASK, { requireByoApp: true });
		expect(missing.map((f) => f.name)).toEqual(['client_id', 'client_secret']);
	});
});

describe('oauthConnectionState', () => {
	it('reads connected, needs-reconnect and not-connected for the ERP shown', () => {
		const connected = status({ provider: 'xero', connected: true });
		expect(oauthConnectionState('xero', connected)).toBe('connected');
		const expired = status({ provider: 'xero', needs_reconnect: true });
		expect(oauthConnectionState('xero', expired)).toBe('needs_reconnect');
		// Connected to a different ERP is not connected to this one.
		expect(oauthConnectionState('quickbooks_online', connected)).toBe('not_connected');
		expect(oauthConnectionState('xero', null)).toBe('not_connected');
	});
});

describe('withoutOAuthReturn', () => {
	it('drops both return params and keeps the rest', () => {
		const base = 'http://acme.localhost/organization';
		expect(withoutOAuthReturn(new URL(`${base}?section=erp&erp_error=x`))).toBe(
			'/organization?section=erp'
		);
		expect(withoutOAuthReturn(new URL(`${base}?erp_connected=xero&section=erp#top`))).toBe(
			'/organization?section=erp#top'
		);
		expect(withoutOAuthReturn(new URL(`${base}?section=erp`))).toBeNull();
	});
});

describe('optionLabelKey', () => {
	it('labels every catalogued option value in every locale, and nothing else', async () => {
		const { CATALOGUE_LOADERS } = await import('#lib/i18n/catalogues.ts');
		// Blackbaud approval_status, Xero bill_status, QuickBooks environment, Merge.dev "other".
		const values = ['Pending', 'Approved', 'AUTHORISED', 'DRAFT', 'production', 'sandbox', 'other'];
		for (const [locale, load] of Object.entries(CATALOGUE_LOADERS)) {
			const messages = await load();
			for (const value of values) {
				const key = optionLabelKey(value);
				expect(key, value).not.toBeNull();
				expect(messages[key!], `${locale}: ${key}`).toBeTruthy();
			}
		}
		expect(optionLabelKey('sap_s4hana')).toBeNull();
		expect(optionLabelKey('toString')).toBeNull();
	});
});
