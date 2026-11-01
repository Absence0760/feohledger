import { describe, expect, it } from 'vitest';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';
import {
	buildErpPayload,
	buildErpSecretUpdate,
	buildErpTestPayload,
	byoAppRequired,
	callUsageLines,
	type ErpCallUsage,
	fieldApplies,
	groupProviders,
	initialValues,
	missingRequired,
	oauthAttentionKey,
	oauthConnectionState,
	optionLabelKey,
	withoutOAuthReturn,
	type ErpOAuthStatus,
	OAUTH_ERROR_KEYS,
	CONNECT_FROM_PLATFORM_HOST,
	platformConnectUrl,
	destinationChanged,
	erpTestResultText,
	readOAuthReturn,
	secretIsSaved,
	selectedProviderKey,
	type ErpProvider
} from './erpConnection';

/** `GET /api/organization/credentials` → `erp`: the stored secret NAMES. */
const STORED_NAMES = ['consumer_secret'];

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
			options: ['production', 'sandbox'],
			// As the backend catalogue serves it (decisions §316).
			destination: false
		}
	]
};

/** Business Central: here `environment` IS a destination (a URL path segment). */
const bc: ErpProvider = {
	key: 'dynamics_365_bc',
	label: 'Microsoft Dynamics 365 Business Central',
	regions: ['US', 'ZA'],
	auth: 'credentials',
	docs_url: 'https://example.invalid/bc',
	available: true,
	fields: [
		{
			name: 'environment',
			label_key: 'org.erp.environment',
			secret: false,
			required: false,
			destination: true
		},
		{
			name: 'tenant_id',
			label_key: 'org.erp.tenantId',
			secret: false,
			required: true,
			destination: true
		},
		{ name: 'client_id', label_key: 'org.erp.clientId', secret: false, required: true },
		{ name: 'client_secret', label_key: 'org.erp.clientSecret', secret: true, required: true },
		{
			name: 'company_id',
			label_key: 'org.erp.companyId',
			secret: false,
			required: true,
			destination: true
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
	account_id: '123'
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
	it('never puts a secret into an input; "saved" comes from the stored names', () => {
		const values = initialValues(netsuite, storedNetsuite);
		expect(values).toEqual({ account_id: '123', consumer_secret: '' });
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, STORED_NAMES)).toBe(true);
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, [])).toBe(false);
		expect(secretIsSaved(netsuite, netsuite.fields[0], storedNetsuite, STORED_NAMES)).toBe(false);
	});

	it('stops treating a secret as saved once a destination field changes', () => {
		const same = initialValues(netsuite, storedNetsuite);
		const moved = { ...same, account_id: '999' };
		expect(destinationChanged(netsuite, same, storedNetsuite)).toBe(false);
		expect(destinationChanged(netsuite, moved, storedNetsuite)).toBe(true);
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, STORED_NAMES, same)).toBe(
			true
		);
		// The backend drops the stored secrets on a new destination, so the form
		// must not say it keeps them: the secret becomes required again.
		expect(secretIsSaved(netsuite, netsuite.fields[1], storedNetsuite, STORED_NAMES, moved)).toBe(
			false
		);
		expect(
			missingRequired(netsuite, moved, storedNetsuite, STORED_NAMES).map((f) => f.name)
		).toEqual(['consumer_secret']);
	});

	it('reads blank and absent destinations as the same', () => {
		const stored = { type: 'netsuite', integration_method: 'direct' };
		const values = { account_id: '  ', consumer_secret: '' };
		expect(destinationChanged(netsuite, values, stored)).toBe(false);
	});

	it('starts blank, with nothing saved, for a provider other than the one on file', () => {
		expect(initialValues(syspro, storedNetsuite)).toEqual({
			account_id: '',
			consumer_secret: ''
		});
		expect(secretIsSaved(syspro, syspro.fields[1], storedNetsuite, STORED_NAMES)).toBe(false);
	});

	it('defaults a choice field to its first option', () => {
		expect(initialValues(qbo, undefined).environment).toBe('production');
	});

	it("keeps QuickBooks' own-app secret across an environment change", () => {
		// The catalogue marks QuickBooks' environment destination: false (it
		// only picks between two Intuit hosts), so production → sandbox is the
		// same connection and Save must not ask for the client secret again.
		const stored = {
			type: 'quickbooks_online',
			integration_method: 'direct',
			client_id: 'byo',
			environment: 'production'
		};
		const values = { client_id: 'byo', client_secret: '', environment: 'sandbox' };
		const secret = qbo.fields[1];
		expect(destinationChanged(qbo, values, stored)).toBe(false);
		expect(secretIsSaved(qbo, secret, stored, ['client_secret'], values)).toBe(true);
		expect(
			missingRequired(qbo, values, stored, ['client_secret'], { requireByoApp: true })
		).toEqual([]);
	});

	it('still asks again when a Business Central environment changes', () => {
		const stored = {
			type: 'dynamics_365_bc',
			integration_method: 'direct',
			environment: 'production',
			tenant_id: 't',
			client_id: 'c',
			company_id: 'co'
		};
		const values = { ...initialValues(bc, stored), environment: 'sandbox' };
		const secret = bc.fields.find((f) => f.name === 'client_secret')!;
		expect(destinationChanged(bc, values, stored)).toBe(true);
		expect(secretIsSaved(bc, secret, stored, ['client_secret'], values)).toBe(false);
		expect(missingRequired(bc, values, stored, ['client_secret']).map((f) => f.name)).toEqual([
			'client_secret'
		]);
	});
});

describe('buildErpPayload', () => {
	it('sends a direct ERP with its key and trimmed values, and no secret field', () => {
		expect(
			buildErpPayload(netsuite, { account_id: ' 999 ', consumer_secret: 'typed' }, 'ignored')
		).toEqual({
			type: 'netsuite',
			integration_method: 'direct',
			account_id: '999'
		});
	});

	it('sends Merge.dev with the long-tail ERP as its type', () => {
		expect(buildErpPayload(merge, { api_key: 'k', account_token: '' }, 'sap_s4hana')).toEqual({
			type: 'sap_s4hana',
			integration_method: 'merge_dev'
		});
	});

	it('never sends an OAuth block', () => {
		const body = buildErpPayload(qbo, { client_id: '', client_secret: '', environment: 'sandbox' }, '');
		expect(body).not.toHaveProperty('oauth');
		expect(body).not.toHaveProperty('client_secret');
	});
});

describe('buildErpSecretUpdate', () => {
	it('sets what was typed, clears what was removed, and leaves blanks alone', () => {
		expect(
			buildErpSecretUpdate(
				merge,
				{ api_key: ' new-key ', account_token: '' },
				{ api_key: false, account_token: true }
			)
		).toEqual({ set: { api_key: 'new-key' }, clear: ['account_token'] });
	});

	it('is null when no secret changes, so no request is made', () => {
		expect(
			buildErpSecretUpdate(netsuite, { account_id: '9', consumer_secret: '' }, {})
		).toBeNull();
	});

	it('lets a typed value win over a stale remove toggle, and never sends a non-secret', () => {
		expect(
			buildErpSecretUpdate(
				netsuite,
				{ account_id: '9', consumer_secret: 'typed' },
				{ consumer_secret: true }
			)
		).toEqual({ set: { consumer_secret: 'typed' } });
	});
});

describe('buildErpTestPayload', () => {
	it('carries the typed secrets for the test, and leaves blank ones to the server', () => {
		expect(
			buildErpTestPayload(merge, { api_key: 'typed', account_token: '' }, 'sap_s4hana')
		).toEqual({ type: 'sap_s4hana', integration_method: 'merge_dev', api_key: 'typed' });
	});
});

describe('missingRequired', () => {
	it('counts a stored secret as filled', () => {
		expect(
			missingRequired(
				netsuite,
				{ account_id: '123', consumer_secret: '' },
				storedNetsuite,
				STORED_NAMES
			)
		).toEqual([]);
	});

	it('names required fields left empty', () => {
		const missing = missingRequired(netsuite, { account_id: ' ', consumer_secret: '' }, undefined, []);
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
		const { loadAllMessages } = await import('#lib/i18n/allMessages.ts');
		for (const locale of SUPPORTED_LOCALES) {
			const messages = await loadAllMessages(locale);
			for (const key of Object.values(OAUTH_ERROR_KEYS)) {
				expect(messages[key], `${locale}: ${key}`).toBeTruthy();
			}
		}
		expect(OAUTH_ERROR_KEYS.no_external_tenant).toBe('org.erp.oauth.error.noExternalTenant');
		// The login-CSRF refusal: a callback from a browser that did not start it.
		expect(OAUTH_ERROR_KEYS.browser_mismatch).toBe('org.erp.oauth.error.browserMismatch');
	});

	it('states the connect-from-platform refusal in every locale', async () => {
		const { loadAllMessages } = await import('#lib/i18n/allMessages.ts');
		for (const locale of SUPPORTED_LOCALES) {
			const messages = await loadAllMessages(locale);
			for (const key of [
				'org.erp.oauth.connectFromPlatform',
				'org.erp.oauth.connectUnavailableHere'
			] as const) {
				expect(messages[key], `${locale}: ${key}`).toContain('{erp}');
			}
			expect(messages['org.erp.oauth.connectFromPlatformLink'], locale).toBeTruthy();
		}
	});
});

describe('platformConnectUrl', () => {
	it('is the code the backend refuses a vanity-host connect with', () => {
		expect(CONNECT_FROM_PLATFORM_HOST).toBe('erp_connect_platform_host');
	});

	it('links only an absolute http(s) URL', () => {
		expect(platformConnectUrl({ url: 'https://acme.feohledger.com/organization?section=erp' })).toBe(
			'https://acme.feohledger.com/organization?section=erp'
		);
		expect(platformConnectUrl({ url: 'http://acme.localhost:7777/organization' })).toBe(
			'http://acme.localhost:7777/organization'
		);
		expect(platformConnectUrl({})).toBeNull();
		expect(platformConnectUrl({ url: 42 })).toBeNull();
		expect(platformConnectUrl({ url: '/organization' })).toBeNull();
		expect(platformConnectUrl({ url: 'javascript:alert(1)' })).toBeNull();
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

describe('buildErpPayload: unrendered settings', () => {
	it('sends back a stored setting the form does not render, for the same ERP only', () => {
		const stored = {
			...storedNetsuite,
			transaction_code_values: [{ id: 1, value: 'Ops' }],
			oauth: { connected: true }
		};
		const values = { account_id: '9', consumer_secret: '' };
		const body = buildErpPayload(netsuite, values, '', stored);
		expect(body.transaction_code_values).toEqual([{ id: 1, value: 'Ops' }]);
		// The form's own value wins over what is stored; the OAuth block is never sent.
		expect(body.account_id).toBe('9');
		expect(body).not.toHaveProperty('oauth');
		// A different ERP starts clean.
		const other = buildErpPayload(syspro, values, '', stored);
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
			STORED_NAMES,
			{ clear: { consumer_secret: true } }
		);
		expect(missing.map((f) => f.name)).toEqual(['consumer_secret']);
	});

	it('requires the client id and secret when no app is configured', () => {
		const values = { client_id: '', client_secret: '', environment: 'production' };
		expect(missingRequired(xero, values, undefined, [])).toEqual([]);
		const missing = missingRequired(xero, values, undefined, [], { requireByoApp: true });
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

describe('oauthAttentionKey', () => {
	it('names why a lost connection must be reconnected (decisions §295)', () => {
		const revoked = status({
			provider: 'quickbooks_online',
			needs_reconnect: true,
			reconnect_reason: 'refresh_token_revoked'
		});
		expect(oauthAttentionKey('quickbooks_online', revoked)).toBe(
			'org.erp.oauth.needsReconnectRevoked'
		);
		const expired = status({
			provider: 'quickbooks_online',
			needs_reconnect: true,
			reconnect_reason: 'refresh_token_expired'
		});
		expect(oauthAttentionKey('quickbooks_online', expired)).toBe(
			'org.erp.oauth.needsReconnectExpired'
		);
		// A reason this build does not know keeps the generic sentence.
		const other = status({ provider: 'quickbooks_online', needs_reconnect: true });
		expect(oauthAttentionKey('quickbooks_online', other)).toBe('org.erp.oauth.needsReconnect');
	});

	it('warns before expiry only for a live connection with a stated expiry', () => {
		const soon = status({
			provider: 'quickbooks_online',
			connected: true,
			expires_soon: true,
			refresh_token_expires_at: '2026-11-01T00:00:00+00:00'
		});
		expect(oauthAttentionKey('quickbooks_online', soon)).toBe('org.erp.oauth.expiresSoon');
		expect(
			oauthAttentionKey('quickbooks_online', { ...soon, refresh_token_expires_at: null })
		).toBeNull();
		expect(oauthAttentionKey('quickbooks_online', { ...soon, expires_soon: false })).toBeNull();
		// Another ERP's connection says nothing about this one.
		expect(oauthAttentionKey('xero', soon)).toBeNull();
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
		const { loadAllMessages } = await import('#lib/i18n/allMessages.ts');
		// Blackbaud approval_status, Xero bill_status, QuickBooks environment, Merge.dev "other".
		const values = ['Pending', 'Approved', 'AUTHORISED', 'DRAFT', 'production', 'sandbox', 'other'];
		for (const locale of SUPPORTED_LOCALES) {
			const messages = await loadAllMessages(locale);
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

describe('erpTestResultText', () => {
	const t = (key: string, params?: Record<string, string | number>) =>
		`${key}${params ? ' ' + JSON.stringify(params) : ''}`;
	const label = (p: ErpProvider) => p.label;
	const merge: ErpProvider = { ...netsuite, key: 'merge_dev', label: 'Merge.dev' };

	it('names a connected ERP by its display name, not its key', () => {
		const result = {
			success: true,
			code: 'connected',
			provider: 'quickbooks_online',
			message: 'Connected to quickbooks_online successfully'
		};
		expect(erpTestResultText(result, qbo, label, t)).toBe(
			'org.erp.test.connected {"erp":"QuickBooks Online"}'
		);
	});

	it('tells an OAuth ERP to reconnect and a credentials ERP to check its details', () => {
		const failed = (provider: string) => ({
			success: false,
			code: 'connection_failed',
			provider,
			message: 'Connection failed — check your credentials'
		});
		expect(erpTestResultText(failed('quickbooks_online'), qbo, label, t)).toBe(
			'org.erp.test.failedOAuth {"erp":"QuickBooks Online"}'
		);
		expect(erpTestResultText(failed('netsuite'), netsuite, label, t)).toBe(
			'org.erp.test.failed {"erp":"Oracle NetSuite"}'
		);
		// A Merge.dev connection reports the linked ERP's type as its provider.
		expect(erpTestResultText(failed('sap_s4hana'), merge, label, t)).toBe(
			'org.erp.test.failed {"erp":"Merge.dev"}'
		);
	});

	it('states the provider-less codes', () => {
		expect(
			erpTestResultText(
				{ success: false, code: 'credentials_unavailable', message: 'x' },
				netsuite,
				label,
				t
			)
		).toBe('org.erp.test.credentialsUnavailable');
		expect(
			erpTestResultText(
				{ success: false, code: 'unknown_adapter', adapter: 'quikbooks', message: 'x' },
				netsuite,
				label,
				t
			)
		).toBe('org.erp.test.unknownAdapter {"adapter":"quikbooks"}');
	});

	it.each([
		['no code (an older backend)', { success: true, message: 'server says' }],
		['an unknown code', { success: true, code: 'brand_new', message: 'server says' }],
		[
			'a result for another ERP than the form shows',
			{ success: true, code: 'connected', provider: 'xero', message: 'server says' }
		],
		['an unknown adapter with no key', { success: false, code: 'unknown_adapter', message: 'server says' }]
	])('falls back to the server sentence for %s', (_label, result) => {
		expect(erpTestResultText(result, qbo, label, t)).toBe('server says');
	});
});

/** NetSuite as the catalogue now serves it: a sign-in method, then each
 *  method's own credentials (`catalog.py`, issue #653). */
const OAUTH2 = { field: 'auth_method', values: ['oauth2_m2m'] };
const TBA = { field: 'auth_method', values: ['tba'] };
const netsuiteTwoMethods: ErpProvider = {
	...netsuite,
	fields: [
		{ name: 'account_id', label_key: 'org.erp.accountId', secret: false, required: true },
		{
			name: 'auth_method',
			label_key: 'org.erp.field.auth_method',
			secret: false,
			required: true,
			options: ['oauth2_m2m', 'tba'],
			stored_default: 'tba'
		},
		{ name: 'client_id', label_key: 'org.erp.clientId', secret: false, required: true, show_when: OAUTH2 },
		{
			name: 'private_key',
			label_key: 'org.erp.field.private_key',
			secret: true,
			required: true,
			show_when: OAUTH2
		},
		{ name: 'consumer_key', label_key: 'org.erp.consumerKey', secret: false, required: true, show_when: TBA },
		{
			name: 'consumer_secret',
			label_key: 'org.erp.consumerSecret',
			secret: true,
			required: true,
			show_when: TBA
		}
	]
};

describe('NetSuite sign-in method (show_when / stored_default)', () => {
	it('starts a new connection on OAuth 2.0, the first option', () => {
		expect(initialValues(netsuiteTwoMethods, undefined).auth_method).toBe('oauth2_m2m');
	});

	it('reads a saved connection with no method as TBA, as the adapter does', () => {
		// Showing the first option here would switch a working tenant on save.
		expect(initialValues(netsuiteTwoMethods, storedNetsuite).auth_method).toBe('tba');
		const saved = { ...storedNetsuite, auth_method: 'oauth2_m2m' };
		expect(initialValues(netsuiteTwoMethods, saved).auth_method).toBe('oauth2_m2m');
	});

	it('applies only the chosen method’s fields', () => {
		const shown = (method: string) =>
			netsuiteTwoMethods.fields
				.filter((f) => fieldApplies(f, { auth_method: method }))
				.map((f) => f.name);
		expect(shown('oauth2_m2m')).toEqual(['account_id', 'auth_method', 'client_id', 'private_key']);
		expect(shown('tba')).toEqual(['account_id', 'auth_method', 'consumer_key', 'consumer_secret']);
	});

	it('never requires the other method’s credentials', () => {
		const values = { account_id: '123', auth_method: 'oauth2_m2m', client_id: '', private_key: '' };
		expect(missingRequired(netsuiteTwoMethods, values, undefined, []).map((f) => f.name)).toEqual([
			'client_id',
			'private_key'
		]);
	});

	it('labels both methods', () => {
		expect(optionLabelKey('oauth2_m2m')).toBe('org.erp.option.oauth2_m2m');
		expect(optionLabelKey('tba')).toBe('org.erp.option.tba');
	});
});

describe('callUsageLines', () => {
	const t = (key: string, params?: Record<string, string | number>) =>
		`${key}${params ? ' ' + JSON.stringify(params) : ''}`;
	// Stand-ins that show the figures went through the locale formatters.
	const fmt = { count: (n: number) => `#${n}`, time: (iso: string) => `@${iso}` };
	const RESET = '2026-10-27T00:00:00+00:00';
	const usage = (overrides: Partial<ErpCallUsage> = {}): ErpCallUsage => ({
		scope: 'org_share',
		used: 1234,
		limit: 2500,
		resets_at: RESET,
		paused_reason: null,
		paused_until: null,
		...overrides
	});

	it('says nothing when the status carries no usage', () => {
		expect(callUsageLines(null, 'Blackbaud', t, fmt)).toEqual([]);
		expect(callUsageLines(undefined, 'Blackbaud', t, fmt)).toEqual([]);
	});

	it("measures a platform-key org against its share and its own key against the key's quota", () => {
		expect(callUsageLines(usage(), 'Blackbaud', t, fmt)).toEqual([
			`org.erp.usage.ofShare {"erp":"Blackbaud","used":"#1234","limit":"#2500","reset":"@${RESET}"}`
		]);
		expect(
			callUsageLines(usage({ scope: 'subscription', limit: 25000 }), 'Blackbaud', t, fmt)
		).toEqual([
			`org.erp.usage.ofQuota {"erp":"Blackbaud","used":"#1234","limit":"#25000","reset":"@${RESET}"}`
		]);
		expect(callUsageLines(usage({ limit: null }), 'Blackbaud', t, fmt)).toEqual([
			`org.erp.usage.noLimit {"erp":"Blackbaud","used":"#1234","reset":"@${RESET}"}`
		]);
	});

	it('says why calls are paused and until when, and falls back for a reason it does not know', () => {
		const until = '2026-10-26T14:02:00+00:00';
		const paused = (reason: string) =>
			callUsageLines(usage({ paused_reason: reason, paused_until: until }), 'FE NXT', t, fmt)[1];
		expect(paused('rate_limited')).toBe(
			`org.erp.usage.paused.rateLimited {"erp":"FE NXT","until":"@${until}"}`
		);
		expect(paused('tenant_budget')).toBe(
			`org.erp.usage.paused.tenantBudget {"erp":"FE NXT","until":"@${until}"}`
		);
		expect(paused('subscription_quota')).toBe(
			`org.erp.usage.paused.subscriptionQuota {"erp":"FE NXT","until":"@${until}"}`
		);
		expect(paused('something_new')).toBe(`org.erp.usage.paused {"erp":"FE NXT","until":"@${until}"}`);
		expect(paused('toString')).toBe(`org.erp.usage.paused {"erp":"FE NXT","until":"@${until}"}`);
		// A reason with no time is not a pause the page can state.
		expect(callUsageLines(usage({ paused_reason: 'rate_limited' }), 'X', t, fmt)).toHaveLength(1);
	});

	it('has every usage sentence in every locale', async () => {
		const { loadAllMessages } = await import('#lib/i18n/allMessages.ts');
		const keys: MessageKey[] = [
			'org.erp.usage.ofShare',
			'org.erp.usage.ofQuota',
			'org.erp.usage.noLimit',
			'org.erp.usage.paused',
			'org.erp.usage.paused.rateLimited',
			'org.erp.usage.paused.tenantBudget',
			'org.erp.usage.paused.subscriptionQuota'
		];
		for (const locale of SUPPORTED_LOCALES) {
			const messages = await loadAllMessages(locale);
			for (const key of keys) expect(messages[key], `${locale}: ${key}`).toBeTruthy();
		}
	});
});
